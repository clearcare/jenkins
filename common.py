#!/usr/bin/env python
# Common functions that can be used anywhere
import subprocess
import sys
import os
import time
import datetime
try:
    import dateutil.parser
except:
    pass
import unicodedata
import random
import json
import traceback
try:
    btermcolor = True
    import termcolor
except:
    btermcolor = False


# AWS environment mappings:
if os.path.exists('environment.json'):
    with open('environment.json', 'r') as f:
            g_env_map = json.load(f)
else:
    print('***ERROR: environment.json not found.')
    print('It must be in the same directory as common.py')
    print('It\'s contents must be as follows:')

    sample = {
        'user-friendly-env-name': {
            'ami_id': 'UNKNOWN',
            'account-id': '123456789012',
            'region': 'us-west-2',
            'vpcid': 'vpc-12345678',
            'jenkins-sg': 'sg-12345678',
            'jenkins-master-sg': 'sg-abcdefgh',
            'instance-profile': 'jenkins-cloud-ope',
            'vpcsubnet': [{'id': 'subnet-12345678', 'az': 'us-west-2a'},
                          {'id': 'subnet-12345678', 'az': 'us-west-2b'},
                          {'id': 'subnet-12345678', 'az': 'us-west-2c'}],
            'jenkins_url': 'https://my-jenkins-master.com/'}
    }
    print(json.dumps(sample, indent=4, sort_keys=True))
    sys.exit(1)


class CreateInstanceException(Exception):
    def __init__(self, message, instance_id=None):

        # Call the base class constructor with the parameters it needs
        super(CreateInstanceException, self).__init__(message)

        # Now for your custom code...
        self.instance_id = instance_id


# We need to flush stdout for Jenkins:
def say(s, banner=None, file_name=None, color=None, do_print=True, use_termcolor=True):
    raw_s = s
    if banner is not None:
        s = '{}\n{}\n{}'.format(banner * 50, str(s), banner * 50)

    # If termcolor package is installed, use it:
    if btermcolor and use_termcolor is True:
        s = termcolor.colored(str(s), color=color, attrs=['bold', 'dark'])

    if do_print is True:
        print(s)

    if file_name is not None:
        with open(file_name, 'a+') as fd:
            fd.write(str(raw_s) + '\n')
    sys.stdout.flush()


def safe_str(obj):
    """ return the byte string representation of obj """
    try:
        return str(obj)
    except UnicodeEncodeError:
        # obj is unicode
        return unicode(obj).encode('unicode_escape')


# Helper function to strip out unicode characters from a string:
def strip_unicode(s):
    if sys.version_info >= (3, 0):
        # 'str' object has no attribute 'decode'.
        # In python 3, string is already decoded:
        # We decode to utf-8 to convert it back to str class:
        return s.encode('ascii', 'ignore').decode("utf-8")
    else:
        return s.decode('unicode_escape').encode('ascii', 'ignore')


# General purpose run command:
def run(cmd, hide_command=True, raise_on_failure=True,
        separate_std_out_err=False, retry_count=0,
        retry_sleep_secs=30, debug=False):
    try:
        xrange
    except NameError:
        xrange = range
    for i_attempt in xrange(retry_count + 1):
        if hide_command is False or debug is True:
            say('cmd: {0}'.format(cmd))
        output = None
        stdout = None
        stderr = None
        returncode = None
        try:
            if separate_std_out_err is False:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, shell=True)
                output = safe_str(p.communicate()[0])
                returncode = p.returncode
            else:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, shell=True)
                stdout, stderr = p.communicate()
                # Normalize any special characters:
                stdout = safe_str(stdout)
                stderr = safe_str(stderr)
                returncode = p.returncode
        except Exception:
            say('***Error in command: {0}'.format(cmd))
            say('Exception:----------------')
            say(traceback.format_exc())
            say('--------------------------')
        if returncode != 0:
            # There was an error, lets retry, if possible:
            if i_attempt != retry_count:
                # Only sleep if not end of the loop:
                if debug is True:
                    say('retrying command: {}, after sleeping: {}s'.format(cmd, retry_sleep_secs))
                time.sleep(retry_sleep_secs)
            continue
        else:
            # Command was success, let's not retry:
            break

    if returncode != 0 and raise_on_failure is True:
        say('***Error in command and raise_on_failure is True so exiting. CMD:\n{0}'.format(cmd))
        all_output = None
        if separate_std_out_err is False:
            all_output = output
        else:
            all_output = stdout + '\n' + stderr
        say('This is the output from that command, if any:\n{0}'.format(all_output))
        raise Exception('Command_Error')
    if separate_std_out_err is True:
        if debug is True:
            say('Debug Information:\nstdout:\n{0}\nstderr:\n{1}\nreturncode: {2}'.format(stdout, stderr, returncode))
        return stdout, stderr, returncode
    else:
        if debug is True:
            say('Debug Information:\noutput:\n{0}\nreturncode: {1}'.format(output, returncode))
        return output, returncode


# Helper function to extract info from g_env_map:
def getAzFromSubnet(target_env, subnet_id):
    for subnet in g_env_map['environments'][target_env]['vpcsubnet']:
        if subnet_id == subnet['id']:
            return subnet['az']
    print('***Error: Could not find AZ in target_env: {0} from subnet_id: {1}'.format(target_env, subnet_id))
    return None


# Convert Instance Launch time to (d, h, m, s):
def timeDiff(launch_time, current_time):
    # Inputs are datetime objects.
    running_time = current_time - launch_time
    return convertSecondsToDateFormat(seconds=running_time.total_seconds())


# Helper function to get proper days/hours/minutes/seconds:
def convertSecondsToDateFormat(seconds):
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    elapsed_time = "%d:%d:%02d:%02d" % (d, h, m, s)
    return (d, h, m, s)


# General purpose boto connect function:
def connect(func=None, *args, **kwargs):
    # Examples:
    # conn = connect(boto.ec2.elb.connect_to_region, region_name=region)
    # conn = connect(boto.connect_iam)
    # conn = connect(boto.sts.STSConnection, 'us-west-2')
    # conn = connect(boto.ec2.connect_to_region, 'us-west-2')
    # conn = connect(boto.sqs.connect_to_region, 'us-west-2')
    conn = None
    if ('AWS_ACCESS_KEY_ID' in os.environ and 'AWS_SECRET_ACCESS_KEY' in os.environ):
        aaki = os.environ['AWS_ACCESS_KEY_ID']
        asak = os.environ['AWS_SECRET_ACCESS_KEY']
        st = None if 'AWS_SESSION_TOKEN' not in os.environ else os.environ['AWS_SESSION_TOKEN']
        # Remember to pass the environmental variables into the connection.
        conn = func(*args, aws_access_key_id=aaki, aws_secret_access_key=asak, security_token=st, **kwargs)
    else:
        # Connect via IAM or other means:
        conn = func(*args, **kwargs)
    return conn


# General purpose function to create an instance:
def createInstance(ami_id=None, instance_type=None, target_env=None,
                   ssh_user='ec2-user', id_rsa=None, key_name=None,
                   tags_dict={}, security_group_ids=None, instance_profile=None,
                   debug=False, subnet_id=None):
    say('Creating Instance in ENV: {0}'.format(target_env), banner="*")

    if subnet_id is None:
        subnet_id = random.choice(g_env_map['environments'][target_env]['vpcsubnet'])['id']
    cmd = 'aws ec2 run-instances --region ' + g_env_map['environments'][target_env]['region'] + \
          ' --image-id ' + ami_id + \
          ' --key-name ' + key_name + \
          ' --placement Tenancy=default' + \
          ' --instance-type ' + instance_type + \
          ' --subnet-id ' + subnet_id + \
          ' --security-group-ids ' + security_group_ids + \
          ' --iam-instance-profile ' + instance_profile

    output, returncode = run(cmd, debug=debug, retry_count=3)
    instance_id = json.loads(output)['Instances'][0]['InstanceId']
    say('Instance is being created: ' + instance_id)

    tags_option = ''
    if len(tags_dict) != 0:
        tags_option = ' --tags'
        for k, v in tags_dict.items():
            tags_option += ' Key={0},Value={1}'.format(k, v)

    # Add Tags to instance
    cmd = 'aws ec2 create-tags --region ' + g_env_map['environments'][target_env]['region'] + \
          ' --resources ' + str(instance_id) + tags_option
    output, returncode = run(cmd, debug=debug, retry_count=3)

    # Wait up to 5 min for instance to be ready:
    cmd = 'aws ec2 describe-instance-status --region ' + g_env_map['environments'][target_env]['region'] + \
          ' --instance-ids ' + str(instance_id)
    bInstanceReady = False
    loop_counter = 120
    say('Waiting for instance to be ready...')
    for i in range(loop_counter):
        output, returncode = run(cmd, debug=debug, retry_count=3)
        j = json.loads(output)
        if len(j['InstanceStatuses']) == 0:
            current_state = 'UNKNOWN'
            current_system_status = 'UNKNOWN'
            current_instance_status = 'UNKNOWN'
        else:
            current_state = str(j['InstanceStatuses'][0]['InstanceState']['Name'])
            current_system_status = str(j['InstanceStatuses'][0]['SystemStatus']['Status'])
            current_instance_status = str(j['InstanceStatuses'][0]['InstanceStatus']['Status'])
        if current_state == 'running' and current_system_status == 'ok' and current_instance_status == 'ok':
            say('Instance is running and status is good!')
            bInstanceReady = True
            break
        else:
            say('Instance is not ready yet. {}/{}. Sleeping 5s. System status: {}, state: {}, instance status: {}'.format(i, loop_counter, current_system_status, current_state, current_instance_status))
        time.sleep(5)
    if bInstanceReady is False:
        say('***Error: We waited {0} seconds for instance to be ready and its not.'.format(loop_counter * 5))
        raise CreateInstanceException("Instance_Not_Started", instance_id=str(instance_id))

    # Wait for ssh. First get the IP address:
    cmd = 'aws ec2 describe-instances --region ' + g_env_map['environments'][target_env]['region'] +\
          ' --instance-ids ' + str(instance_id)
    output, returncode = run(cmd, debug=debug, retry_count=3)
    # Now that the instance is up and runing and has a public-ip, that is what
    # we want to return:
    instance = json.loads(output)['Reservations'][0]['Instances'][0]
    ip_addresses = []
    ip_address_used = None
    if 'PrivateIpAddress' in instance:
        ip_addresses.append(instance['PrivateIpAddress'])
    if 'PublicIpAddress' in instance:
        ip_addresses.append(instance['PublicIpAddress'])

    identity_file = '' if id_rsa is None else '-i {0}'.format(id_rsa)
    ssh_cmd = 'ssh {0} -o ControlMaster=no -o ConnectTimeout=30 -t -n -o PreferredAuthentications=publickey -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'.format(identity_file)

    loop_counter = 30
    bInstanceSshReady = False
    for i in range(loop_counter):
        say('Waiting for ssh to work: ' + str(i) + '/' + str(loop_counter))
        for ip in ip_addresses:
            cmd = '{0} {1}@{2} \'echo hello world\''.format(ssh_cmd, ssh_user, ip)
            output, returncode = run(cmd, raise_on_failure=False, debug=debug)
            if returncode == 0:
                bInstanceSshReady = True
                ip_address_used = ip
                break
        if bInstanceSshReady is True:
            break
        else:
            time.sleep(10)

    if bInstanceSshReady is False:
        say('***Error: We waited 5 min for instance to be ssh-able and its not.')
        raise CreateInstanceException("Instance_not_ssh_able", instance_id=str(instance_id))

    say('Instance is ready!')
    # return the complete instance json:
    return instance
