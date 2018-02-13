## How it Works ##

### Prerequisites: ###

- Configre your "master" node with the label "master_node"
- Install Swarm Plugin: [https://wiki.jenkins-ci.org/display/JENKINS/Swarm+Plugin](https://wiki.jenkins-ci.org/display/JENKINS/Swarm+Plugin)
- Install Groovy Plugin: [https://wiki.jenkins-ci.org/display/JENKINS/Groovy+plugin](https://wiki.jenkins-ci.org/display/JENKINS/Groovy+plugin)
  - Needed to run System Groovy commands to run the Garbage Collector job.
  - Garbage Collector job needs these pre-approved under [JENKINS_URL]/scriptApproval/
```
method groovy.lang.Script println java.lang.Object
method java.lang.Runtime freeMemory
method java.lang.Runtime totalMemory
staticMethod java.lang.Runtime getRuntime
staticMethod java.lang.System gc
```
- AnsiColor plugin (optional) for pretty colors in the output.

- jenkins-master security group needs this inline policy:
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "Stmt1447855754000",
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": "arn:aws:iam::[AWS_ACCOUNT_ID]:role/jenkins_master"
        }
    ]
}
```
- jenkins-master also needs the IAM policy to create instances.

- jenkins-master needs Agent port 30001 open under [JENKINS_URL]/configureSecurity

- jenkins-slave needs the minimal IAM policy needed to run your jobs. ReadOnlyAccess policy, for instance.

- jenkins-slave needs to run the swarm jar on boot. You can do it like this:
  - create an executable file, /var/lib/cloud/scripts/per-boot/start_swarm.sh, with the contents:

```bash
#!/bin/bash
REGION=$(curl -s http://169.254.169.254/latest/dynamic/instance-identity/document | jq -r .region)
export MY_PASSWORD=`aws ssm --region ${REGION} get-parameter --name jenkins_slave --with-decryption | jq --raw-output .Parameter.Value`
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
SLAVE_DATA=`aws ec2 describe-tags --filters "Name=resource-id,Values=$INSTANCE_ID" "Name=key,Values=slave_data" --region=$REGION --output=text | cut -f5`
# Now get All elements from slave_data that we need:
MASTER_URL=`echo $SLAVE_DATA | jq --raw-output .jenkins_url`
DESCRIPTION=`echo $SLAVE_DATA | jq --raw-output .description`
SLAVE_LABELS=`echo $SLAVE_DATA | jq --raw-output .slave_labels`
NUM_OF_EXECUTORS=`echo $SLAVE_DATA | jq --raw-output .num_of_executors`

java -jar /jenkins/swarm-client-3.5.jar \
 -name "${SLAVE_LABELS}-${INSTANCE_ID}" \
 -description "$DESCRIPTION" \
 -executors $NUM_OF_EXECUTORS \
 -fsroot /jenkins/ws \
 -labels "$SLAVE_LABELS" \
 -master "$MASTER_URL" \
 -mode normal \
 -retry 30 \
 -disableSslVerification \
 -username '[JENKINS_USER_NAME]' \
 -passwordEnvVariable MY_PASSWORD &> /var/log/swarm.log &
```

- The Slaves should be EBS backed, so that we can start/stop them, rather than terminating.

You'll need 2 jobs:
- FreeSytle project called "util_slave_manager". Set it to run on "master_node" every 5 minutes.  It takes ~30m to run, so with this set up, it will essetially run 24/7.
This is the contents of the shell script to run:

```bash
# Download the environment.json file from s3:
aws s3 cp s3://[YOUR_PRIVATE_BUCKET]/environment.json .
# Download the id_rsa file needed to run jenkins cli jar file:
aws s3 cp s3://[YOUR_PRIVATE_BUCKET]/id_rsa .

# This is the AMI to use across all environments:
AMI_ID=[SLAVE_AMI]

python2.7 slave_manager/slave_manager.py \
--id_rsa id_rsa \
--owner_email "[OWNER_EMAIL]" \
--aws_sqs_account_id 784548236052 \
--aws_sqs_region us-east-2 \
--url ${JENKINS_URL} \
--max_num_of_slaves_in_env 2 \
--max_spot_price 0.2 \
--ami_ids my-test-env:${AMI_ID} \
--loop_counter 300 \
--jenkins_master_region us-east-2 \
--required_ip_list \
443:0.0.0.0/0 \
80:0.0.0.0/0 \
8080:0.0.0.0/0 \
22:[SPECIFIC_IP]/32
```

- FreeSytle project called "util-slave-manager-garbage-collector". Set it to run on "master_node".  It should run a system groovy script called "run_gc.groovy"


### How it Works in a NutShell: ###

In a nutshell, the "slave manager" job is a FreeSytle project that runs continuously on a Jenkins Master executor that asks Jenkins every 5 seconds:

- What is on the build queue?
- What is on the slave list?

And based on that information, it will create an instance from an AMI (or start an instance, if there is already a stopped instance) that matches the build label.
Also, the script will "stop" an instance if it has been idle for 45 minutes.

### Other Things this Script Does ###

- Read build requests from an SQS Queue: The script periodically reads an SQS queue (jenkins-requests) and based on the messages on that queue, it can do certain things. By default, if no "action" key is present, like the example below, the action is "build".  The actions NOT allowed are: 'delete', 'groovy', 'install', 'node'.  It basically runs: java -jar jenkins-cli.jar "action" "name" "parameters".

{
  "name": "foo",
  "action": "build",
  "parameters": {
    "BAR": "baz"
  }
}

- The script also needs to open up ports from jslaves in Production, since these slaves do not have access to jenkins.  The master STS's into production to get the public IP address of the jslaves and tweaks its own Security Group to allow port 443 from that specific IP address.  It does the same thing with SCM (github or bitbucket) (It needs to allow the SCM system to POST to jenkins)

### How it Works in Detail: ###

Technically, the "slave manager" job does not run continuously; it runs on a 15 minute cron, but the job takes 30 minutes to run, hence it always running. (This is so that we can collect and plot stats on what happened over 30 minute chunks of time (ie how many instances were created/stopped/terminated))

The job runs the jenkins-cli.jar using the "groovy" argument to collect information from Jenkins.  The groovy script does all the heavy lifting.  It prints out a json blob, whose contents are the things on the queue AND the list of slaves that Jenkins knows about.

So here is an example:

1. Something gets put on the queue that needs a slave with a label "foo".
2. The slave manager job detects that.
3. The slave manager sees if there is a stopped instance that has an AWS Tag called "Key=slave_label, Value=foo" that it can start.  If yes it just starts it. (Side Note: If there are multiple stopped instances, the slave manager will figure out the last instance the job ran on and if it exists, it will start that particular instance, else a random one.)
4. If there are no stopped instances, it has to go and create a new instance from an AMI-ID that is passed in as a command line arg to the slave manager.
5. The slave manager writes a file on disk called "working_on_[JOB_NAME].working" so that it knows not to create/start another instance.
6. When the job is off the queue, the slave manager will delete the file.

Now, here are some gotcha's:

- If the file exists after 5 minutes, it will be deleted and it will try again (Due to priority queues, something (perhaps a long running job?) can "steal" a jobs executor... might as well try again.).
- There is a hard limit on the total number of instances that can be created/started (to avoid AWS cost).
- The script is hard coded to use STS to assume the role in a different AWS account.

### Why not EC2-Plugin? ###
There are few reasons we can't use the ec2-plugin:

- Slaves do not gracefully terminate when there is a configuration change. For example, if there is an instance with multiple executors, it has to prevent NEW jobs from running on it AND let existing jobs finish.
- It uses ssh FROM master TO slave.  Due to firewall issues, this restriction makes it really hard to manage security groups. The swarm plugin works the other way: from slave TO master.  The slave manager, since it created the instance in X AWS account, has its public-ip, so it has the ability to punch a hole in its Security Group for port 443 to that IP address.
- You don't get error like this during a jenkins update or a plugin update: https://stackoverflow.com/questions/47003627/jenkins-ec2-plugin-script-approval


### Use STS ###
The jenkins master uses STS to launch instances in other accounts, including its own.
See this on how to do this: [How-to-enable-cross-account-access](https://blogs.aws.amazon.com/security/post/Tx70F69I9G8TYG/How-to-enable-cross-account-access-to-the-AWS-Management-Console)

### Why id_rsa and not username/password when using jenkins-cli.jar? ###
Bottom line, there is a bug in Jenkins which forces you to use id_rsa file:
[JENKINS-12543](https://issues.jenkins-ci.org/browse/JENKINS-12543)
To apply a public_key to your account, go to [JENKINS_URL]/me and click on "configure".
You'll see a place to add your public key.  Plus, it's probably safer to use id_rsa.
