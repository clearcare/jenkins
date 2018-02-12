## How it Works ##

### What this Script Does ###

- Create a new Jenkins master from scratch.
- "Clone" an existing Jenkins master created by this script.

### Prerequisites: ###
- Create a key name (or upload your id_rsa file to AWS).
- Create a jenkins-master Security Group. I suggest opening port 22 to your personal IP address, and then 8080, 443, and 80 open the world.
- Create a jenkins-master IAM Role.

### Creating a new Jenkins Master ###
To create a NEW Jenkins Master:
1. Modify common.py g_env_map dictionary
2. For the "key", put a user friendly name. ie infra-us-east-2 (I always like to put the region in the name)
3. Select a VPC and the list of subnet's associated with the VPC.
4. Create a security group called "jenkins-master". I usually open port 22, 8443, and 8080.
5. Run the script like so:

* Note, you can find the correct AMI to use here: [amazon-linux-ami](https://aws.amazon.com/amazon-linux-ami/)

```
python clone_jenkins_master.py --ami_id ami-f2d3638a \
  --jenkins_rpm http://pkg.jenkins-ci.org/redhat-stable/jenkins-2.89.3-1.1.noarch.rpm \
  --id_rsa [YOUR PEM FILE] \
  --debug \
  --instance_type t2.large \
  --volume_size 12 \
  --target_env [USER_FRIENDLY_NAME_IN_ENVIRONMENT_JSON] \
  --key_pair_name [KEY_PAIR_NAME]
```

### To Clone an Existing Master: ###

Simply pass in --current-master-ip to the script!
* Note: You must have created the Jenkins master with this script, since there are a lot of assumptions (like volume mapping, directory names, etc. etc.)

The cloning script:
* Makes sure jenkins is not running on the source ip address.
* Creates a snapshot of the existing volume and attaches a newly created volume to the instance that it creates.

```
python clone_jenkins_master.py --ami_id ami-f2d3638a \
  --jenkins_rpm http://pkg.jenkins-ci.org/redhat-stable/jenkins-2.89.3-1.1.noarch.rpm \
  --id_rsa [YOUR PEM FILE] \
  --debug \
  --instance_type t2.large \
  --volume_size 12 \
  --current-master-ip W.X.Y.Z
```
