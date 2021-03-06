from __future__ import absolute_import

from collections import defaultdict
import os
import time
import pickle
from Queue import Queue
import logging
import sys

import mesos.interface
import mesos.native

from mesos.interface import mesos_pb2

from toil.batchSystems.abstractBatchSystem import AbstractBatchSystem
from toil.batchSystems.mesos import ToilJob, ResourceRequirement, TaskData
from toil.batchSystems.mesos.badExecutor import BadMesosExecutor
from toil.batchSystems.mesos.executor import MesosExecutor

log = logging.getLogger(__name__)


class MesosBatchSystem(AbstractBatchSystem, mesos.interface.Scheduler):
    """
    A toil batch system implementation that uses Apache Mesos to distribute toil jobs as Mesos tasks over a
    cluster of slave nodes. A Mesos framework consists of a scheduler and an executor. This class acts as the
    scheduler and is typically run on the master node that also runs the Mesos master process with which the
    scheduler communicates via a driver component. The executor is implemented in a separate class. It is run on each
    slave node and communicates with the Mesos slave process via another driver object. The scheduler may also be run
    on a separate node from the master, which we then call somewhat ambiguously the driver node.
    """

    @staticmethod
    def supportsHotDeployment():
        return True

    def __init__(self, config, maxCpus, maxMemory, maxDisk, masterIP, useBadExecutor=False, userScript=None, toilDistribution=None):
        AbstractBatchSystem.__init__(self, config, maxCpus, maxMemory, maxDisk)
        # The hot-deployed resources representing the user script and the toil distribution respectively. Will be
        # passed along in every Mesos task. See toil.common.HotDeployedResource for details.
        self.userScript = userScript
        self.toilDistribution = toilDistribution

        # Written to when mesos kills tasks, as directed by toil
        self.killedSet = set()

        # Dictionary of queues, which toil assigns jobs to. Each queue represents a batchjob type,
        # defined by resource usage
        self.jobQueueList = defaultdict(list)

        # IP of mesos master. specified in MesosBatchSystem, currently loopback
        self.masterIP = masterIP

        # queue of jobs to kill, by jobID.
        self.killSet = set()

        # Dict of launched jobIDs to TaskData named tuple. Contains start time, executorID, and slaveID.
        self.runningJobMap = {}

        # Queue of jobs whose status has been updated, according to mesos. Req'd by toil
        self.updatedJobsQueue = Queue()

        # Wether to use implicit/explicit acknowledgments
        self.implicitAcknowledgements = self.getImplicit()

        # Reference to the Mesos driver used by this scheduler, to be instantiated in run()
        self.driver = None

        # FIXME: This comment makes no sense to me

        # Returns Mesos executor object, which is merged into Mesos tasks as they are built
        self.executor = self.buildExecutor(bad=useBadExecutor)

        self.nextJobID = 0
        self.lastReconciliation = time.time()
        self.reconciliationPeriod = 120

        # Start the driver
        self._startDriver()

    def issueBatchJob(self, command, memory, cpu, disk):
        """
        Issues the following command returning a unique jobID. Command is the string to run, memory is an int giving
        the number of bytes the batchjob needs to run in and cpu is the number of cpus needed for the batchjob and error-file
        is the path of the file to place any std-err/std-out in.
        """
        # puts batchjob into job_type_queue to be run by Mesos, AND puts jobID in current_job[]
        self.checkResourceRequest(memory, cpu, disk)
        jobID = self.nextJobID
        self.nextJobID += 1

        batchjob = ToilJob(jobID=jobID,
                         resources=ResourceRequirement(memory=memory, cpu=cpu, disk=disk),
                         command=command,
                         userScript=self.userScript,
                         toilDistribution=self.toilDistribution)
        job_type = batchjob.resources

        log.debug("Queueing the batchjob command: %s with batchjob id: %s ..." % (command, str(jobID)))
        self.jobQueueList[job_type].append(batchjob)
        log.debug("... queued")

        return jobID

    def killBatchJobs(self, jobIDs):
        """
        Kills the given batchjob IDs.
        """
        localSet = set()
        if self.driver is None:
            raise RuntimeError("There is no scheduler driver")
        for jobID in jobIDs:
            log.debug("passing tasks to kill to Mesos driver")
            self.killSet.add(jobID)
            localSet.add(jobID)

            if jobID not in self.getIssuedBatchJobIDs():
                self.killSet.remove(jobID)
                localSet.remove(jobID)
                log.debug("Batchjob %s already finished", jobID)
            else:
                taskId = mesos_pb2.TaskID()
                taskId.value = str(jobID)
                self.driver.killTask(taskId)

        while localSet:
            log.debug("in while loop")
            intersection = localSet.intersection(self.killedSet)
            localSet -= intersection
            self.killedSet -= intersection
            if not intersection:
                log.debug("sleeping in the while")
                time.sleep(1)

    def getIssuedBatchJobIDs(self):
        """
        A list of jobs (as jobIDs) currently issued (may be running, or maybe just waiting).
        """
        # TODO: Ensure jobList holds jobs that have been "launched" from Mesos
        jobList = []
        for k, queue in self.jobQueueList.iteritems():
            for item in queue:
                jobList.append(item.jobID)
        for k, v in self.runningJobMap.iteritems():
            jobList.append(k)

        return jobList

    def getRunningBatchJobIDs(self):
        """
        Gets a map of jobs (as jobIDs) currently running (not just waiting) and a how long they have been running for
        (in seconds).
        """
        currentTime = dict()
        for jobID, data in self.runningJobMap.iteritems():
            currentTime[jobID] = time.time() - data.startTime
        return currentTime

    def getUpdatedBatchJob(self, maxWait):
        """
        Gets a batchjob that has updated its status, according to the batchjob manager. Max wait gives the number of seconds to
        pause waiting for a result. If a result is available returns (jobID, exitValue) else it returns None.
        """
        i = self.getFromQueueSafely(self.updatedJobsQueue, maxWait)
        if i is None:
            return None
        jobID, retcode = i
        self.updatedJobsQueue.task_done()
        log.debug("Batchjob updated with code {}".format(retcode))
        return i

    def getWaitDuration(self):
        """
        Gets the period of time to wait (floating point, in seconds) between checking for missing/overlong jobs.
        """
        return self.reconciliationPeriod

    @classmethod
    def getRescueBatchJobFrequency(cls):
        """
        Parasol leaks jobs, but rescuing jobs involves calls to parasol list jobs and pstat2, making it expensive. We
        allow this every 10 minutes..
        """
        return 1800  # Half an hour

    def buildExecutor(self, bad):
        """
        Creates and returns an ExecutorInfo instance representing either the regular or the "bad" test executor.
        """

        # FIXME: This isn't going to work since toil might be installed in a different location on the slaves.
        # FIXME: ... setup.py should install the executor as an entry point which will place it on PATH

        def scriptPath(executorClass):
            path = sys.modules[executorClass.__module__].__file__
            if path.endswith('.pyc'):
                path = path[:-1]
            return path

        executorInfo = mesos_pb2.ExecutorInfo()
        if bad:
            executorInfo.command.value = scriptPath(BadMesosExecutor)
            executorInfo.executor_id.value = "badExecutor"
        else:
            executorInfo.command.value = scriptPath(MesosExecutor)
            executorInfo.executor_id.value = "toilExecutor"
        executorInfo.name = "Test Executor (Python)"
        executorInfo.source = "python_test"
        return executorInfo

    def getImplicit(self):
        """
        Determine whether to run with implicit or explicit acknowledgements.
        """
        implicitAcknowledgements = 1
        if os.getenv("MESOS_EXPLICIT_ACKNOWLEDGEMENTS"):
            log.debug("Enabling explicit status update acknowledgements")
            implicitAcknowledgements = 0

        return implicitAcknowledgements

    def _startDriver(self):
        """
        The Mesos driver thread which handles the scheduler's communication with the Mesos master
        """
        framework = mesos_pb2.FrameworkInfo()
        framework.user = ""  # Have Mesos fill in the current user.
        framework.name = "toil"


        if os.getenv("MESOS_CHECKPOINT"):
            log.debug("Enabling checkpoint for the framework")
            framework.checkpoint = True

        if os.getenv("MESOS_AUTHENTICATE"):
            raise NotImplementedError("Authentication is currently not supported")
        else:
            framework.principal = framework.name
            self.driver = mesos.native.MesosSchedulerDriver(self, framework, self.masterIP,
                                                            self.implicitAcknowledgements)
        assert self.driver.start() == mesos_pb2.DRIVER_RUNNING

    def shutdown(self):
        log.info("Stopping Mesos driver")
        self.driver.stop()
        log.info("Joining Mesos driver")
        driver_result = self.driver.join()
        log.info("Joined Mesos driver")
        if driver_result != mesos_pb2.DRIVER_STOPPED:
            raise RuntimeError("Mesos driver failed with %i", driver_result)

    def registered(self, driver, frameworkId, masterInfo):
        """
        Invoked when the scheduler successfully registers with a Mesos master
        """
        log.debug("Registered with framework ID %s" % frameworkId.value)

    def _sortJobsByResourceReq(self):
        job_types = list(self.jobQueueList.keys())
        # sorts from largest to smallest cpu usage
        # TODO: add a size() method to ResourceSummary and use it as the key. Ask me why.
        job_types.sort(key=lambda resourceRequirement: ResourceRequirement.cpu)
        job_types.reverse()
        return job_types

    def _declineAllOffers(self, driver, offers):
        for offer in offers:
            log.warning("No jobs to assign. Rejecting offer".format(offer.id.value))
            driver.declineOffer(offer.id)

    def _determineOfferResources(self, offer):
        offerCpus = 0
        offerMem = 0
        offerStor = 0
        for resource in offer.resources:
            if resource.name == "cpus":
                offerCpus += resource.scalar.value
            elif resource.name == "mem":
                offerMem += resource.scalar.value
            elif resource.name == "disk":
                offerStor += resource.scalar.value
        return offerCpus, offerMem, offerStor

    def _prepareToRun(self, job_type, offer, index):
        jt_job = self.jobQueueList[job_type][index]  # get the first element to insure FIFO
        task = self._createTask(jt_job, offer)
        return task

    def _deleteByJobID(self, jobID, ):
        # FIXME: not efficient, I'm sure.
        for key, jobType in self.jobQueueList.iteritems():
            for batchjob in jobType:
                if jobID == batchjob.jobID:
                    jobType.remove(batchjob)

    def _updateStateToRunning(self, offer, task):
        self.runningJobMap[int(task.task_id.value)] = TaskData(startTime=time.time(),
                                                               slaveID=offer.slave_id,
                                                               executorID=task.executor.executor_id)
        self._deleteByJobID(int(task.task_id.value))

    def resourceOffers(self, driver, offers):
        """
        Invoked when resources have been offered to this framework.
        """
        job_types = self._sortJobsByResourceReq()

        if len(job_types) == 0 or (len(self.getIssuedBatchJobIDs()) - len(self.getRunningBatchJobIDs()) == 0):
            log.debug("Declining offers")
            # If there are no jobs, we can get stuck with no jobs and no new offers until we decline it.
            self._declineAllOffers(driver, offers)
            return

        # Right now, gives priority to largest jobs
        for offer in offers:
            tasks = []
            # TODO: In an offer, can there ever be more than one resource with the same name?
            offerCpus, offerMem, offerStor = self._determineOfferResources(offer)
            log.debug("Received offer %s with cpus: %s, disk: %s, and mem: %s" \
                      % (offer.id.value, offerCpus, offerStor, offerMem))
            remainingCpus = offerCpus
            remainingMem = offerMem
            remainingStor = offerStor

            for job_type in job_types:
                nextToLaunchIndex = 0
                # Because we are not removing from the list until outside of the while loop, we must decrement the
                # number of jobs left to run ourselves to avoid infinite loop.
                while (len(self.jobQueueList[job_type]) - nextToLaunchIndex > 0) and \
                                remainingCpus >= job_type.cpu and \
                                remainingStor >= self.__bytesToMB(job_type.disk) and \
                                remainingMem >= self.__bytesToMB(job_type.memory):  # toil specifies mem in bytes.

                    task = self._prepareToRun(job_type, offer, nextToLaunchIndex)
                    if int(task.task_id.value) not in self.runningJobMap:
                        # check to make sure task isn't already running (possibly in very unlikely edge case)
                        tasks.append(task)
                        log.info("Preparing to launch Mesos task %s using offer %s..." % (
                            task.task_id.value, offer.id.value))
                        remainingCpus -= job_type.cpu
                        remainingMem -= self.__bytesToMB(job_type.memory)
                        remainingStor -= job_type.disk
                    nextToLaunchIndex += 1

            # If we put the launch call inside the while loop, multiple accepts are used on the same offer.
            driver.launchTasks(offer.id, tasks)

            for task in tasks:
                self._updateStateToRunning(offer, task)
                log.info("...launching Mesos task %s" % task.task_id.value)

            if len(tasks) == 0:
                log.info("Offer not large enough to run any tasks. Required: %s Offered: %s" % (job_types[-1], (offerMem/ 1000000, offerCpus, offerStor/ 1000000)))

    def _createTask(self, jt_job, offer):
        """
        Build the Mesos task object from the toil batchjob here to avoid further cluttering resourceOffers
        """
        task = mesos_pb2.TaskInfo()
        task.task_id.value = str(jt_job.jobID)
        task.slave_id.value = offer.slave_id.value
        task.name = "task %d" % jt_job.jobID

        # assigns toil command to task
        task.data = pickle.dumps(jt_job)

        task.executor.MergeFrom(self.executor)

        cpus = task.resources.add()
        cpus.name = "cpus"
        cpus.type = mesos_pb2.Value.SCALAR
        cpus.scalar.value = jt_job.resources.cpu

        disk = task.resources.add()
        disk.name = "disk"
        disk.type = mesos_pb2.Value.SCALAR
        disk.scalar.value = jt_job.resources.disk / 1000000

        mem = task.resources.add()
        mem.name = "mem"
        mem.type = mesos_pb2.Value.SCALAR
        mem.scalar.value = jt_job.resources.memory / 1000000
        return task

    def __updateState(self, intID, exitStatus):
        self.updatedJobsQueue.put((intID, exitStatus))
        del self.runningJobMap[intID]

    def statusUpdate(self, driver, update):
        """
        Invoked when the status of a task has changed (e.g., a slave is lost and so the task is lost, a task finishes
        and an executor sends a status update saying so, etc). Note that returning from this callback _acknowledges_
        receipt of this status update! If for whatever reason the scheduler aborts during this callback (or the
        process exits) another status update will be delivered (note, however, that this is currently not true if the
        slave sending the status update is lost/fails during that time).
        """
        log.debug("Task %s is in a state %s" % \
                  (update.task_id.value, mesos_pb2.TaskState.Name(update.state)))

        intID = int(update.task_id.value)  # toil keeps jobIds as ints
        stringID = update.task_id.value  # Mesos keeps jobIds as strings

        try:
            self.killSet.remove(intID)
        except KeyError:
            pass
        else:
            self.killedSet.add(intID)

        if update.state == mesos_pb2.TASK_FINISHED:
            self.__updateState(intID, 0)

        if update.state == mesos_pb2.TASK_LOST or \
                        update.state == mesos_pb2.TASK_FAILED or \
                        update.state == mesos_pb2.TASK_KILLED or \
                        update.state == mesos_pb2.TASK_ERROR:
            log.warning("Task %s is in unexpected state %s with message '%s'" \
                        % (stringID, mesos_pb2.TaskState.Name(update.state), update.message))
            self.__updateState(intID, 1)

        # Explicitly acknowledge the update if implicit acknowledgements are not being used.
        if not self.implicitAcknowledgements:
            driver.acknowledgeStatusUpdate(update)

    def frameworkMessage(self, driver, executorId, slaveId, message):
        """
        Invoked when an executor sends a message.
        """
        log.info("Executor {} on slave {} sent a message: {}".format(executorId, slaveId, message))

    def __reconcile(self, driver):
        """
        Queries the master about a list of running tasks. If the master has no knowledge of them, their state will be
        updated to LOST.
        """
        # FIXME: we need additional reconciliation. What about the tasks the master knows about but haven't updated?
        now = time.time()
        if now > self.lastReconciliation + self.reconciliationPeriod:
            self.lastReconciliation = now
            driver.reconcileTasks(list(self.runningJobMap.keys()))

    def reregistered(self, driver, masterInfo):
        """
        Invoked when the scheduler re-registers with a newly elected Mesos master.
        """
        log.debug("Registered with new master")

    def executorLost(self, driver, executorId, slaveId, status):
        """
        Invoked when an executor has exited/terminated.
        """
        log.warning("executor %s lost.".format(executorId))

    @staticmethod
    def __bytesToMB(mem):
        """
        used when converting toil reqs to Mesos reqs
        """
        return mem / 1024 / 1024
