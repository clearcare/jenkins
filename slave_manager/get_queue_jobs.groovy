import groovy.json.*;

// The only args to the script are valid amis.
// We need a try/catch because running this in Jenkins causes an exception:
valid_ami_list = ["UNKNOWN"];
try {
    valid_ami_list = valid_ami_list + (java.util.ArrayList)(args);
} catch (groovy.lang.MissingPropertyException e) {
    // An ami-id that is UNKNOWN (ie the script can't figure out what the AMI ID is)
}

return_queue = ["build_queue": [], "slave_queue": [], "messages":[]]

// Read the build queue:.
for (anItem in hudson.model.Hudson.instance.getQueue().getItems()) {
    if (anItem.isBlocked() == true){
        continue;
    }
    // Matrix projects that are restricted to run on a node are "unBuildable".
    if (anItem.isBuildable() == true || anItem.toString().contains("hudson.matrix.MatrixProject")) {
        // println anItem.metaClass.methods*.name.sort().unique();
        return_queue["messages"].add('============ Item in Queue Needs a Slave ===================');

        jobName = anItem.toString().substring(anItem.toString().lastIndexOf('[') + 1, anItem.toString().lastIndexOf(']'));
        job = hudson.model.Hudson.instance.getItem(jobName);
        if (job == null){
            return_queue["messages"].add('Item on queue is from multiconfiguration or promotion job: ' + jobName);
            parentName = jobName.toString().substring(0,jobName.toString().indexOf("/"));
            parent_job = hudson.model.Hudson.instance.getItem(parentName);
            // Initialize the variables:
            lastBuildOn = "UNKNOWN";
            parameters = "NONE";
            labels = anItem.getAssignedLabel();
            throttleEnabled = false;

            for (child in parent_job.getAllJobs()){
                if (child.isInQueue() == true){
                    childName = child.toString().substring(child.toString().lastIndexOf('[') + 1, child.toString().lastIndexOf(']'));
                    if (childName == jobName){
                        // We need a slave for this child:
                        return_queue["messages"].add('We need a slave for this child: ' + childName.toString());
                        labels = child.getAssignedLabel();
                        parameters = "NONE";
                        if (child.isParameterized() == true){
                            parameters = child.getParams();
                            if (parameters == ""){
                                parameters = "NONE";
                            }
                        }
                        lastBuildOn = "UNKNOWN";
                        if (child.getLastBuild() != null) {
                            lastBuildOn = child.getLastBuild().getBuiltOnStr();
                        }
                        throttleProperty = null;
                        try {
                            throttleProperty = child.getProperty(hudson.plugins.throttleconcurrents.ThrottleJobProperty);
                        } catch (groovy.lang.MissingPropertyException e) {
                            return_queue["messages"].add('Throttle plugin not installed.');
                        }
                        if (throttleProperty != null){
                            throttleEnabled = throttleProperty.getThrottleEnabled();
                        } else {
                            throttleEnabled = false;
                        }
                        break;
                    }
                }
            }
        } else {

            labels = anItem.getAssignedLabel();
            parameters = anItem.getParams();
            if (parameters == ""){
                parameters = "NONE";
            }
            lastBuildOn = "UNKNOWN";
            if (job.getLastBuild() != null) {
                lastBuildOn = job.getLastBuild().getBuiltOnStr();
            }
            throttleProperty = null;
            try {
                throttleProperty = job.getProperty(hudson.plugins.throttleconcurrents.ThrottleJobProperty);
            } catch (groovy.lang.MissingPropertyException e) {
                return_queue["messages"].add('Throttle plugin not installed.');
            }
            if (throttleProperty != null){
                throttleEnabled = throttleProperty.getThrottleEnabled();
            } else {
                throttleEnabled = false;
            }
        }
        return_queue["messages"].add(jobName + ' needs a Slave with Attributes: ' + labels);
        return_queue["build_queue"].add(["jobName":jobName.toString(),
                                         "labels":labels.toString(),
                                         "lastBuiltOn": lastBuildOn.toString(),
                                         "throttleEnabled": throttleEnabled.toString(),
                                         "parameters":parameters.toString()])
    }
}

// Read the slave queue:
g_TimeOutValue = 60 * 45; // Value in seconds.

g_SlaveDescriptionString = "Created by the Slave Creator Job"; // Will only delete nodes that have this text in the description.

// Get the current time in milliseconds from epoch:
timeInMillis = System.currentTimeMillis();
return_queue["messages"].add('Current Time: ' + new Date((long) timeInMillis));

for (aSlave in hudson.model.Hudson.instance.slaves) {
    return_queue["messages"].add('==========================================================');
    return_queue["messages"].add('Name: ' + aSlave.name);
    return_queue["messages"].add('getLabelString: ' + aSlave.getLabelString());
    return_queue["messages"].add('getAssignedLabels: ' + aSlave.getAssignedLabels());
    return_queue["messages"].add('getNodeDescription: ' + aSlave.getNodeDescription());
    return_queue["messages"].add('getNumExectutors: ' + aSlave.getNumExecutors());
    return_queue["messages"].add('getRemoteFS: ' + aSlave.getRemoteFS());
    return_queue["messages"].add('getMode: ' + aSlave.getMode());
    return_queue["messages"].add('getRootPath: ' + aSlave.getRootPath());
    return_queue["messages"].add('getDescriptor: ' + aSlave.getDescriptor());
    return_queue["messages"].add('getComputer: ' + aSlave.getComputer());
    return_queue["messages"].add('    computer.isAcceptingTasks: ' + aSlave.getComputer().isAcceptingTasks());
    return_queue["messages"].add('    computer.isLaunchSupported: ' + aSlave.getComputer().isLaunchSupported());

    connectTime = aSlave.getComputer().getConnectTime();
    demandTime = aSlave.getComputer().getDemandStartMilliseconds();
    idleTime = aSlave.getComputer().getIdleStartMilliseconds();
    ope_idle_count = aSlave.getComputer().countIdle();

    return_queue["messages"].add('    computer.getConnectTime: ' + connectTime);
    return_queue["messages"].add('    computer.getDemandStartMilliseconds: ' + demandTime);
    return_queue["messages"].add('    computer.getIdleStartMilliseconds: ' + idleTime);
    return_queue["messages"].add('    computer.isOffline: ' + aSlave.getComputer().isOffline());
    return_queue["messages"].add('    computer.countBusy: ' + aSlave.getComputer().countBusy());
    return_queue["messages"].add('    computer.isJnlpAgent: ' + aSlave.getComputer().isJnlpAgent());

    // If the *online* slave has been idle for over an hour, kill it:
    diff = (int)((timeInMillis - idleTime) / 1000);
    connect_time_minutes = (int)((timeInMillis - connectTime) / 1000 / 60);
    countBusy = aSlave.getComputer().countBusy();
    isOffLine = aSlave.getComputer().isOffline();

    terminate_me = "false";
    locCreated = aSlave.getNodeDescription().indexOf("Created by Swarm");
    return_queue["messages"].add("Current diff in seconds: " + diff);
    return_queue["messages"].add("Current timeout value (seconds): " + g_TimeOutValue);
    return_queue["messages"].add("Current connect time (minutes): " + connect_time_minutes);
    return_queue["messages"].add("IsOffLine: " + isOffLine);
    return_queue["messages"].add("countBusy: " + countBusy);
    return_queue["messages"].add("Does contain '" + g_SlaveDescriptionString + "' in description: " + locCreated);

    ami_id = "UNKNOWN";
    ami_id_loc = aSlave.getNodeDescription().indexOf("AmiId=");
    if ( ami_id_loc >= 0 ) {
      if (ami_id_loc + 6 + 12 > aSlave.getNodeDescription().length() ) {
        return_queue["messages"].add("AmiId was found, but it is not right length.");
      } else {
        ami_id = aSlave.getNodeDescription().substring(ami_id_loc + 6, ami_id_loc + 6 + 12);
      }
    }

    // Terminate check: If Idle (and it's a slave we created), mark it to die:
    g_idle_minutes_before_billing_cycle = 60 * 5
    if (countBusy == 0 && isOffLine == false && locCreated >= 0 && ami_id != "UNKNOWN") {
      b_terminate_me = false;
      // OK. This machine is idling. Let see if it's time to stop this instance:
      if (diff > g_idle_minutes_before_billing_cycle && connect_time_minutes > 50) {
        return_queue["messages"].add("***********************************************************************************");
        return_queue["messages"].add("This machine has been idle for: " + diff + " and we are within billing time");
        return_queue["messages"].add("***********************************************************************************");
        b_terminate_me = true;
      }
      if (diff > g_TimeOutValue) {
          return_queue["messages"].add("***********************************************************************************");
          return_queue["messages"].add("This machine has been idle for: " + diff + " seconds and will be turned off.");
          return_queue["messages"].add("Which is over the default: " + g_TimeOutValue + " seconds, and it is not doing anything right now!");
          return_queue["messages"].add("***********************************************************************************");
          b_terminate_me = true;
      }
      if (b_terminate_me == true){
        // mark temporarily offline, because that call is instant, and it will prevent other jobs from jumping on it:
        aSlave.getComputer().setTemporarilyOffline(true, new hudson.slaves.OfflineCause.ByCLI("groovy_script_killed_me"));
        // "terminate" in this sense is termination of the jenkins slave, not the underlying instance.
        // The slave manager will determine when to start/stop/terminate/create.
        terminate_me = "true";
      }
    }
    // Terminate check: If the computer is offLine and it was already set to die, mark it to die:
    offline_cause = aSlave.getComputer().getOfflineCauseReason();
    if( isOffLine == true && offline_cause == "groovy_script_killed_me" ) {
        terminate_me = "true";
    }
    // Terminate check: If the slave is offLine, and there is nothing running on it, mark it to die:
    if( isOffLine == true && countBusy == 0 && locCreated >= 0 &&
        (offline_cause == "groovy_script_killed_me" || offline_cause == "groovy_script_says_old_ami")) {
        terminate_me = "true";
    }
    // Terminate check: If the AMI does not match, set it offline. When the running job is done, it will be marked to die:
    if ( isOffLine == false && valid_ami_list.size() > 0 && valid_ami_list.contains(ami_id) == false && locCreated >= 0) {
        return_queue["messages"].add("***********************************************************************************");
        return_queue["messages"].add("Running slave is using old ami. Mark offline. It will eventually be killed.");
        return_queue["messages"].add("***********************************************************************************");
        aSlave.getComputer().setTemporarilyOffline(true, new hudson.slaves.OfflineCause.ByCLI("groovy_script_says_old_ami"));
    }
    return_queue["slave_queue"].add(["slaveName":aSlave.name.toString(),
                                     "labels":aSlave.getAssignedLabels().toString(),
                                     "isOffLine": aSlave.getComputer().isOffline().toString(),
                                     "description": aSlave.getNodeDescription().toString(),
                                     "ami_id": ami_id,
                                     "ope_idle_count": ope_idle_count.toString(),
                                     "connectTime": connectTime.toString(),
                                     "idle_seconds": diff.toString(),
                                     "terminate_me": terminate_me])
}
println new JsonBuilder( return_queue ).toPrettyString();
