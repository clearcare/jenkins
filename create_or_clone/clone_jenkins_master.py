import os
import sys
import subprocess
import json
import argparse
import time
import uuid
import random
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from common import run, say, getAzFromSubnet, createInstance, g_env_map

try:
    input = raw_input
except NameError:
    pass


# Parse command line args:
def parseArgs():
    default_rpm = 'http://pkg.jenkins-ci.org/redhat-stable/jenkins-1.625.3-1.1.noarch.rpm'
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--ami_id', help='AMI-ID.', default='ami-8f5a4bee')
    parser.add_argument('--current-master-ip', help='Public IP Address of existing Jenkins Master.')
    parser.add_argument('--id_rsa', help='id_rsa file used to ssh onto the master.', default=None)
    parser.add_argument('--key_pair_name', help='AWS key name.', default='jenkins.cloud')
    parser.add_argument('--ssh_user', help='ssh user.', default='ec2-user')
    parser.add_argument('--target_env', help='The target env to create instance in. Usually "eod-us-west-2".',
                        choices=g_env_map['environments'].keys(), default='eod-us-west-2')
    parser.add_argument('--volume_type', help='The EBS volume type.', choices=['gp2', 'standard'], default='gp2')
    parser.add_argument('--volume_size', help='The EBS volume size in GB. If none specified, the size of the snapshot is used.', default=None)
    parser.add_argument('--instance_type', help='The size of the instance.', default='t2.medium')
    parser.add_argument('--jenkins_rpm', help='The http location of the jenkins rpm.', default=default_rpm)
    parser.add_argument('--owner_email', help='The owner tag for the jenkins master', default='owner@example.com')
    parser.add_argument('--debug', action='store_true', help='Show debug information.')
    args = parser.parse_args()

    if args.current_master_ip is None and args.volume_size is None:
        say('You have not specified an existing jenkins master, so a EBS volme will be created for you.')
        say('You must specify a volume size for this new volume.')
        say('Exiting with error...')
        sys.exit(1)
    return args


# Find the volume that contains a certain tag:
def findJenkinsVolume(instance=None):
    say('Finding data storage block device...', banner="*")
    for block_device in instance['BlockDeviceMappings']:
        if 'Ebs' in block_device.keys():
            output, returncode = run('aws ec2 describe-volumes --volume-ids ' + block_device['Ebs']['VolumeId'], hide_command=g_hide_command, debug=g_args.debug)
            j = json.loads(output)
            if 'Tags' in j['Volumes'][0].keys():
                for tag in j['Volumes'][0]['Tags']:
                    if tag['Key'] == 'Name' and tag['Value'] == 'jenkins-master-volume':
                        say('Found the volume that we want to create a snapshot from (has tag jenkins-master-volume): ' + block_device['Ebs']['VolumeId'] + '. Size:' + str(json.loads(output)['Volumes'][0]['Size']))
                        return block_device
    say('***Error: Could not find master volume via the Name tag.')
    return None


# Find the existing master:
def getExistingMasterInstance(current_master_ip=None):
    say('Making sure jenkins is not running on the master...', banner="*")
    # First make sure you can run a dummy command.
    cmd = '{} {}@{} \'echo hello\''.format(g_ssh_cmd, g_args.ssh_user, g_args.current_master_ip)
    run(cmd, hide_command=g_hide_command)
    say('Script was able to ssh onto master and run "echo hello world"...')

    # OK, not make sure jenkins is not running: Add an extra "-t", otherwise you will get:
    # sudo: sorry, you must have a tty to run sudo
    cmd = '{} -t {}@{} \'sudo service jenkins status\''.format(g_ssh_cmd, g_args.ssh_user, g_args.current_master_ip)
    output, returncode = run(cmd, raise_on_failure=False, hide_command=g_hide_command, debug=g_args.debug)
    if 'jenkins' in output and 'is running...' in output:
        user_input = input('Jenkins is running on the master.  Are you sure you want to continue? (y|n)')
        if user_input != 'y':
            say('goodbye!')
            sys.exit(0)
    say('Getting instance from IP: ' + g_args.current_master_ip)
    output, returncode = run('aws ec2 describe-instances --filters "Name=ip-address,Values=' + g_args.current_master_ip + '"',
                             hide_command=g_hide_command, debug=g_args.debug)
    instance = json.loads(output)['Reservations'][0]['Instances'][0]
    say('Instance-id of existing Jenkins master: ' + instance['InstanceId'])
    return instance


# Create a snapshot of the data volume:
def createSnapshot(volume_id=None):
    say('Creating snapshot...', banner="*")
    cmd = 'aws ec2 create-snapshot --description jenkins-master-snapshot --volume-id ' + volume_id
    output, returncode = run(cmd, hide_command=g_hide_command, debug=g_args.debug)
    snapshot_id = json.loads(output)['SnapshotId']
    while True:
        cmd = 'aws ec2 describe-snapshots --snapshot-ids ' + str(snapshot_id)
        output, returncode = run(cmd, hide_command=g_hide_command, debug=g_args.debug)
        status = json.loads(output)['Snapshots'][0]['State']
        progress = json.loads(output)['Snapshots'][0]['Progress']
        if status == 'completed':
            say('Snapshot has been created!')
            break
        else:
            say('Current Status: ' + str(status) + '. Current Progress: ' + str(progress))
            time.sleep(15)
    cmd = 'aws ec2 create-tags --resources ' + str(snapshot_id) + ' --tags Key=Name,Value=jenkins-master-snapshot'
    output, returncode = run(cmd, hide_command=g_hide_command, debug=g_args.debug)
    return snapshot_id


# Create a volume from a snapshot:
def createVolume(snapshot_id=None, volume_type=None, volume_size=None, region=None, az=None):
    say('Creating volume...', banner="*")
    snapshot_arg = ' --snapshot-id {}'.format(snapshot_id)
    if snapshot_id is None:
        snapshot_arg = ''

    volume_size = '' if volume_size is None else ' --size ' + volume_size
    cmd = 'aws ec2 create-volume --region ' + region + \
          ' --availability-zone ' + az + ' ' + \
          snapshot_arg + \
          ' --volume-type ' + volume_type + volume_size
    output, returncode = run(cmd, hide_command=g_hide_command, debug=g_args.debug)
    volume_id = json.loads(output)['VolumeId']
    size = json.loads(output)['Size']
    while True:
        cmd = 'aws ec2 describe-volumes --volume-ids ' + str(volume_id)
        output, returncode = run(cmd, hide_command=g_hide_command, debug=g_args.debug)
        state = json.loads(output)['Volumes'][0]['State']
        if state == 'available':
            say('Volume has been created: ' + str(volume_id))
            break
        else:
            say('Current State: ' + str(state))
            time.sleep(15)
    cmd = 'aws ec2 create-tags --resources ' + str(volume_id) + ' --tags Key=Name,Value=jenkins-master-volume'
    output, returncode = run(cmd, hide_command=g_hide_command, debug=g_args.debug)
    return volume_id, size


# Attach volume to an instance:
def attacheVolume(volume_id=None, instance_id=None, region=None):
    say('Attaching volume...', banner="*")
    cmd = 'aws ec2 attach-volume --volume-id ' + str(volume_id) + \
          ' --instance-id ' + str(instance_id) + \
          ' --device /dev/sdb'
    output, returncode = run(cmd, hide_command=g_hide_command, debug=g_args.debug)
    say(output)
    while True:
        cmd = 'aws ec2 describe-volumes --volume-ids ' + str(volume_id)
        output, returncode = run(cmd, hide_command=g_hide_command, debug=g_args.debug)
        state = json.loads(output)['Volumes'][0]['State']
        if state == 'in-use':
            say('Volume has been attached: ' + str(volume_id))
            break
        else:
            say('Current State: ' + str(state))
            time.sleep(15)
    # Re-describe the instance, since it now has a new volume:
    cmd = 'aws ec2 describe-instances --instance-ids {} --region {}'.format(instance_id, region)
    output, returncode = run(cmd, hide_command=g_hide_command, debug=g_args.debug)
    instance = json.loads(output)['Reservations'][0]['Instances'][0]
    return instance


# Generic ssh command:
def run_ssh(cmd, raise_on_failure=True):
    pre_cmd = '{} -t {}@{} '.format(g_ssh_cmd, g_args.ssh_user, g_new_instance_ip_address)
    output, returncode = run(pre_cmd + '\'{}\''.format(cmd), hide_command=g_hide_command, debug=g_args.debug, raise_on_failure=raise_on_failure)
    say(output)
    return output, returncode


# Generic scp command:
def run_scp(local_file, destination):
    scp_cmd = 'scp {} '.format(g_id_rsa_option)
    output, returncode = run(scp_cmd + ' {} {}@{}:{}'.format(local_file, g_args.ssh_user, g_new_instance_ip_address, destination),
                             hide_command=g_hide_command, debug=g_args.debug)
    say(output)


# Run some post scripts to setup the instance:
def configureInstance(volume_size=None, default_rpm=None, is_new_instance=True):
    say('Running ssh commands to configure box...', banner="*")
    run_ssh('sudo mkdir -p /var/build')
    while True:
        say('Waiting for {}G partition to be availabe by the OS...'.format(volume_size))
        # -i option in lsblk is "ascii output"
        output, returncode = run_ssh('sudo lsblk -i | grep {}G | grep -v grep'.format(volume_size), raise_on_failure=False)
        if returncode == 0:
            say('{}G volume is now availabe by the OS!'.format(volume_size))
            run_ssh('sudo lsblk -i')
            break
        time.sleep(5)

    run_ssh('sudo file -s /dev/xvdb')
    if is_new_instance is True:
        say('Brand new master. Formating EBS drive...')
        # Creating brand new jenkin master, not cloned from an existing one.
        # Format existing volume:
        run_ssh('sudo mkfs -F -t ext4 /dev/xvdb')

    run_ssh('sudo mount /dev/xvdb /var/build')
    run_ssh('sudo mkdir -p /var/build/jenkins')

    # Modify /etc/fstab:
    run_ssh('echo "#!/bin/sh" > /tmp/fstab.sh')
    run_ssh('echo "echo "/dev/xvdb /var/build ext4 defaults,nofail 0 2" >> /etc/fstab" >> /tmp/fstab.sh')
    run_ssh('sudo chmod +x /tmp/fstab.sh')
    run_ssh('sudo /tmp/fstab.sh')

    # Make sure /etc/fstab is OK:
    run_ssh('sudo mount -a')

    # Install some packages:
    run_ssh('sudo yum -y install git')
    run_ssh('sudo yum -y install java-1.8.0-openjdk-devel')
    run_ssh('sudo alternatives --set java /usr/lib/jvm/jre-1.8.0-openjdk.x86_64/bin/java')

    # Install Jenkins:
    run_ssh('cd /tmp && wget --quiet {}'.format(default_rpm))
    run_ssh('cd /tmp && sudo yum -y install {}'.format(default_rpm))

    if is_new_instance is True:
        say('Brand new instance must have /var/build/jenkins/etc.sysconfig.jenkins...')
        run_ssh('sudo mv /etc/sysconfig/jenkins /var/build/jenkins/etc.sysconfig.jenkins')

    run_ssh('sudo rm -rf /etc/sysconfig/jenkins')
    run_ssh('sudo ln -s /var/build/jenkins/etc.sysconfig.jenkins /etc/sysconfig/jenkins')
    run_ssh('sudo rm -rf /var/lib/jenkins')
    run_ssh('sudo ln -s /var/build/jenkins/ /var/lib/jenkins')

    # Install pip and virtualenv:
    run_ssh('sudo wget --quiet https://bootstrap.pypa.io/get-pip.py')
    run_ssh('sudo python get-pip.py')
    run_ssh('sudo pip install virtualenv')
    run_ssh('sudo pip install termcolor')
    run_ssh('sudo pip install requests==2.18.4')

    # Install logrotate:
    run_scp(os.path.abspath(os.path.join(os.path.dirname(__file__), 'logrotate_jenkins')), '/tmp')
    run_ssh('sudo mv /tmp/logrotate_jenkins /etc/logrotate.d/')
    run_ssh('sudo chown root:root /etc/logrotate.d/logrotate_jenkins')

    # Install and run nginx:
    run_scp(os.path.abspath(os.path.join(os.path.dirname(__file__), 'install_nginx.sh')), '/tmp')
    run_scp(os.path.abspath(os.path.join(os.path.dirname(__file__), 'nginx.conf')), '/tmp')
    run_scp(os.path.abspath(os.path.join(os.path.dirname(__file__), 'etc.init.d.nginx')), '/tmp')
    run_ssh('sudo mv /tmp/nginx.conf /var/build/jenkins/')
    run_ssh('sudo mv /tmp/etc.init.d.nginx /var/build/jenkins/')
    run_ssh('sudo bash /tmp/install_nginx.sh')

    if is_new_instance is True:
        run_ssh('sudo chown -R jenkins:jenkins /var/build/jenkins')
        say("This is a brand new installation of jenkins.  Please see /var/lib/jenkins/secrets/initialAdminPassword for the initial password!")
        say("And start jenkins with: sudo service jenkins start.  It will be running on port 8080")
        say("Your next steps are to install a cert and run jenkins on port 443.")


def setTerminationPolicy(instance=None, region=None):
    # Before terminating, make sure all block devices are set to DeleteOnTermination is true:
    for block_device in instance['BlockDeviceMappings']:
        say('------------')
        say(block_device)
        if 'Ebs' in block_device.keys():
            if block_device['Ebs']['DeleteOnTermination'] is False:
                # Flip the bit to true:
                device_name = block_device['DeviceName']
                cmd_modify = 'aws ec2 modify-instance-attribute' + \
                             ' --region ' + region + \
                             ' --instance-id ' + str(instance['InstanceId']) + \
                             ' --block-device-mappings ' + \
                             '\'[{"DeviceName": "' + device_name + '","Ebs":{"DeleteOnTermination":true}}]\''
                output, returncode = run(cmd_modify, hide_command=True, retry_count=3)
                # Wait for it to stick:
                bHasStuck = False
                say('Waiting for termination policy to stick...')
                for i in range(30):
                    cmd = 'aws ec2 describe-instance-attribute --instance-id {}' \
                          ' --attribute blockDeviceMapping --region {}'.format(instance['InstanceId'], region)
                    output, returncode = run(cmd, hide_command=True, retry_count=3)
                    # We have to loop (again) to find the block device that we just set :(
                    for bd in json.loads(output)['BlockDeviceMappings']:
                        if bd['DeviceName'] == device_name:
                            if bd['Ebs']['DeleteOnTermination'] is True:
                                bHasStuck = True
                                break
                    if bHasStuck is True:
                        say('DeleteOnTermination has stuck!')
                        break
                    else:
                        say('Block device DeleteOnTermination has not stuck yet. Sleeping for 2 seconds...')
                        time.sleep(2)
                if bHasStuck is False:
                    say('***Error: We set the launch perm, but it did not stick. Error!')
                    sys.exit(1)
    say('All block devices on instance, ' + str(instance['InstanceId']) + ', are set to terminate on deletion.')


# Verify AMI exists:
def amiExists(ami_id=None):
    cmd = 'aws ec2 describe-images --image-ids ' + ami_id
    output, returncode = run(cmd, hide_command=g_hide_command, debug=g_args.debug)
    j = json.loads(output)
    if len(j['Images']) == 0:
        say('AMI-ID not found: ' + ami_id)
        return False
    else:
        say('AMI-ID found: ' + ami_id)
        return True


if __name__ == "__main__":
    # Parse the command line args:
    g_args = parseArgs()

    # Debug or no Debug:
    g_hide_command = False if g_args.debug is True else True

    # Is this a new master or clone from existing master:
    b_new_instance = True if g_args.current_master_ip is None else False
    g_new_instance_ip_address = None

    # Setup general ssh command syntax:
    if g_args.id_rsa is not None:
        if not os.path.exists(g_args.id_rsa):
            say('***Error: File does not exist: {}'.g_args.id_rsa)
            sys.exit(1)
    g_id_rsa_option = '' if g_args.id_rsa is None else '-i {}'.format(g_args.id_rsa)
    g_ssh_cmd = ('ssh {} -o ControlMaster=no -o ConnectTimeout=30 -t -n '
                 '-o PreferredAuthentications=publickey -o StrictHostKeyChecking=no '
                 '-o UserKnownHostsFile=/dev/null').format(g_id_rsa_option)

    if amiExists(ami_id=g_args.ami_id) is False:
        say('***Error: You have to pass in an AMI that exists.')
        sys.exit(1)

    snapshot_id = None
    subnet_id = random.choice(g_env_map['environments'][g_args.target_env]['vpcsubnet'])['id']
    az = getAzFromSubnet(target_env=g_args.target_env, subnet_id=subnet_id)
    if b_new_instance is False:
        say('Cloning existing jenkins master from: '.format(g_args.current_master_ip))
        # Make sure Jenkins service is NOT running and grab the instance-id of the master:
        existing_instance = getExistingMasterInstance(current_master_ip=g_args.current_master_ip)
        # Get the existing az, needed to create a new volume:
        subnet_id = existing_instance['SubnetId']
        az = existing_instance['Placement']['AvailabilityZone']
        # Find volume that /var/build lives on:
        block_device = findJenkinsVolume(existing_instance)

        # Create snapshot from that volume (and add tags):
        snapshot_id = createSnapshot(volume_id=str(block_device['Ebs']['VolumeId']))

    # Create volume from that snapshot (and add tags):
    volume_id, volume_size = createVolume(snapshot_id=snapshot_id,
                                          volume_type=g_args.volume_type,
                                          volume_size=g_args.volume_size,
                                          region=g_env_map['environments'][g_args.target_env]['region'],
                                          az=az)

    # Create new jenkins master instance:
    tags = {'role': 'jenkins-master', 'service': 'jenkins-master', 'owner': g_args.owner_email,
            'Name': 'jenkins-master', 'environment': 'eod'}
    instance_profile = '\'{{"Arn":"arn:aws:iam::{}:instance-profile/{}"}}\''.format(g_env_map['environments'][g_args.target_env]['account-id'],
                                                                                    g_env_map['jenkins-master']['jenkins-master-iam-role'])

    new_instance = createInstance(ami_id=g_args.ami_id,
                                  instance_type=g_args.instance_type,
                                  target_env=g_args.target_env,
                                  ssh_user=g_args.ssh_user,
                                  id_rsa=g_args.id_rsa,
                                  key_name=g_args.key_pair_name,
                                  tags_dict=tags,
                                  security_group_ids=g_env_map['jenkins-master']['jenkins-master-sg-id'],
                                  instance_profile=instance_profile,
                                  subnet_id=subnet_id,
                                  debug=g_args.debug)

    # Attach volume to this instance (we reset the new_instance variable because the EBS vol has been attached):
    new_instance = attacheVolume(volume_id=volume_id, instance_id=str(new_instance['InstanceId']), region=g_env_map['environments'][g_args.target_env]['region'])

    # Set termination of ebs volume on termination of instance:
    setTerminationPolicy(instance=new_instance, region=g_env_map['environments'][g_args.target_env]['region'])

    # Run ssh command on that box:
    g_new_instance_ip_address = new_instance['PublicIpAddress']
    configureInstance(volume_size=volume_size,
                      default_rpm=g_args.jenkins_rpm,
                      is_new_instance=b_new_instance)

    # Tell user to tweak route53 entries in TCA account and wait till they are done:
    say('', banner="*", color='green')
    say('Manual Steps to make it "live":', color='green')
    say('* If you haven\'t done so alreay, you should associate an Elastic IP to this instance.', color='green')
    say('* Associate your EIP to this instance (select "reassociate ip address" checkbox): {}'.format(new_instance['InstanceId']), color='green')
