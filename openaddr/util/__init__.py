import logging; _L = logging.getLogger('openaddr.util')

from urllib.parse import urlparse, parse_qsl, urljoin
from datetime import datetime, timedelta, date
from os.path import join, basename, splitext, dirname
from operator import attrgetter
from tempfile import mkstemp
from os import close
import ftplib, httmock
import io, zipfile
import json, time
import shlex

from boto.exception import EC2ResponseError
from boto.ec2 import blockdevicemapping

# http://docs.aws.amazon.com/AWSEC2/latest/UserGuide/InstanceStorage.html
block_device_sizes = {'r3.large': 32, 'r3.xlarge': 80, 'r3.2xlarge': 160, 'r3.4xlarge': 320}

def get_version():
    ''' Prevent circular imports.
    '''
    from .. import __version__
    return __version__

def prepare_db_kwargs(dsn):
    '''
    '''
    p = urlparse(dsn)
    q = dict(parse_qsl(p.query))

    assert p.scheme == 'postgres'
    kwargs = dict(user=p.username, password=p.password, host=p.hostname, port=p.port)
    kwargs.update(dict(database=p.path.lstrip('/')))

    if 'sslmode' in q:
        kwargs.update(dict(sslmode=q['sslmode']))
    
    return kwargs

def set_autoscale_capacity(autoscale, cloudwatch, cloudwatch_ns, capacity):
    '''
    '''
    span, now = 60 * 60 * 3, datetime.now()
    start, end = now - timedelta(seconds=span), now
    args = 'tasks queue', cloudwatch_ns, 'Maximum'

    (measure, ) = cloudwatch.get_metric_statistics(span, start, end, *args)

    group_name = 'CI Workers {0}.x'.format(*get_version().split('.'))
    (group, ) = autoscale.get_all_groups([group_name])
    
    if group.desired_capacity >= capacity:
        return
    
    if measure['Maximum'] > .9:
        group.set_capacity(capacity)

def request_task_instance(ec2, autoscale, instance_type, chef_role, lifespan, command, bucket, aws_sns_arn):
    '''
    '''
    group_name = 'CI Workers {0}.x'.format(*get_version().split('.'))

    (group, ) = autoscale.get_all_groups([group_name])
    (config, ) = autoscale.get_all_launch_configurations(names=[group.launch_config_name])
    (image, ) = ec2.get_all_images(image_ids=[config.image_id])
    keypair = [kp for kp in ec2.get_all_key_pairs() if kp.name.startswith('oa-')][0]

    yyyymmdd = datetime.utcnow().strftime('%Y-%m-%d-%H-%M')
    
    with open(join(dirname(__file__), 'templates', 'task-instance-userdata.sh')) as file:
        userdata_kwargs = dict(
            role = shlex.quote(chef_role),
            command = ' '.join(map(shlex.quote, command)),
            lifespan = shlex.quote(str(lifespan)),
            version = shlex.quote(get_version()),
            log_prefix = shlex.quote('logs/{}-{}'.format(yyyymmdd, command[0])),
            bucket = shlex.quote(bucket or 'data.openaddresses.io'),
            aws_sns_arn = '', aws_region = '',
            command_name = command[0]
            )
        
        if aws_sns_arn:
            try:
                _, _, _, aws_region, _ = aws_sns_arn.split(':', 4)
            except ValueError:
                pass
            else:
                if aws_sns_arn.startswith('arn:aws:sns:'):
                    userdata_kwargs.update(aws_sns_arn = shlex.quote(aws_sns_arn),
                                           aws_region = shlex.quote(aws_region))
    
        device_map = blockdevicemapping.BlockDeviceMapping()

        if instance_type in block_device_sizes:
            device_map = blockdevicemapping.BlockDeviceMapping()
            dev_sdb = blockdevicemapping.BlockDeviceType()
            dev_sdb.size = block_device_sizes[instance_type]
            device_map['/dev/sdb'] = dev_sdb

        run_kwargs = dict(instance_type=instance_type, security_groups=['default'],
                          instance_initiated_shutdown_behavior='terminate',
                          user_data=file.read().format(**userdata_kwargs),
                          # TODO: use current role from http://169.254.169.254/latest/meta-data/iam/info
                          instance_profile_name='machine-communication',
                          key_name=keypair.name, block_device_map=device_map)
        
    reservation = image.run(**run_kwargs)
    (instance, ) = reservation.instances
    
    try:
        instance.add_tag('Name', 'Scheduled {} {}'.format(yyyymmdd, command[0]))
    except EC2ResponseError:
        time.sleep(10)
        try:
            instance.add_tag('Name', 'Scheduled {} {}'.format(yyyymmdd, command[0]))
        except EC2ResponseError:
            time.sleep(10)
            instance.add_tag('Name', 'Scheduled {} {}'.format(yyyymmdd, command[0]))
    
    _L.info('Started EC2 instance {} from AMI {}'.format(instance, image))
    
    return instance

def package_output(source, processed_path, website, license):
    ''' Write a zip archive to temp dir with processed data and optional .vrt.
    '''
    _, ext = splitext(processed_path)
    handle, zip_path = mkstemp(prefix='util-package_output-', suffix='.zip')
    close(handle)
    
    zip_file = zipfile.ZipFile(zip_path, mode='w', compression=zipfile.ZIP_DEFLATED)
    
    template = join(dirname(__file__), 'templates', 'README.txt')
    with io.open(template, encoding='utf8') as file:
        content = file.read().format(website=website, license=license, date=date.today())
        zip_file.writestr('README.txt', content.encode('utf8'))

    if ext == '.csv':
        # Add virtual format to make CSV readable by QGIS, OGR, etc.
        # More information: http://www.gdal.org/drv_vrt.html
        template = join(dirname(__file__), 'templates', 'conform-result.vrt')
        with io.open(template, encoding='utf8') as file:
            content = file.read().format(source=basename(source))
            zip_file.writestr(source + '.vrt', content.encode('utf8'))
    
    zip_file.write(processed_path, source + ext)
    zip_file.close()
    
    return zip_path

def summarize_result_licenses(results):
    '''
    '''
    template = u'{source}\nWebsite: {website}\nLicense: {license}\nRequired attribution: {attribution}\n'
    license_lines = [u'Data collected by OpenAddresses (http://openaddresses.io).\n']
    
    for result in sorted(results, key=attrgetter('source_base')):
        attribution = 'No'
        if result.run_state.attribution_flag != 'false':
            attribution = result.run_state.attribution_name or 'Yes'

        license_line = template.format(
            source=result.source_base,
            website=result.run_state.website or 'Unknown',
            license=result.run_state.license or 'Unknown',
            attribution=attribution
            )

        license_lines.append(license_line)

    return '\n'.join(license_lines)

def build_request_ftp_file_callback():
    '''
    '''
    file = io.BytesIO()
    callback = lambda bytes: file.write(bytes)
    return file, callback

def request_ftp_file(url):
    '''
    '''
    _L.info('Getting {} via FTP'.format(url))
    parsed = urlparse(url)
    
    try:
        ftp = ftplib.FTP(parsed.hostname)
        ftp.login(parsed.username, parsed.password)
    
        file, callback = build_request_ftp_file_callback()
        ftp.retrbinary('RETR {}'.format(parsed.path), callback)
        file.seek(0)
    except Exception as e:
        _L.warning('Got an error from {}: {}'.format(parsed.hostname, e))
        return httmock.response(400, b'', headers={'Content-Type': 'application/octet-stream'})

    # Using mock response because HTTP responses are expected downstream
    return httmock.response(200, file.read(), headers={'Content-Type': 'application/octet-stream'})

def s3_key_url(key):
    '''
    '''
    base = u'https://s3.amazonaws.com'
    path = join(key.bucket.name, key.name.lstrip('/'))
    
    return urljoin(base, path)
