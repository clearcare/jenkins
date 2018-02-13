import os
import sys
import subprocess
import json
import argparse
import random
import datetime
import dateutil.parser
import time
import glob
import hashlib
import base64
import tempfile
# Python 2 vs python 3:
try:
    import urllib.request as urllib2
except ImportError:
    import urllib2
import csv
import traceback
import ssl
import boto
from boto.sqs.message import Message
import boto.sqs
import boto.ec2

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from common import *


# Valid Labels:
# [ENV]-[REGION] (implys t2.small)
# [ENV]-[REGION]_[INSTANCE_TYPE]
# [ENV]-[REGION]_shared (implys t2.small)
# [ENV]-[REGION]_shared_[INSTANCE_TYPE]
# [ENV]-[REGION]_spot (implys t2.small)
# [ENV]-[REGION]_spot_[INSTANCE_TYPE]

# Special labels to indicate special executor types:
g_label_map = {'shared': {'num_of_executors': 5}, 'spot': {'num_of_executors': 1}}

g_jenkins_instance_types = ['t2.small', 't2.medium', 't2.large',
                            'm3.medium', 'm3.large', 'm3.xlarge',
                            'm4.large', 'c3.large', 'c4.large', 'c4.xlarge',
                            'c4.2xlarge', 'c3.2xlarge', 'm4.2xlarge', 'm3.2xlarge']
# Valid Node Labels:
g_valid_labels = set(list(g_env_map['environments'].keys()) + [env + '_' + label for env in list(g_env_map['environments'].keys()) for label in list(g_label_map.keys())])
g_valid_labels = set(list(g_valid_labels) + [l + '_' + t for l in list(g_valid_labels) for t in g_jenkins_instance_types])

# The stats of what happened in a run:
g_instance_stats = {'instances_created': 0, 'instances_started': 0, 'instances_terminated': 0,
                    'instances_stopped': 0}

g_error_stats = {'instance_stopped_state': 0, 'job_on_queue': 0, 'instance_not_slave': 0,
                 'jobs_waited_too_long': 0, 'jar_file_error': 0, 'error_stopping_instance': 0}

g_sqs_stats = {'sqs_handled': 0, 'sqs_dropped': 0, }

# Current instance count:
g_instance_count = dict.fromkeys(g_env_map['environments'].keys(), set())
g_spot_instance_count = dict.fromkeys(g_env_map['environments'].keys(), set())

# Current instance count based on shared or not shared:
g_instance_details = {}

# The original, unsullied env vars:
g_os_environ_orig = dict(os.environ)

# Instances that we have set termination policy (so we don't do it again)
g_termination_policy = dict.fromkeys(g_env_map['environments'].keys(), [])

# Instances that we have checked its AMI-ID (so we don't do it again)
g_old_ami_check = dict.fromkeys(g_env_map['environments'].keys(), [])


# Dynamically generate the user-data script:
def generateDataTag(target_env=None, labels_string=None):
    num_of_executors = 1
    # Look for shared and deploy keywords:
    label_list = [str(labels_string).strip()]
    for label in label_list:
        for sub_label in label.split('_'):
            if sub_label in g_label_map.keys():
                if 'num_of_executors' in g_label_map[sub_label].keys():
                    num_of_executors = g_label_map[sub_label]['num_of_executors']
                    break
    context = {
        'num_of_executors': num_of_executors,
        'environment': 'infra',
        'slave_labels': ' '.join(label_list),
        'description': 'Created by Swarm',
        'jenkins_url': g_env_map['environments'][target_env]['jenkins_url']
    }
    return json.dumps(context, ensure_ascii=True)


# Switch Environments via STS:
def generateStsCredentials(target_env=None, session_name=None, account_id=None):
    say('Assuming the role of {} in {}...'.format(g_env_map['jenkins-master']['jenkins-master-sg-name'], target_env))

    cmd = ('aws sts assume-role --role-arn "arn:aws:iam::{}:role/{}" '
           '--role-session-name {} --region {}').format(account_id,
                                                        g_env_map['jenkins-master']['jenkins-master-sg-name'],
                                                        session_name,
                                                        g_env_map['environments'][target_env]['region'])
    output, returncode = run(cmd, retry_count=3)
    j = json.loads(output)
    SecretAccessKey = j['Credentials']['SecretAccessKey']
    SessionToken = j['Credentials']['SessionToken']
    AccessKeyId = j['Credentials']['AccessKeyId']

    return AccessKeyId, SecretAccessKey, SessionToken


# Switch Environments via STS:
def switchEnvironments(target_env=None, session_name=None, account_id=None):
    say('Getting pre-cached STS credentials for env: {}...'.format(target_env))
    # Create the env vars needed by the aws cli commands:
    os.environ['AWS_ACCESS_KEY_ID'] = g_env_map['environments'][str(target_env)]['AccessKeyId']
    os.environ['AWS_SECRET_ACCESS_KEY'] = g_env_map['environments'][str(target_env)]['SecretAccessKey']
    os.environ['AWS_SESSION_TOKEN'] = g_env_map['environments'][str(target_env)]['SessionToken']


# Create a spot instance:
def createSpotInstance(target_env=None, job_name=None, labels_string=None, slave_name=None, owner_email=None):
    say('Creating Spot Instance in ENV: {}'.format(target_env), banner='+')
    # Use the correct AMI:
    ami_id = g_env_map['environments'][target_env]['ami_id']
    if ami_id == 'UNKNOWN':
        say('ami_id is UNKNOWN. Not creating instance.')
        return

    account_id = g_env_map['environments'][target_env]['account-id']
    os_environ_orig = dict(os.environ)
    # We need to Assume the Role in the target env to set the tags there:
    switchEnvironments(target_env=str(target_env), session_name='run-an-instance', account_id=account_id)
    data_json = generateDataTag(target_env=target_env, labels_string=labels_string)

    # Get the instance_type:
    instance_type = getInstanceTypeFromLabelString(labels_string=labels_string)

    # Generate a launch-specification json file:
    launch_specification = {
        "ImageId": ami_id,
        "KeyName": "jenkins.cloud",
        "InstanceType": instance_type,
        "BlockDeviceMappings": [{
            "DeviceName": "/dev/xvda",
            "Ebs": {
                "VolumeSize": 25,
                "DeleteOnTermination": True,
                "VolumeType": "standard"
            }
        }],
        "NetworkInterfaces": [{
            "DeviceIndex": 0,
            "SubnetId": random.choice(g_env_map['environments'][target_env]['vpcsubnet'])['id'],
            "Groups": [g_env_map['environments'][target_env]['jenkins-sg']],
            "AssociatePublicIpAddress": True
        }],
        "IamInstanceProfile": {
            "Arn": 'arn:aws:iam::' + account_id + ':instance-profile/' + g_env_map['environments'][target_env]['instance-profile']
        }
    }
    os_fd, file_name = tempfile.mkstemp(prefix='temp_spot_', suffix='.json', text=True)
    os.close(os_fd)
    with open(file_name, 'w') as fd:
        json.dump(launch_specification, fd)
    if args.debug is True:
        with open(file_name, 'r') as fd:
            say(fd.read())

    # Request a spot instance:
    cmd = ('aws ec2 request-spot-instances --spot-price "{}" --instance-count 1 '
           '--type "one-time" --region {} --launch-specification file://{}').format(args.max_spot_price, g_env_map['environments'][target_env]['region'], file_name)
    output, returncode = run(cmd, retry_count=3)
    os.remove(file_name)
    j = json.loads(output)
    spotInstanceRequestId = j['SpotInstanceRequests'][0]['SpotInstanceRequestId']

    # Wait till we get an instance_id:
    instance_id = None
    for i in range(10):
        cmd = ('aws ec2 describe-spot-instance-requests --region {} '
               '--spot-instance-request-ids {}').format(g_env_map['environments'][target_env]['region'], spotInstanceRequestId)
        output, returncode = run(cmd, retry_count=3, hide_command=args.debug is False)
        j = json.loads(output)
        if 'InstanceId' in j['SpotInstanceRequests'][0]:
            instance_id = j['SpotInstanceRequests'][0]['InstanceId']
            break
        say('Waiting for spot intance to be available...', do_print=args.debug)
        time.sleep(6)
    if instance_id is None:
        say('Could not get instance-id after 1m. Terminating the spot instance request: {}'.format(spotInstanceRequestId))
        cmd = 'aws ec2 cancel-spot-instance-requests --spot-instance-request-ids {} --region {}'.format(spotInstanceRequestId, g_env_map['environments'][target_env]['region'])
        output, returncode = run(cmd, retry_count=3)
        return False

    instance = None
    for i in range(10):
        try:
            # Add the tags (We need to use boto, since cli does not handle json values very well)
            conn = connect(boto.ec2.connect_to_region, g_env_map['environments'][target_env]['region'])
            reservations = conn.get_all_instances(instance_ids=instance_id)
            instance = reservations[0].instances[0]
            break
        except boto.exception.EC2ResponseError:
            pass
        time.sleep(1)
    if instance is None:
        say('Could not get instance after 10s. Terminating the spot instance request: {}'.format(spotInstanceRequestId))
        cmd = 'aws ec2 cancel-spot-instance-requests --spot-instance-request-ids {} --region {}'.format(spotInstanceRequestId, g_env_map['environments'][target_env]['region'])
        output, returncode = run(cmd, retry_count=3)
        return False

    instance.add_tag('Name', slave_name)
    instance.add_tag('slave_data', data_json)
    instance.add_tag('owner', owner_email)
    instance.add_tag('environment', 'infra')
    instance.add_tag('role', slave_name)
    instance.add_tag('is_spot', 'true')
    instance.add_tag('is_asg', 'false')
    instance.add_tag('jumpcloud_tags', 'admin,superadmin')

    g_spot_instance_count[str(target_env)].add(str(instance_id))
    g_instance_stats['instances_created'] += 1
    # Reset the env vars:
    os.environ.clear()
    os.environ.update(os_environ_orig)
    # Write a file on disk to indicate we just started this instance:
    label_list = [str(labels_string).strip()]

    with open('{}__{}.start_instance'.format(target_env, instance_id), 'w') as fd:
        fd.write('labels=' + ','.join(label_list) + '\n')
        fd.write('instance_id={}\n'.format(instance_id))
    return True


# Helper function to get Instance Type from label string:
def getInstanceTypeFromLabelString(labels_string):
    # Get the instance_type:
    instance_type = 't2.small'
    label_list = [str(labels_string).strip()]
    for label in label_list:
        for sub_label in label.split('_'):
            if sub_label in g_jenkins_instance_types:
                instance_type = sub_label
                break
    return instance_type


# Create a single instance:
def createInstance(target_env=None, job_name=None, labels_string=None, slave_name=None, owner_email=None):
    say('Creating Instance in ENV: {}'.format(target_env), banner='+')
    # Use the correct AMI:
    ami_id = g_env_map['environments'][target_env]['ami_id']
    if ami_id == 'UNKNOWN':
        say('ami_id is UNKNOWN. Not creating instance.')
        return

    account_id = g_env_map['environments'][target_env]['account-id']
    os_environ_orig = dict(os.environ)
    # We need to Assume the Role in the target env to set the tags there:
    switchEnvironments(target_env=str(target_env), session_name='run-an-instance', account_id=account_id)
    data_json = generateDataTag(target_env=target_env, labels_string=labels_string)

    # Connect to ec2:
    conn = connect(boto.ec2.connect_to_region, g_env_map['environments'][target_env]['region'])

    # Create blockDeviceMapping for use in 'run_instances"
    dev_sda1 = boto.ec2.blockdevicemapping.EBSBlockDeviceType()
    # Size in Gigabytes
    dev_sda1.size = 25
    bdm = boto.ec2.blockdevicemapping.BlockDeviceMapping()
    bdm['/dev/xvda'] = dev_sda1
    # Get the instance_type:
    instance_type = getInstanceTypeFromLabelString(labels_string=labels_string)

    # Create the new instance, and assign the output returned to "output"
    instance_profile_arn = 'arn:aws:iam::{}:instance-profile/{}'.format(account_id,
                                                                        g_env_map['environments'][target_env]['instance-profile'])
    output = conn.run_instances(image_id=ami_id,
                                key_name='jenkins.cloud',
                                instance_type=instance_type,
                                subnet_id=random.choice(g_env_map['environments'][target_env]['vpcsubnet'])['id'],
                                block_device_map=bdm,
                                security_group_ids=[g_env_map['environments'][target_env]['jenkins-sg']],
                                instance_profile_arn=instance_profile_arn)
    # Pull instance id out of the returned output
    instance_id = output.instances[0].id
    say('Instance is being created and tags are being added: {}'.format(instance_id))

    # Sometimes the instance doesn't quite exist yet:
    time.sleep(5)
    output.instances[0].add_tag('Name', slave_name)
    output.instances[0].add_tag('slave_data', data_json)
    output.instances[0].add_tag('owner', owner_email)
    output.instances[0].add_tag('environment', 'infra')
    output.instances[0].add_tag('role', slave_name)
    output.instances[0].add_tag('is_spot', 'false')
    output.instances[0].add_tag('is_asg', 'false')
    output.instances[0].add_tag('jumpcloud_tags', 'admin,superadmin')

    say('adding0 {} to g_instance_count'.format(instance_id), do_print=args.debug)
    g_instance_count[str(target_env)].add(str(instance_id))
    g_instance_stats['instances_created'] += 1
    # Reset the env vars:
    os.environ.clear()
    os.environ.update(os_environ_orig)
    # Write a file on disk to indicate we just started this instance:
    label_list = [str(labels_string).strip()]
    with open('{}__{}.start_instance'.format(target_env, instance_id), 'w') as fd:
        fd.write('labels=' + ','.join(label_list) + '\n')
        fd.write('instance_id={}\n'.format(instance_id))


# Start a stopped intance:
def startInstance(target_env=None, ip_preference=None, label_set=None, slave_name=None, jenkins_queue=None, job_name=None):
    # Function returns: True|False (if we started an instance)
    # Get the existing env vars:
    account_id = g_env_map['environments'][target_env]['account-id']
    os_environ_orig = dict(os.environ)
    # We need to Assume the Role in the target env to set the tags there:
    switchEnvironments(target_env=str(target_env), session_name='stop-an-instance', account_id=account_id)

    # Get a list of all non-terminated instances:
    cmd = ('aws ec2 describe-instances --region {}'
           ' --filters "Name=instance-state-name,Values=[stopped,stopping,pending,running]"').format(g_env_map['environments'][target_env]['region'])
    output, returncode = run(cmd, retry_count=3)
    j = json.loads(output)

    stopped_instances = []
    shared_pending_instances = []
    soon_to_be_off_queue = []
    # Read any "start_instance" files to see if we are recently starting one up
    # (tags on recently started instances do not exist...) (we call str because InstanceId is unicode)
    all_instance_ids_in_env = map(str, [i['InstanceId'] for r in j['Reservations'] for i in r['Instances']])
    recently_started_instances = {}  # Key is intance_id, Value is list of labels.
    for fname in glob.glob('{}__*.start_instance'.format(target_env)):
        # Read the labels on this instance:
        instance_id = None
        starting_instance_labels = None
        with open(fname, 'r') as fd:
            for line in fd.readlines():
                if line.startswith('labels='):
                    starting_instance_labels = line[len('labels='):].strip().split(',')
                if line.startswith('instance_id='):
                    instance_id = line[len('instance_id='):].strip()
        if instance_id not in all_instance_ids_in_env:
            # Delete the file on disk. It's an old file:
            say('Instance ID: {} obtained from file: {} does not exist. Deleting file.'.format(instance_id, fname))
            os.remove(fname)
        else:
            recently_started_instances[instance_id] = starting_instance_labels

    # Now lets go through all instances to see if can start a stopped one or wait for one
    # that is already starting.
    for r in j['Reservations']:
        for i in r['Instances']:
            # Get the Name and slave_labels of the instance:
            instance_label_set = set()
            instance_id = str(i['InstanceId'])
            tags = getTags(i)
            instance_name = tags.get('Name')
            is_asg = True if tags.get('is_asg') == 'true' else False
            try:
                j = json.loads(tags.get('slave_data'))
                instance_label_set = set(map(str, j['slave_labels'].split(' ')))
            except (ValueError, TypeError):
                say('Could not parse slave_data json from instance {}. slave_data: {}'.format(instance_id,
                                                                                              tags.get('slave_data')), do_print=args.debug)

            # If this instance is in the list of recently created instances,
            # and this instance has the tags, lets delete the file and remove it from the dict.
            # (It is now detectable via its tags)
            if str(instance_id) in recently_started_instances.keys():
                if instance_name == slave_name:
                    # We already have the name and instance_id, so if it has labels,
                    # we don't care what they are as long it has them:
                    if len(instance_label_set) != 0:
                        fname = '{}__{}.start_instance'.format(target_env, instance_id)
                        say('This recently created instance, {} is now detectable via tags. Deleting file: {}'.format(instance_id, fname))
                        del recently_started_instances[instance_id]
                        if os.path.exists(fname):
                            os.remove(fname)

            # Now update the global counter:
            # Both recently_started_instances[k] and instance_label_set have value like this:
            # eod-us-west-2_spot_c4.xlarge
            for k in recently_started_instances.keys():
                if not any(['spot' in l for l in recently_started_instances[k]]):
                    g_instance_count[str(target_env)].add(k)
                else:
                    g_spot_instance_count[str(target_env)].add(k)
            if instance_name == slave_name and is_asg is False:
                if not any(['spot' in l for l in instance_label_set]):
                    g_instance_count[str(target_env)].add(instance_id)
                else:
                    g_spot_instance_count[str(target_env)].add(instance_id)

            # OK. Now lets examine this instance:
            if i['State']['Name'] != 'stopping':
                # Filter out instances that do not have the necessary labels:
                # If the instance's slave_labels Tag contains the job tag, we can use this instance:
                bIsLabelSubset = (instance_name == slave_name and label_set.issubset(instance_label_set))
                bIsInstanceRecentlyStarted = instance_id in recently_started_instances.keys()
                bIsLabelRecentlyStarted = False
                if instance_id in recently_started_instances:
                    bIsLabelRecentlyStarted = label_set.issubset(set(recently_started_instances[instance_id]))

                if bIsLabelSubset is True or (bIsInstanceRecentlyStarted is True and bIsLabelRecentlyStarted is True):
                    say('Found instance, {}, that has jobs labels.  Checking if pending. Current state: {}'.format(instance_id,
                                                                                                                   i['State']['Name']))
                    if instance_id in recently_started_instances.keys():
                        say('This is a running instance just created. It may or may not be a slave yet.')
                    if i['State']['Name'] == 'stopped':
                        if instance_id in recently_started_instances.keys():
                            say('***Error: Recently started instance is in stopped state: {}'.format(instance_id))
                            g_error_stats['instance_stopped_state'] += 1
                        stopped_instances.append(i)
                    else:
                        # This is a tricky scenario, but it happens often:
                        # job1-needs-shared-dev gets put on the queue and this script starts or creates an instance.
                        # instance is running but not connected to master yet.
                        # job2-needs-shared-dev gets put on the queue.
                        # We should not start or create an instance in this scenario.
                        # BUT: if we have an instance that is running AND is a slave BUT is completely busy,
                        # lets start|create a new one.
                        # Final scenario: Sometimes a slave exists, has the executors, but for whatever reason the job
                        # is still on the queue. The groovy script should not have put it in the json to begin with.
                        if i['State']['Name'] == 'running' or i['State']['Name'] == 'pending':
                            # Check to see if the label_set of the queue item is a special "shared" label:
                            if any([label.endswith(special_tag) for label in list(label_set) for special_tag in g_label_map.keys()]):
                                # If the shared instance is running AND is already a slave AND all the exectors are full,
                                # then this is completely busy slave and is not "pending".
                                if i['State']['Name'] == 'running':
                                    # Check if this instance is registered slave:
                                    say('Instance is running. PrivateIpAddress: {}. Scanning slaves...'.format(i['PrivateIpAddress']))
                                    bIsSlave = False
                                    for slave in jenkins_queue['slave_queue']:
                                        slave_labels_list = map(str.strip, map(str, slave['labels'].strip('[').strip(']').split(',')))
                                        say('slave_labels_list: {}'.format(slave_labels_list))
                                        if any(label.endswith(str(i['PrivateIpAddress'])) for label in slave_labels_list):
                                            say('This instance is a slave! Slave ope_idle_count: {}'.format(slave['ope_idle_count']))
                                            bIsSlave = True
                                            if slave['ope_idle_count'] != '0' and slave['isOffLine'] == 'false':
                                                s = ('WTF. Instance has free executors and is online! ',
                                                     'This job should not have been on our list to begin with: {}').format(job_name)
                                                say(s)
                                                soon_to_be_off_queue.append(i)
                                            break
                                    if bIsSlave is False:
                                        say('Wierd. Instance is running, but is not a slave.')
                                else:
                                    # Instance must be pending:
                                    # This is pending/shared instance that is about to become a slave that this job will able to run on:
                                    say('This is pending/shared instance that this job will be able to run on...')
                                    shared_pending_instances.append(i)

    if len(soon_to_be_off_queue) != 0:
        # There is an edge case where a job is on the queue and there is a free executor for it.
        # Lets not create an instance for this.  We should be trapping this in the groovy script,
        # but I have not been able to reproduce this reliably.
        say('***Error: This job should not even be on the queue: {}'.format(job_name))
        g_error_stats['job_on_queue'] += 1
        # Reset the env vars:
        os.environ.clear()
        os.environ.update(os_environ_orig)
        # Return true so that we don't create a brand new instance:
        return True

    if len(shared_pending_instances) != 0:
        say('There are pending/shared instances that this job will run on. No instances should be created or started.')
        # Reset the env vars:
        os.environ.clear()
        os.environ.update(os_environ_orig)
        # Return true so that we don't create a brand new instance:
        return True

    if len(stopped_instances) == 0:
        say('There are no stopped instances to start up.  If we did not hit a limit, we will have to create brand new instance.')
        # Reset the env vars:
        os.environ.clear()
        os.environ.update(os_environ_orig)
        # Return false so that we attempt to create a brand new instance:
        return False

    # We need to start a stopped instance:
    say('We need to start a stopped instance...')
    instance = None
    if ip_preference is None:
        # Randomly start an instance:
        say('Last IP address is unknown. Randomly starting a stopped instance...')
        instance = random.choice(stopped_instances)
    else:
        for i in stopped_instances:
            if 'PrivateIpAddress' in i.keys():
                if str(i['PrivateIpAddress']) == str(ip_preference):
                    # Found the last used instance!
                    say('Found the last instance we ran on! Starting it up: '.format(ip_preference))
                    instance = i
                    break
        if instance is None:
            # Sigh. Could not find last instance used. Randomly select one:
            say('Last instance we ran on not found.  Starting up a random one: '.format(ip_preference))
            instance = random.choice(stopped_instances)

    say('Starting this instance: {}'.format(instance['InstanceId']), banner='>')
    cmd = ('aws ec2 start-instances --region {}'
           ' --instance-ids {}').format(g_env_map['environments'][target_env]['region'], instance['InstanceId'])
    output, returncode = run(cmd, retry_count=3)
    say(output)
    g_instance_stats['instances_started'] += 1

    # Write a file on disk to indicate we just started this instance:
    with open('{}__{}.start_instance'.format(target_env, instance_id), 'w') as fd:
        fd.write('labels=' + ','.join(list(label_set)) + '\n')
        fd.write('instance_id={}\n'.format(instance['InstanceId']))

    # We started an instance. Reset the env vars:
    os.environ.clear()
    os.environ.update(os_environ_orig)
    # Return true so that we don't create a brand new instance:
    return True


# Get env from label:
def getEnvStringFromLabelSet(labels_set=None):
    say('Getting env label from the set: {}'.format(labels_set))
    # If the user specified labels: (dev, foo), get 'dev':
    if len(labels_set & g_valid_labels) == 0:
        say('These labels, {}, are not a valid label.'.format(labels_set))
        return None
    # If the user specified labels: (ENV_shared), get 'ENV':
    env_label = labels_set & set(g_env_map['environments'].keys())
    if len(env_label) == 0:
        for item_label in list(labels_set):
            for i in item_label.split('_'):
                env_label = set([i]) & set(g_env_map['environments'].keys())
                if len(env_label) == 1:
                    return list(env_label)[0]
    elif len(env_label) == 1:
        return list(env_label)[0]
    return None


# Create or Start any needed slaves:
def createOrStartSlaves(jenkins_queue, max_spot_slaves, max_slaves, slave_name, owner_email):
    # [u'slave_queue', u'build_queue', u'messages']
    # Look at the build_queue for anything we need to create:
    for item in jenkins_queue['build_queue']:
        # TODO: Handle ||.
        try:
            p = str(item['parameters'].encode('ascii', 'replace').strip().replace(' ', '_'))
            hash_params = hashlib.md5(p).hexdigest()
        except UnicodeEncodeError:
            say('Caught UnicodeEncodeError Exception in parameters to build.')
            print(item['parameters'])
            continue
        job_name = str(item['jobName']).replace('/', '_CHILD_') + '___' + hash_params
        if len(job_name) > (255 - len('.working') - len('working_on_')):
            say('job_name too long: {}'.format(job_name))
            job_name = job_name[:255 - len('.working') - len('working_on_')]
        # Only create/start a new instance if we are not already working on it:
        say('We may need to create a slave of type: {}. For this job: {}'.format(item['labels'], job_name))
        bWorkingOn = False
        for fname in glob.glob('*.working'):
            working_job_name = fname[len('working_on_'):-len('.working')]
            if job_name == working_job_name:
                say('We are already working on: {}'.format(job_name))
                bWorkingOn = True
                break
        if bWorkingOn is False:
            # We only know how to create certain types of slaves: $ENV, $ENV_[shared||spot]
            item_labels = set([str(item['labels']).strip()])
            # If the user specified labels: (dev, foo), get 'dev':
            env_label = getEnvStringFromLabelSet(labels_set=item_labels)
            say('Env from this label set: {} is: {}'.format(item_labels, env_label))
            # Only handle valid/known ENV labels:
            if env_label is not None:
                # Check if an AWS Node exists that is stopped that can be turned on:
                target_env = env_label
                private_ip_address = None
                if item['lastBuiltOn'] != 'UNKNOWN':
                    private_ip_address = item['lastBuiltOn'][len(target_env) + 1:]
                bDidStartInstance = startInstance(target_env=target_env,
                                                  ip_preference=private_ip_address,
                                                  label_set=item_labels,
                                                  slave_name=slave_name,
                                                  jenkins_queue=jenkins_queue,
                                                  job_name=job_name)
                say('Total number of regular slaves in env: {} : {}/{}'.format(target_env,
                                                                               len(g_instance_count[str(target_env)]),
                                                                               max_slaves))
                say('Regular Slaves: {}'.format(g_instance_count), do_print=args.debug)
                say('Total number of spot slaves in env   : {} : {}/{}'.format(target_env,
                                                                               len(g_spot_instance_count[str(target_env)]),
                                                                               max_spot_slaves))
                say('Spot Slaves: {}'.format(g_spot_instance_count), do_print=args.debug)
                bDidCreateInstance = False
                if bDidStartInstance is False:
                    current_slave_count = len(g_instance_count[str(target_env)])
                    max_allows_slaves = max_slaves
                    isSpot = False
                    if 'spot' in item['labels']:
                        isSpot = True
                        max_allows_slaves = max_spot_slaves
                        current_slave_count = len(g_spot_instance_count[str(target_env)])

                    # Create an instance:
                    if current_slave_count < max_allows_slaves:
                        # TODO: We need a better way of determining if spot is a label:
                        if isSpot is True:
                            bsuccess = createSpotInstance(target_env=target_env, job_name=job_name, labels_string=item['labels'],
                                                          slave_name=slave_name, owner_email=owner_email)
                            if bsuccess is False:
                                return
                        else:
                            createInstance(target_env=target_env, job_name=job_name, labels_string=item['labels'],
                                           slave_name=slave_name, owner_email=owner_email)
                        bDidCreateInstance = True
                    else:
                        say('Sorry, we have reached the max number of slaves: {} in: {}'.format(max_slaves, item['labels']))
                        say('Attempting to terminate a randomly stopped instance...')
                        # Search for any stopped instances and kill them:
                        cmd = ('aws ec2 describe-instances --region {}'
                               ' --filters "Name=tag:Name,Values={}" '
                               '"Name=instance-state-name,Values=[stopped]"').format(g_env_map['environments'][env]['region'], slave_name)
                        output, returncode = run(cmd, retry_count=3)
                        j = json.loads(output)
                        stopped_instances = [i['InstanceId'] for r in j['Reservations'] for i in r['Instances']]
                        if len(stopped_instances) == 0:
                            say('Sorry again, no stopped instances to termiante.')
                            say('You will have to wait your turn, or increate number of allowable slaves in this env.')
                        else:
                            terminate_instance(instance_id=random.choice(stopped_instances), region=g_env_map['environments'][env]['region'])

                if bDidCreateInstance is True or bDidStartInstance is True:
                    # Create a file to indicate this queue item has been handled:
                    with open('working_on_{}.working'.format(job_name), 'w') as fd:
                        fd.write('')
            else:
                say('I do not know how to create a slave for this job: {} with labels: {}'.format(item['jobName'], item['labels']))

    # Remove any working_on_ files if job is off the queue or we waited too long:
    for fname in glob.glob('*.working'):
        working_job_name = fname[len('working_on_'):-1 * len('.working')]
        bOffQueue = True
        for item in jenkins_queue['build_queue']:
            p = str(item['parameters'].encode('ascii', 'replace').strip().replace(' ', '_'))
            hash_params = hashlib.md5(p).hexdigest()
            job_name = str(item['jobName']).replace('/', '_CHILD_') + '___' + hash_params
            if working_job_name == job_name:
                bOffQueue = False
                break
        if bOffQueue is True:
            say('The following job is off the queue: ' + working_job_name)
            say('Deleting the working on file: ' + fname)
            os.remove(fname)
        else:
            seconds = 60 * 5
            seconds_working = time.time() - os.path.getctime(fname)
            say('We have been working on {}, for the following seconds: {}/{}'.format(fname, round(seconds_working), seconds))
            # Delete any working on files that have been around too long:

            if seconds_working > seconds:
                say('We have been working on this file for too long (over ' + str(seconds) + ' seconds). Deleting this file and trying again: ' + str(fname))
                g_error_stats['jobs_waited_too_long'] += 1
                os.remove(fname)


# Stop an instance:
def stopInstance(instance_id, target_env=None):
    say('Stopping Instance in ENV: ' + str(target_env), banner='<')
    # Get the existing env vars:
    account_id = g_env_map['environments'][target_env]['account-id']
    os_environ_orig = dict(os.environ)
    # We need to Assume the Role in the target env to set the tags there:
    switchEnvironments(target_env=str(target_env), session_name='stop-an-instance', account_id=account_id)

    # Rather than stopping this instance, lets see if we can just terminate it right now:
    bDidTerminateInstance = terminateOldAmiInstance(instance_id=instance_id, environment=target_env)

    if bDidTerminateInstance is False:
        # Only stop this instance if it is "running":
        cmd = 'aws ec2 describe-instances --region {} --instance-ids {}'.format(g_env_map['environments'][target_env]['region'], instance_id)
        output, returncode = run(cmd, hide_command=True, retry_count=3)
        instance_state = json.loads(output)['Reservations'][0]['Instances'][0]['State']['Name']
        if instance_state == 'running':
            cmd = 'aws ec2 stop-instances --region {} --instance-ids {}'.format(g_env_map['environments'][target_env]['region'], instance_id)
            output, returncode = run(cmd, hide_command=True, retry_count=3)
            say(output)
            g_instance_stats['instances_stopped'] += 1
    else:
        say('Instance was not stopped; It was terminated because it was created from an old ami-id.')

    # Reset the env vars:
    os.environ.clear()
    os.environ.update(os_environ_orig)
    say('<<<<-Done trying to stop instance.')


# Set Termination Policy on an Instance:
def setTerminationPolicy(instance=None, region=None, environment=None):
    # Only set the termination policy if we have never done it before:
    if str(instance['InstanceId']) not in g_termination_policy[environment]:
        # Before terminating, make sure all block devices are set to DeleteOnTermination is true:
        for block_device in instance['BlockDeviceMappings']:
            if 'Ebs' in block_device.keys():
                if block_device['Ebs']['DeleteOnTermination'] is False:
                    # Flip the bit to true:
                    device_name = block_device['DeviceName']
                    say('InstanceId of instance to modify: {}'.format(instance['InstanceId']), do_print=args.debug)
                    say('block_device: \n{}'.format(block_device), do_print=args.debug)
                    say(device_name, do_print=args.debug)
                    cmd_modify = ('aws ec2 modify-instance-attribute'
                                  ' --region {}'
                                  ' --instance-id {}'
                                  ' --block-device-mappings '
                                  '\'[{{"DeviceName": "{}","Ebs":{{"DeleteOnTermination":true}}}}]\'').format(region,
                                                                                                              instance['InstanceId'],
                                                                                                              device_name)
                    output, returncode = run(cmd_modify, hide_command=True, retry_count=3)
                    # Wait for it to stick:
                    bHasStuck = False
                    say('Waiting for termination policy to stick...')
                    for i in range(30):
                        cmd = ('aws ec2 describe-instance-attribute --instance-id {}'
                               ' --attribute blockDeviceMapping --region {}').format(instance['InstanceId'], region)
                        output, returncode = run(cmd, hide_command=True, retry_count=3)
                        # We have to loop (again) to find the block device that we just set :(
                        say('InstanceId of instance: {}'.format(instance['InstanceId']), do_print=args.debug)
                        for bd in json.loads(output)['BlockDeviceMappings']:
                            if bd['DeviceName'] == device_name:
                                if bd['Ebs']['DeleteOnTermination'] is True:
                                    bHasStuck = True
                                    break
                        if bHasStuck is True:
                            say('DeleteOnTermination has stuck! Ready for instance termination!')
                            break
                        else:
                            say('Block device DeleteOnTermination has not stuck yet. Sleeping for 2 seconds...')
                            time.sleep(2)
                    if bHasStuck is False:
                        say('***Error: We set the launch perm, but it did not stick. Error!')
                        sys.exit(1)
        say('All block devices on instance, {}, are set to terminate on deletion.'.format(instance['InstanceId']), do_print=args.debug)
        g_termination_policy[environment].append(str(instance['InstanceId']))


# Terminate an instance by id:
def terminate_instance(instance_id, region):
    say('Terminating instance...', banner='%')
    cmd_terminate = ('aws ec2 terminate-instances --region {}'
                     ' --instance-ids {}').format(region, instance_id)
    output, returncode = run(cmd_terminate, retry_count=3)
    say(output)
    g_instance_stats['instances_terminated'] += 1


# Delete any old instances that does not match the ami that you want:
def terminateOldAmiInstance(instance_id, environment):
    # Get the ami-id of this instance:
    account_id = g_env_map['environments'][environment]['account-id']
    os_environ_orig = dict(os.environ)
    os_environ_orig = dict(os.environ)
    ami_id = g_env_map['environments'][environment]['ami_id']
    bDidTerminateInstance = False

    # Only set the termination policy if we have never done it before:
    if str(instance_id) not in g_old_ami_check[environment]:
        say('Checking to see if we have to terminate this instance: {} in env: {}'.format(instance_id, environment), do_print=args.debug)
        if ami_id == 'UNKNOWN':
            say('ami_id is unknown.  not terminating this instance: {}'.format(instance_id))
            return bDidTerminateInstance

        # Reset the env vars to an unsullied state:
        os.environ.clear()
        os.environ.update(g_os_environ_orig)

        # We need to Assume the Role in the target env to set the tags there:
        switchEnvironments(target_env=str(environment), session_name='terminate-an-instance', account_id=account_id)

        # Examine this instance:
        cmd_describe = ('aws ec2 describe-instances --region {}'
                        ' --instance-ids {}').format(g_env_map['environments'][environment]['region'], instance_id)
        output, returncode = run(cmd_describe, retry_count=3)
        j = json.loads(output)
        instance = j['Reservations'][0]['Instances'][0]
        instance_state = str(instance['State']['Name'])

        # Only terminate it if instance is not termated or terminating:
        if instance_state not in ['shutting-down', 'terminated']:
            if str(instance['ImageId']) != ami_id:
                say('We need to terminate this instance. It is using an old ami.')
                say('ami that this instance is using: {}'.format(instance['ImageId']))
                say('ami that we are supposed to be using: {}'.format(ami_id))
                # Before terminating, make sure all block devices are set to DeleteOnTermination is true:
                setTerminationPolicy(instance=instance, region=g_env_map['environments'][environment]['region'],
                                     environment=environment)

                terminate_instance(instance_id=instance_id, region=g_env_map['environments'][environment]['region'])
                # Remove the instance from the count:
                g_instance_count[str(environment)].discard(str(instance_id))
                bDidTerminateInstance = True
            else:
                say('We do not need to terminate instance, {}, because the AMI is up to date.'.format(instance_id))
                g_old_ami_check[environment].append(str(instance_id))
        else:
            say('This instance,{}, was not terminated because current state is: {}'.format(instance_id, instance_state))
        # Reset the env vars:
        os.environ.clear()
        os.environ.update(os_environ_orig)
    return bDidTerminateInstance


# Stop any idle Slaves:
def stopSlaves(jenkins_queue):
    for slave in jenkins_queue['slave_queue']:
        # Groovy is currently setting the nodes offline. Now tell AWS to Stop these instances:
        # strip any new spaces in labels, and convert unicode to ascii:
        slave_labels_list = map(str.strip, map(str, slave['labels'].strip('[').strip(']').split(',')))

        if slave['isOffLine'] == 'true' and 'swarm' in slave_labels_list and slave['terminate_me'] == 'true':
            say('We need to stop this instance: {}'.format(slave))
            loc_1 = slave['description'].find('InstanceID=') + len('InstanceID=')
            loc_2 = slave['description'].find(' ', loc_1)
            instance_id = slave['description'][loc_1:loc_2]

            env = getEnvStringFromLabelSet(labels_set=set(slave_labels_list))

            if env is None:
                say('***Error: Could not determine which env this slave is for. Not stopping instance.')
                say('g_env_map keys: {}'.format(g_env_map['environments'].keys()))
                say('slave labels  : {}'.format(slave_labels_list))
                g_error_stats['error_stopping_instance'] += 1
            else:
                if any(['spot' in label for label in slave_labels_list]):
                    terminate_instance(instance_id=instance_id, region=g_env_map['environments'][env]['region'])
                else:
                    stopInstance(instance_id, target_env=env)


# Get a list of public IP addresses:
def getAllProdSlaveIPAddress(slave_name=None):
    os_environ_orig = dict(os.environ)
    instance_ips = []
    for env in g_env_map['environments'].keys():
        if 'prod' in env:
            account_id = g_env_map['environments'][env]['account-id']
            # Assume the role in that environment and get the IP address:
            switchEnvironments(target_env=str(env), session_name='get-ip-addresses', account_id=account_id)

            # Get a list of stopped instances:
            cmd = ('aws ec2 describe-instances --region {}'
                   ' --filters "Name=tag:Name,Values={}" '
                   '"Name=instance-state-name,Values=[running]"').format(g_env_map['environments'][env]['region'], slave_name)
            output, returncode = run(cmd, retry_count=3)
            j = json.loads(output)
            for r in j['Reservations']:
                for i in r['Instances']:
                    # While we are here, lets print out the running instances:
                    say('Running jslave: {}-{}'.format(env, i['PrivateIpAddress']))
                    if 'PublicIpAddress' in i.keys():
                        instance_ips.append(i['PublicIpAddress'])
                    else:
                        say('Warning: running jslave does not have a public ip address.')
            # Reset the env vars in each loop:
            os.environ.clear()
            os.environ.update(os_environ_orig)
    # Reset the env vars upon leaving the function:
    os.environ.clear()
    os.environ.update(os_environ_orig)
    return instance_ips


# Open up the SG for intances in prod and apse2:
def updateSecurityGroups(security_group_to_tweak=None, required_ip_list=[], slave_name=None, jenkins_master_region=None):
    prefix = '-' * 15
    say(prefix + 'Checking to see if the proper Security Groups are set...')
    prod_ip_addresses = getAllProdSlaveIPAddress(slave_name=slave_name)
    # Now get your security group:
    cmd = 'aws ec2 describe-security-groups --region {} --group-ids {}'.format(jenkins_master_region, security_group_to_tweak)
    output, returncode = run(cmd, retry_count=3)
    j = json.loads(output)
    sg = j['SecurityGroups'][0]

    # The required_ip_list is a list of PORT:IP/CID.  Lets massage the prod ip addresses by adding port and cid:
    all_required_ip_list = required_ip_list + ['30001:' + ip + '/32' for ip in prod_ip_addresses]

    # Now lets get a list of IP addresses from bitbucket:
    say('Checking IP address of github.com to make sure it can talk to Jenkins...')
    output, returncode = run('dig +short github.com', retry_count=3)
    current_bitbucket_ip_list = list(set([x.strip()[:x.strip().rfind('.')] + '.0' for x in output.split('\n') if x != '']))
    current_bitbucket_ip_list = ['443:' + ip + '/24' for ip in current_bitbucket_ip_list]

    # Now we have the complete of IP addresses that we need:
    all_required_ip_list = all_required_ip_list + current_bitbucket_ip_list

    # OK. Now lets add the ones that are missing:
    for required_ip in all_required_ip_list:
        bAddIp = False
        required_port = str(required_ip.split(':')[0])
        required_cid = str(required_ip.split(':')[1])
        for perm in sg['IpPermissions']:
            if str(perm['ToPort']) == required_port:
                for ipRange in perm['IpRanges']:
                    if required_cid == str(ipRange['CidrIp']):
                        bAddIp = True
                        break
                if bAddIp is True:
                    break

        if bAddIp is False:
            say('We need to add this: {}:{}'.format(required_port, required_cid))
            jenkins_master_region
            cmd = ('aws ec2 authorize-security-group-ingress --region {} '
                   '--group-id {} --protocol tcp --port {} '
                   '--cidr {}').format(jenkins_master_region,
                                       security_group_to_tweak,
                                       required_port, required_cid)
            run(cmd, retry_count=3)
    # Now that we added everything, lets remove everything that is not on the list:
    for perm in sg['IpPermissions']:
        for ipRange in perm['IpRanges']:
            bRemoveIp = True
            # Check to see if this range should be deleted:
            for required_ip in all_required_ip_list:
                required_port = str(required_ip.split(':')[0])
                required_cid = str(required_ip.split(':')[1])
                if str(perm['ToPort']) == required_port and str(ipRange['CidrIp']) == required_cid:
                    bRemoveIp = False
                    break
            if bRemoveIp is True:
                say('We need to remove this ingress rule: {}:{}'.format(perm['ToPort'], ipRange['CidrIp']))
                cmd = ('aws ec2 revoke-security-group-ingress --region {}'
                       ' --group-id {}'
                       ' --protocol tcp --port {}'
                       ' --cidr {}').format(jenkins_master_region, security_group_to_tweak,
                                            perm['ToPort'], ipRange['CidrIp'])
                run(cmd, retry_count=3)
    say(prefix + 'Done checking security groups!')


# For some reason we need to run the garbage collector periodically:
def runGc():
    say('Running Jenkins GC...', do_print=args.debug)
    cmd = ('java -jar jenkins-cli.jar -remoting -noCertificateCheck -i {} -s {}'
           ' build util-slave-manager-garbage-collector -s -v').format(args.id_rsa, args.url)
    output, returncode = run(cmd=cmd,
                             hide_command=bHide_command,
                             retry_count=3,
                             raise_on_failure=True)
    if 'Garbage collector executed' in output:
        for el in output.split('\n'):
            if 'Garbage collector executed' in el:
                say(el.strip(), color='green')
    else:
        say(output, color='yellow')


# Write out a file with the stats:
def writeStats(output_file=None, stats_dict=None):
    say('Writing {} with the stats of this run.'.format(output_file))
    with open(output_file, 'wt') as fd:
        writer = csv.writer(fd)
        writer.writerow(sorted(stats_dict.keys()))
        writer.writerow([stats_dict[key] for key in sorted(stats_dict.keys())])


# Run groovy script to get jenkins info:
def getJenkinsQueues(bHide_command=True, current_counter=0, max_loop=0):
    jenkins_json = None
    say(' ')
    say('===== Running jar file to get jenkins queue and node info: {}/{}'.format(current_counter, max_loop))
    all_amis = ' '.join([g_env_map['environments'][k]['ami_id'] for k in g_env_map['environments'].keys()])
    cmd = ('java -jar jenkins-cli.jar -remoting -noCertificateCheck -i {} -s {}'
           ' groovy {} {}').format(args.id_rsa, args.url, args.groovy, all_amis)
    stdout, stderr, returncode = run(cmd=cmd,
                                     hide_command=bHide_command,
                                     separate_std_out_err=True,
                                     retry_count=3)
    # Jenkins sometimes puts a benign exception message, so lets ignore that:
    try:
        ignore_warning = 'Skipping HTTPS certificate checks altogether. Note that this is not secure at all.'
        if ignore_warning in stdout:
            stdout = stdout.replace(ignore_warning, '')
        jenkins_json = json.loads(stdout)
    except:
        say('***Error: Something went wrong. Here is the output from jenkins: \n{}\n{}'.format(stdout, stderr))
        g_error_stats['jar_file_error'] += 1
    return jenkins_json


# Helper function to return a dict of instance's keys:
def getTags(instance):
    tags = {}
    if 'Tags' in instance.keys():
        for tag in instance['Tags']:
            tags[tag['Key']] = tag['Value']
    return tags


# Set termination policy on existing instances:
def setTerminationPolicyOnAllExistingInstances(slave_name=None, jenkins_queue=None):
    say('Setting Termination policy on all instances in all environments whose name tag is: {}'.format(slave_name))
    os_environ_orig = dict(os.environ)
    # Reset the contents of this global dict of instances:
    # Since we are cycling through all instances in all environments, let get a snapshot of all instances:
    global g_instance_details
    g_instance_details = {}
    for env in g_env_map['environments'].keys():
        account_id = g_env_map['environments'][env]['account-id']
        switchEnvironments(target_env=str(env), session_name='set-termination-policy', account_id=account_id)
        # Get a list of all non-terminated instances:
        cmd = 'aws ec2 describe-instances --region ' + g_env_map['environments'][env]['region']
        output, returncode = run(cmd, retry_count=3)
        j = json.loads(output)

        all_instance_ids_in_env = set()
        for r in j['Reservations']:
            for i in r['Instances']:
                if i['State']['Name'] not in ['shutting-down', 'terminated']:
                    all_instance_ids_in_env.add(str(i['InstanceId']))
                else:
                    # remove it from global counters (doesn't matter what counter it is in):
                    g_instance_count[env].discard(str(i['InstanceId']))
                    g_spot_instance_count[env].discard(str(i['InstanceId']))

                # Only set termination policy on slaves:
                bIsSlave = False
                instance_label_set = None
                tags = getTags(i)
                if tags.get('Name') == str(slave_name):
                    bIsSlave = True
                try:
                    if tags.get('slave_data') is not None:
                        j = json.loads(tags.get('slave_data'))
                        instance_label_set = set(map(str, j['slave_labels'].split(' ')))
                except:
                    say('Error parsing slave_data tag: {}'.format(tags.get('slave_data')), do_print=args.debug)

                if bIsSlave is True:
                    # If this is a running jslave and it has been running for 1 hour and is NOT a slave, log an error:
                    if jenkins_queue is not None:
                        # When the slave manager first runs, we don't have the jenkins_queue, so skip this code.
                        if i['State']['Name'] == 'running':
                            launch_time = dateutil.parser.parse(i['LaunchTime'])
                            current_time = datetime.datetime.now(launch_time.tzinfo)
                            (d, h, m, s) = timeDiff(launch_time, current_time)
                            if d > 0 or (d == 0 and h >= 1):
                                # Long running instance. Check if it is a registered slave:
                                for slave in jenkins_queue['slave_queue']:
                                    slave_labels_list = map(str.strip, map(str, slave['labels'].strip('[').strip(']').split(',')))
                                    if any(label.endswith(str(i['PrivateIpAddress'])) for label in slave_labels_list) is False:
                                        say(('***Error: This instance, {}, in {} is NOT a '
                                             'jenkins slave and '
                                             'it has been running for too long: {}').format(i['PrivateIpAddress'],
                                                                                            env,
                                                                                            str((d, h, m, s))))
                                        g_error_stats['instance_not_slave'] += 1
                                        break
                    if i['State']['Name'] in ['stopped', 'stopping', 'pending', 'running']:
                        setTerminationPolicy(instance=i, region=g_env_map['environments'][env]['region'], environment=env)
                        env_key = env + '_None' if instance_label_set is None else '_'.join(list(instance_label_set))
                        if env_key in g_instance_details.keys():
                            g_instance_details[env_key] = g_instance_details[env_key] + 1
                        else:
                            g_instance_details[env_key] = 1
                    if i['State']['Name'] == 'stopped':
                        # If the instance is stopped, lets see if we should just kill it now:
                        terminateOldAmiInstance(instance_id=i['InstanceId'], environment=env)

        # Reset the env vars:
        os.environ.clear()
        os.environ.update(os_environ_orig)
    # Reset the env vars:
    os.environ.clear()
    os.environ.update(os_environ_orig)
    say('Done setting termination policy on all instances in all environments!')


# Handle SQS requests:
def processSqsQueue(jenkis_url=None, aws_sqs_account_id=None, aws_sqs_region=None):
    sqs_queue_name = 'jenkins-requests'
    say('Reading SQS queue, {}, in account, {}, in region, {}, '
        'to see if there is anything to do...'.format(sqs_queue_name, aws_sqs_account_id, aws_sqs_region))
    if aws_sqs_account_id is None:
        say('SQS account id not specified. Not doing anything with processing SQS queue...')
        return
    # Connect to the queue depending on where your environment variables are held
    conn = connect(boto.sqs.connect_to_region, aws_sqs_region)

    q = conn.get_queue(queue_name=sqs_queue_name, owner_acct_id=aws_sqs_account_id)
    # Receive a message:
    if q is None:
        say('Could not find SQS queue: {} in account: {}'.format(sqs_queue_name, aws_sqs_account_id))
        return
    retrievedMessage = q.get_messages()
    if len(retrievedMessage) == 0:
        say('No Messages Found in Queue')
    else:
        message = json.loads(str(retrievedMessage[0].get_body()))
        try:
            job_name = message['name']
            job_parameters = ' '.join([str('-p ' + str(k) + '=' + str(v)) for k, v in message['parameters'].items()])
            action = 'build' if 'action' not in message.keys() else str(message['action'])
            if any([not_allowed in action.lower() for not_allowed in ['delete', 'groovy', 'install', 'node']]):
                raise Exception('Invalid action: {}'.format(action))
            say('Processing message from SQS queue.', banner='SSSS')
            output, returncode = run('java -jar jenkins-cli.jar -remoting -noCertificateCheck -i {} '
                                     '-s {} {} {} {}'
                                     .format(args.id_rsa, jenkis_url, action, job_name, job_parameters),
                                     hide_command=False, retry_count=3)
            g_sqs_stats['sqs_handled'] += 1
        except Exception as err:
            say(traceback.format_exc())
            g_sqs_stats['sqs_dropped'] += 1
        # No matter what delete the message
        q.delete_message(retrievedMessage[0])


# Print out some interesting information:
def printStats(jenkins_queue=None):
    print('Total number of connected slaves: {}'.format(len(jenkins_queue['slave_queue'])))
    for slave in jenkins_queue['slave_queue']:
        (d, h, m, s) = convertSecondsToDateFormat(seconds=int(slave['idle_seconds']))
        say('Slave Name: {}. Idle Time: {}d:{}h:{}m:{}s. Labels: {}'.format(slave['slaveName'],
                                                                            d, h, m, s,
                                                                            slave['labels']), do_print=args.debug)


# Pre-run setup:
def setup(args=None):
    global g_env_map

    # Delete existing jenkins.jar files (it looks like jenkins-cli.jar.NUM):
    for old_jenkins_cli in glob.glob('jenkins-cli.jar.*'):
        os.remove(old_jenkins_cli)

    # Download a fresh jenkins-cli.jar file:
    url = args.url if args.url[-1] == '/' else args.url + '/'
    run('wget --connect-timeout 15 --tries 3 --output-document jenkins-cli.jar '
        '--no-check-certificate {}jnlpJars/jenkins-cli.jar'.format(url), hide_command=False, retry_count=3)

    if not os.path.exists('jenkins-cli.jar'):
        say('***Error: Could not find jenkins-cli.jar. Exiting with error...')
        sys.exit(1)

    # Save the STS crentials so that we don't have to call them each time we need to switch envs:
    for env in g_env_map['environments'].keys():
        account_id = g_env_map['environments'][env]['account-id']
        AccessKeyId, SecretAccessKey, SessionToken = generateStsCredentials(target_env=str(env),
                                                                            session_name='slave-manager-script',
                                                                            account_id=account_id)
        g_env_map['environments'][env]['AccessKeyId'] = AccessKeyId
        g_env_map['environments'][env]['SecretAccessKey'] = SecretAccessKey
        g_env_map['environments'][env]['SessionToken'] = SessionToken

    # Set termination policy on all instances:
    setTerminationPolicyOnAllExistingInstances(slave_name=args.slave_name)


# Parse command line args:
def parseArgs():
    default_groovy = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'get_queue_jobs.groovy')
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--url', help='URL to Jenkins master (http://jenkins.foo.com:8080).', required=True)
    parser.add_argument('--id_rsa', help='Location of id_rsa file to talk with Jenkins Master.', required=True)
    parser.add_argument('--groovy', help='Location of groovy file.', default=default_groovy)
    parser.add_argument('--ami_ids', help='Comma separated list of slave ENV:ami-id.', required=True)
    parser.add_argument('--max_num_of_slaves_in_env', help='The total number of slaves to create in env.', default=10, type=int)
    parser.add_argument('--max_num_of_spot_slaves_in_env', help='The total number of spot slaves to create in env.', default=50, type=int)
    parser.add_argument('--loop_counter', help='The number of times to run main loop.', default=300, type=int)
    parser.add_argument('--slave_name', help='The AWS Name tag of the slaves.', default='jslave-in-house')
    parser.add_argument('--aws_sqs_account_id', help='The AWS Account ID of the SQS queue.', default=None)
    parser.add_argument('--aws_sqs_region', help='The AWS region where the SQS queue lives.', default=None)
    parser.add_argument('--jenkins_master_region', help='The AWS region where the Jenkins master lives.', default='us-west-2')
    parser.add_argument('--owner_email', help='The email address to add to the owner tag.', required=True)
    parser.add_argument('--max_spot_price', help='The maximum spot price to use.', default="0.2")
    parser.add_argument('--debug', help='Add verbosity.', action='store_true')
    parser.add_argument('--required_ip_list',
                        help='List of space delimited PORT:IP/CID to ignore when examining security group',
                        default=[], nargs='+')
    args = parser.parse_args()
    # Slip the args into g_env_map:
    for amis in args.ami_ids.split(','):
        env, ami_id = amis.split(':')
        g_env_map['environments'][env]['ami_id'] = ami_id
    return args


if __name__ == '__main__':
    # TODO: Convert to boto.
    # TODO: Print out time left before termination.
    # TODO: Handle 'thrashing' launching instances that do not connect, and re-creating.

    args = parseArgs()
    start_time = datetime.datetime.now()
    setup(args)
    say('Valid Labels: \n{}'.format('\n'.join(sorted(list(g_valid_labels)))))
    # Only print the java command once, to save clutter in console output
    bHide_command = False
    for i in range(args.loop_counter):
        # Get the list of items and nodes on the queue:
        jenkins_json = getJenkinsQueues(bHide_command=bHide_command, current_counter=i + 1, max_loop=args.loop_counter)
        bHide_command = True
        if jenkins_json is not None:
            createOrStartSlaves(jenkins_queue=jenkins_json, max_slaves=args.max_num_of_slaves_in_env,
                                max_spot_slaves=args.max_num_of_spot_slaves_in_env,
                                slave_name=args.slave_name, owner_email=args.owner_email)
            stopSlaves(jenkins_queue=jenkins_json)

        # Every 15 seconds update your SG for prod instances:
        if i % 3 == 0:
            updateSecurityGroups(security_group_to_tweak=g_env_map['jenkins-master']['jenkins-master-sg-id'],
                                 required_ip_list=args.required_ip_list,
                                 slave_name=args.slave_name,
                                 jenkins_master_region=args.jenkins_master_region)

        # Every 30 seconds or so, run the garbage collector:
        if i % 6 == 0:
            runGc()

        # Every few minutes or so, re-set termination policy and check SQS:
        if i % 15 == 0:
            if jenkins_json is not None:
                printStats(jenkins_queue=jenkins_json)
            setTerminationPolicyOnAllExistingInstances(slave_name=args.slave_name)
            processSqsQueue(jenkis_url=args.url,
                            aws_sqs_account_id=args.aws_sqs_account_id,
                            aws_sqs_region=args.aws_sqs_region)
        (d, h, m, s) = timeDiff(start_time, datetime.datetime.now())
        if m > 55:
            say('Reaching STS limition of 1 hour. Stopping loop now.')
            break
        time.sleep(5)

    # Write all the stats to csv files:
    writeStats(output_file='properties_run.csv', stats_dict=g_instance_stats)
    writeStats(output_file='properties_sqs.csv', stats_dict=g_sqs_stats)
    writeStats(output_file='properties_error.csv', stats_dict=g_error_stats)
    writeStats(output_file='properties_instance_count.csv', stats_dict=g_instance_details)
    say('all done!')
