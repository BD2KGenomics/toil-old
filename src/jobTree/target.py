#!/usr/bin/env python

#Copyright (C) 2011 by Benedict Paten (benedictpaten@gmail.com)
#
#Permission is hereby granted, free of charge, to any person obtaining a copy
#of this software and associated documentation files (the "Software"), to deal
#in the Software without restriction, including without limitation the rights
#to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#copies of the Software, and to permit persons to whom the Software is
#furnished to do so, subject to the following conditions:
#
#The above copyright notice and this permission notice shall be included in
#all copies or substantial portions of the Software.
#
#THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
#THE SOFTWARE.

import sys
import os
import importlib
import time
from optparse import OptionParser
import xml.etree.cElementTree as ET
from abc import ABCMeta, abstractmethod
import tempfile
import uuid

try:
    import cPickle 
except ImportError:
    import pickle as cPickle
    
import logging
logger = logging.getLogger( __name__ )

from jobTree.lib.bioio import (setLoggingFromOptions, 
                               getTotalCpuTimeAndMemoryUsage, getTotalCpuTime)
from jobTree.common import setupJobTree, addOptions
from jobTree.leader import mainLoop

class Target(object):
    """
    Represents a unit of work in jobTree. Targets are composed into graphs
    which make up a workflow. 
    
    This public functions of this class and its nested classes are the user API 
    to jobTree. 
    """
    
    __metaclass__ = ABCMeta
    
    def __init__(self, memory=sys.maxint, cpu=sys.maxint):
        """
        This method must be called by any overiding constructor.
        
        Memory is the maximum number of bytes of memory the target will 
        require to run. Cpu is the number of cores required. 
        """
        self.memory = memory
        self.cpu = cpu
        #Private class variables
        
        #See Target.addChild
        self._children = []
        #See Target.addFollowOn
        self._followOns = []
        #A follow-on or child of a target A, is a "successor" of A, if B
        #is a successor of A, then A is a predecessor of B. 
        self._predecessors = set()
        #Variables used for serialisation
        self._dirName, moduleName = self._resolveMainModule(self.__module__)
        self._importStrings = {moduleName + '.' + self.__class__.__name__}
        #See Target.rv()
        self._rvs = {}
    
    @abstractmethod  
    def run(self, fileStore):
        """
        Do user stuff here, including creating any follow on jobs.
        
        The fileStore argument is an instance of Target.FileStore, and can
        be used to create temporary files which can be shared between targets.
        
        The return values of the function can be passed to other targets
        by means of the rv() function. 
        
        If the return value is a tuple, rv(i) would refer to the ith (indexed from 0)
        member of the tuple. If the return value is not a tuple then rV(0) or rV() would
        refer to the return value of the function. 
        
        Note: We disallow return values to be PromisedTargetReturnValue instances 
        (generated by the Target.rv() function - see below). 
        A check is made that will result in a runtime error if you attempt to do this.
        Allowing PromisedTargetReturnValue instances to be returned does not work because
        the mechanism to pass the promise uses a jobStoreFileID that will be deleted once
        the current job and its successors have been completed. This is similar to
        scope rules in a language like C, where returning a reference to memory allocated
        on the stack within a function will produce an undefined reference. 
        Disallowing this also avoids nested promises (PromisedTargetReturnValue 
        instances that contain other PromisedTargetReturnValue). 
        """
        raise NotImplementedError()
    
    def addChild(self, childTarget):
        """
        Adds the child target to be run as child of this target. Returns childTarget.
        Child targets are run after the Target.run method has completed.
        
        See Target.checkTargetGraphAcylic for formal definition of allowed forms of
        target graph.
        """
        self._children.append(childTarget)
        childTarget._addPredecessor(self)
        return childTarget
    
    def addFollowOn(self, followOnTarget):
        """
        Adds a follow-on target, follow-on targets will be run
        after the child targets and their descendants have been run. 
        Returns followOnTarget.
        
        See Target.checkTargetGraphAcylic for formal definition of allowed forms of
        target graph.
        """
        self._followOns.append(followOnTarget)
        followOnTarget._addPredecessor(self)
        return followOnTarget
        
    ##Convenience functions for creating targets
    
    def addChildFn(self, fn, *args, **kwargs):
        """
        Adds a child fn. See FunctionWrappingTarget. 
        Returns the new child Target.
        """
        return self.addChild(FunctionWrappingTarget(fn, *args, **kwargs))

    def addChildTargetFn(self, fn, *args, **kwargs):
        """
        Adds a child target fn. See TargetFunctionWrappingTarget. 
        Returns the new child Target.
        """
        return self.addChild(TargetFunctionWrappingTarget(fn, *args, **kwargs)) 
    
    def addFollowOnFn(self, fn, *args, **kwargs):
        """
        Adds a follow-on fn. See FunctionWrappingTarget. 
        Returns the new follow-on Target.
        """
        return self.addFollowOn(FunctionWrappingTarget(fn, *args, **kwargs))

    def addFollowOnTargetFn(self, fn, *args, **kwargs):
        """
        Add a follow-on target fn. See TargetFunctionWrappingTarget. 
        Returns the new follow-on Target.
        """
        return self.addFollowOn(TargetFunctionWrappingTarget(fn, *args, **kwargs)) 
    
    @staticmethod
    def wrapTargetFn(fn, *args, **kwargs):
        """
        Makes a Target out of a target function.
        
        Convenience function for constructor of TargetFunctionWrappingTarget
        """
        return TargetFunctionWrappingTarget(fn, *args, **kwargs)
 
    @staticmethod
    def wrapFn(fn, *args, **kwargs):
        """
        Makes a Target out of a function.
        
        Convenience function for constructor of FunctionWrappingTarget
        """
        return FunctionWrappingTarget(fn, *args, **kwargs)
    
    ####################################################
    #The following function is used for passing return values between 
    #target run functions
    ####################################################
    
    def rv(self, argIndex=0):
        """
        Gets a PromisedTargetReturnValue, representing the argIndex return 
        value of the run function (see run method for description).
        This PromisedTargetReturnValue, if a class attribute of a Target instance, 
        call it T, will be replaced by the actual return value just before the 
        run function of T is called. The function rv therefore allows the output 
        from one Target to be wired as input to another Target before either 
        is actually run.  
        """
        #Check if the return value has already been promised and if it has
        #return it
        if argIndex in self._rvs:
            return self._rvs[argIndex]
        #Create, store, return new PromisedTargetReturnValue
        self._rvs[argIndex] = PromisedTargetReturnValue()
        return self._rvs[argIndex]
    
    ####################################################
    #Cycle checking
    ####################################################
    
    def checkTargetGraphAcylic(self):
        """ 
        Raises a RuntimeError exception if the target graph rooted at this target 
        contains any cycles of child/followOn dependencies in the augmented target graph
        (see below). Such cycles are not allowed in valid target graphs.
        This function is run during execution.
        
        A target B that is on a directed path of child/followOn edges from a 
        target A in the target graph is a descendant of A, 
        similarly A is an ancestor of B.
        
        A follow-on edge (A, B) between two targets A and B is equivalent 
        to adding a child edge to B from (1) A, (2) from each child of A, 
        and (3) from the descendants of each child of A. We
        call such an edge an "implied" edge. The augmented target graph is a 
        target graph including all the implied edges. 

        For a target (V, E) the algorithm is O(|V|^2). It is O(|V| + |E|) for 
        a graph with no follow-ons. The former follow on case could be improved!
        """
        #Get implied edges
        extraEdges = self._getImpliedEdges()
            
        #Check for directed cycles in the augmented graph
        self._checkTargetGraphAcylicDFS([], set(), extraEdges)
    
    ####################################################
    #The following nested classes are used for
    #creating job trees (Target.Runner) and 
    #managing temporary files (Target.FileStore)
    ####################################################
    
    class Runner(object):
        """
        Used to setup and run a graph of targets.
        """
    
        @staticmethod
        def getDefaultOptions():
            """
            Returns an optparse.Values object of the 
            options used by a jobTree. 
            """
            parser = OptionParser()
            Target.Runner.addJobTreeOptions(parser)
            options, args = parser.parse_args(args=[])
            assert len(args) == 0
            return options
            
        @staticmethod
        def addJobTreeOptions(parser):
            """
            Adds the default jobTree options to an optparse or argparse
            parser object.
            """
            addOptions(parser)
    
        @staticmethod
        def startJobTree(target, options):
            """
            Runs the jobtree using the given options (see Target.Runner.getDefaultOptions
            and Target.Runner.addJobTreeOptions) starting with this target.
            
            Raises an exception if the given jobTree already exists. 
            """
            setLoggingFromOptions(options)
            config, batchSystem, jobStore = setupJobTree(options)
            jobStore.clean()
            if "rootJob" not in config.attrib: #No jobs have yet been run
                #Setup the first job.
                rootJob = target._serialiseFirstTarget(jobStore)
            else:
                rootJob = jobStore.load(config.attrib["rootJob"])
            return mainLoop(config, batchSystem, jobStore, rootJob)
        
        @staticmethod
        def cleanup(options):
            """
            Removes the jobStore backing the jobTree.
            """
            config, batchSystem, jobStore = setupJobTree(options)
            jobStore.deleteJobStore()
            
    class FileStore:
        """
        Class used to manage temporary files and log messages, 
        passed as argument to the Target.run method.
        """
        
        def __init__(self, jobStore, job, localTempDir):
            """
            This constructor should not be called by the user, 
            FileStore instances are only provided as arguments 
            to the run function.
            """
            self.jobStore = jobStore
            self.job = job
            self.localTempDir = localTempDir
            self.loggingMessages = []
        
        def writeGlobalFile(self, localFileName):
            """
            Takes a file (as a path) and uploads it to to the global file store, returns
            an ID that can be used to retrieve the file. 
            """
            return self.jobStore.writeFile(self.job.jobStoreID, localFileName)
        
        def updateGlobalFile(self, fileStoreID, localFileName):
            """
            Replaces the existing version of a file in the global file store, 
            keyed by the fileStoreID. 
            Throws an exception if the file does not exist.
            """
            self.jobStore.updateFile(fileStoreID, localFileName)
        
        def readGlobalFile(self, fileStoreID, localFilePath=None):
            """
            Returns a path to a local copy of the file keyed by fileStoreID. 
            The version will be consistent with the last copy of the file 
            written/updated to the global file store. If localFilePath is not None, 
            the returned file path will be localFilePath.
            """
            if localFilePath is None:
                fd, localFilePath = tempfile.mkstemp(dir=self.getLocalTempDir())
                self.jobStore.readFile(fileStoreID, localFilePath)
                os.close(fd)
            else:
                self.jobStore.readFile(fileStoreID, localFilePath)
            return localFilePath
        
        def deleteGlobalFile(self, fileStoreID):
            """
            Deletes a global file with the given fileStoreID. Returns true if 
            file exists, else false.
            """
            return self.jobStore.deleteFile(fileStoreID)
        
        def writeGlobalFileStream(self):
            """
            Similar to writeGlobalFile, but returns a context manager yielding a 
            tuple of 1) a file handle which can be written to and 2) the ID of 
            the resulting file in the job store. The yielded file handle does 
            not need to and should not be closed explicitly.
            """
            return self.jobStore.writeFileStream(self.job.jobStoreID)
        
        def updateGlobalFileStream(self, fileStoreID):
            """
            Similar to updateGlobalFile, but returns a context manager yielding 
            a file handle which can be written to. The yielded file handle does 
            not need to and should not be closed explicitly.
            """
            return self.jobStore.updateFileStream(fileStoreID)
        
        def getEmptyFileStoreID(self):
            """
            Returns the ID of a new, empty file.
            """
            return self.jobStore.getEmptyFileStoreID(self.job.jobStoreID)
        
        def readGlobalFileStream(self, fileStoreID):
            """
            Similar to readGlobalFile, but returns a context manager yielding a 
            file handle which can be read from. The yielded file handle does not 
            need to and should not be closed explicitly.
            """
            return self.jobStore.readFileStream(fileStoreID)
           
        def getLocalTempDir(self):
            """
            Get the local temporary directory.
            """
            return self.localTempDir
        
        def logToMaster(self, string):
            """
            Send a logging message to the leader. Will only ne reported if logging 
            is set to INFO level (or lower) in the leader.
            """
            self.loggingMessages.append(str(string))

    ####################################################
    #Private functions
    ####################################################
    
    def _addPredecessor(self, predecessorTarget):
        """
        Adds a predecessor target to the set of predecessor targets. Raises a 
        RuntimeError is the target is already a predecessor.
        """
        if predecessorTarget in self._predecessors:
            raise RuntimeError("The given target is already a predecessor of this target")
        self._predecessors.add(predecessorTarget)

    ####################################################
    #The following functions are used to serialise
    #a target graph to the jobStore
    ####################################################
    
    def _getHashOfTargetsToUUIDs(self, targetsToUUIDs):
        """
        Creates a map of the targets in the graph to randomly selected UUIDs.
        Excludes the root target.
        """
        #Call recursively
        for successor in self._children + self._followOns:
            successor._getHashOfTargetsToUUIDs2(targetsToUUIDs)
        return targetsToUUIDs
        
    def _getHashOfTargetsToUUIDs2(self, targetsToUUIDs):
        if self not in targetsToUUIDs:
            targetsToUUIDs[self] = str(uuid.uuid1())
            self._getHashOfTargetsToUUIDs(targetsToUUIDs)
           
    def _createEmptyJobForTarget(self, jobStore, updateID=None, command=None, 
                                 predecessorNumber=0):
        """
        Create an empty job for the target.
        """
        return jobStore.create(command=command, 
                               memory=(self.memory if self.memory != sys.maxint 
                                       else float(jobStore.config.attrib["default_memory"])),
                               cpu=(self.cpu if self.cpu != sys.maxint
                                    else float(jobStore.config.attrib["default_cpu"])),
                               updateID=updateID, predecessorNumber=predecessorNumber)
        
    def _makeJobWrappers(self, jobStore, targetsToUUIDs, targetsToJobs, predecessor):
        """
        Creates a job for each target in the target graph, recursively.
        """
        if self not in targetsToJobs:
            #The job for the target
            assert predecessor in self._predecessors
            job = self._createEmptyJobForTarget(jobStore, targetsToUUIDs[self],
                                                predecessorNumber=len(self._predecessors))
            targetsToJobs[self] = job
            
            #Add followOns/children to be run after the current target.
            for successors in (self._followOns, self._children):
                jobs = map(lambda successor:
                    successor._makeJobWrappers(jobStore, targetsToUUIDs, 
                                               targetsToJobs, self), successors)
                if len(jobs) > 0:
                    job.stack.append(jobs)
            
            #Pickle the target so that its run method can be run at a later time.
            #Drop out the children/followOns/predecessors - which are all recored
            #within the jobStore and do not need to be stored within the target
            self._children = []
            self._followOns = []
            self._predecessors = set()
            #The pickled target is "run" as the command of the job, see worker
            #for the mechanism which unpickles the target and executes the Target.run
            #method.
            fileStoreID = jobStore.getEmptyFileStoreID(job.jobStoreID)
            with jobStore.writeFileStream(job.jobStoreID) as ( fileHandle, fileStoreID ):
                cPickle.dump(self, fileHandle, cPickle.HIGHEST_PROTOCOL)
            job.command = "scriptTree %s %s %s" % (fileStoreID, self._dirName, 
                                                   " ".join(set( self._importStrings )))
            #Update the status of the job on disk
            jobStore.update(job)
        else:
            #Lookup the already created job
            job = targetsToJobs[self]
            assert job.predecessorNumber > 1
        
        #The return is a tuple stored within the job.stack of the jobs to run.
        #The tuple is jobStoreID, memory, cpu, predecessorID
        #The predecessorID is used to establish which predecessors have been
        #completed before running the given Target - it is just a unique ID
        #per predecessor 
        return (job.jobStoreID, job.memory, job.cpu, 
                None if job.predecessorNumber <= 1 else str(uuid.uuid4()))
    
    def _serialiseTargetGraph(self, job, jobStore):
        """
        Serialises the graph of targets rooted at this target, 
        storing them in the jobStore.
        Assumes the root target is already in the jobStore.
        """
        #Create jobIDs as UUIDs
        targetsToUUIDs = self._getHashOfTargetsToUUIDs({})
        #Set the jobs to delete
        job.jobsToDelete = list(targetsToUUIDs.values())
        #Update the job on disk. The jobs to delete is a record of what to
        #remove if the update goes wrong
        jobStore.update(job)
        #Create the jobs for followOns/children
        targetsToJobs = {}
        for successors in (self._followOns, self._children):
            jobs = map(lambda successor:
                successor._makeJobWrappers(jobStore, targetsToUUIDs, 
                                           targetsToJobs, self), successors)
            if len(jobs) > 0:
                job.stack.append(jobs)
        #Remove the jobs to delete list and remove the old command finishing the update
        job.jobsToDelete = []
        job.command = None
        jobStore.update(job)
        
    def _serialiseFirstTarget(self, jobStore):
        """
        Serialises the root target. Returns the wrapping job.
        """
        #Pickles the target within a shared file in the jobStore called 
        #"firstTarget"
        sharedTargetFile = "firstTarget"
        with jobStore.writeSharedFileStream(sharedTargetFile) as f:
            cPickle.dump(self, f, cPickle.HIGHEST_PROTOCOL)
        #Make the first job
        job = self._createEmptyJobForTarget(jobStore,
            command="scriptTree %s %s %s" % (sharedTargetFile, self._dirName, 
                    " ".join(set( self._importStrings ))))
        #Set the config rootJob attrib
        assert "rootJob" not in jobStore.config.attrib
        jobStore.config.attrib["rootJob"] = job.jobStoreID
        with jobStore.writeSharedFileStream("config.xml") as f:
            ET.ElementTree( jobStore.config ).write(f)
        #Return the first job
        return job

    ####################################################
    #Functions to pass Target.run return values to the 
    #input arguments of other Target instances
    ####################################################
   
    def _switchOutPromisedTargetReturnValues(self, jobStore):
        """
        Replaces each PromisedTargetReturnValue instance that is a class 
        attribute of the target with PromisedTargetReturnValue's stored value.
        Will do this also for PromisedTargetReturnValue instances within lists, 
        tuples, sets or dictionaries that are class attributes of the Target.
        
        This function is called just before the run method.
        """
        #Iterate on the class attributes of the Target instance.
        for attr, value in self.__dict__.iteritems():
            #If the variable is a PromisedTargetReturnValue replace with the 
            #actual stored return value of the PromisedTargetReturnValue
            #else if the variable is a list, tuple or set or dict replace any 
            #PromisedTargetReturnValue instances within
            #the container with the stored return value.
            f = lambda : map(lambda x : x.loadValue(jobStore) if 
                        isinstance(x, PromisedTargetReturnValue) else x, value)
            if isinstance(value, PromisedTargetReturnValue):
                self.__dict__[attr] = value.loadValue(jobStore)
            elif isinstance(value, list):
                self.__dict__[attr] = f()
            elif isinstance(value, tuple):
                self.__dict__[attr] = tuple(f())
            elif isinstance(value, set):
                self.__dict__[attr] = set(f())
            elif isinstance(value, dict):
                self.__dict__[attr] = dict(map(lambda x : (x, value[x].loadValue(jobStore) if 
                        isinstance(x, PromisedTargetReturnValue) else value[x]), value))
      
    def _setFileIDsForPromisedValues(self, jobStore, jobStoreID, visited):
        """
        Sets the jobStoreFileID for each PromisedTargetReturnValue in the 
        graph of targets created.
        """
        #Replace any None references with valid jobStoreFileIDs. We 
        #do this here, rather than within the original constructor of the
        #promised value because we don't necessarily have access to the jobStore when 
        #the PromisedTargetReturnValue instances are created.
        if self not in visited:
            visited.add(self)
            for PromisedTargetReturnValue in self._rvs.values():
                if PromisedTargetReturnValue.jobStoreFileID == None:
                    PromisedTargetReturnValue.jobStoreFileID = jobStore.getEmptyFileStoreID(jobStoreID)
            #Now recursively do the same for the children and follow ons.
            for successorTarget in self._children + self._followOns:
                successorTarget._setFileIDsForPromisedValues(jobStore, jobStoreID, visited)
    
    @staticmethod
    def _setReturnValuesForPromises(target, returnValues, jobStore):
        """
        Sets the values for promises using the return values from the target's
        run function.
        """
        for i in target._rvs.keys():
            if isinstance(returnValues, tuple):
                argToStore = returnValues[i]
            else:
                if i != 0:
                    raise RuntimeError("Referencing return value index (%s)"
                                " that is out of range: %s" % (i, returnValues))
                argToStore = returnValues
            target._rvs[i]._storeValue(argToStore, jobStore)
    
    ####################################################
    #Functions associated with Target.checkTargetGraphAcyclic to establish 
    #that the target graph does not contain any cycles of dependencies. 
    ####################################################
        
    def _dfs(self, visited):
        """Adds the target and all targets reachable on a directed path from current 
        node to the set 'visited'.
        """
        if self not in visited:
            visited.add(self) 
            for successor in self._children + self._followOns:
                successor._dfs(visited)
        
    def _checkTargetGraphAcylicDFS(self, stack, visited, extraEdges):
        """
        DFS traversal to detect cycles in augmented target graph.
        """
        if self not in visited:
            visited.add(self) 
            stack.append(self)
            for successor in self._children + self._followOns + extraEdges[self]:
                successor._checkTargetGraphAcylicDFS(stack, visited, extraEdges)
            assert stack.pop() == self
        if self in stack:
            raise RuntimeError("Detected cycle in augmented target graph: %s" % stack)
        
    def _getImpliedEdges(self):
        """
        Gets the set of implied edges. See Target.checkTargetGraphAcylic
        """
        #Get nodes in target graph
        nodes = set()
        self._dfs(nodes)
        
        ##For each follow-on edge calculate the extra implied edges
        #Adjacency list of implied edges, i.e. map of targets to lists of targets 
        #connected by an implied edge 
        extraEdges = dict(map(lambda n : (n, []), nodes))
        for target in nodes:
            if len(target._followOns) > 0:
                #Get set of targets connected by a directed path to target, starting
                #with a child edge
                reacheable = set()
                for child in target._children:
                    child._dfs(reacheable)
                #Now add extra edges
                for descendant in reacheable:
                    extraEdges[descendant] += target._followOns[:]
        return extraEdges 
    
    ####################################################
    #Function which worker calls to ultimately invoke
    #a targets Target.run method, and then handle created
    #children/followOn targets
    ####################################################
       
    def _execute(self, job, stats, localTempDir, jobStore):
        """This is the core method for running the target within a worker.
        """ 
        if stats != None:
            startTime = time.time()
            startClock = getTotalCpuTime()
        
        baseDir = os.getcwd()
        #Switch out any promised return value instances with the actual values
        self._switchOutPromisedTargetReturnValues(jobStore)
        #Run the target, first cleanup then run.
        fileStore = Target.FileStore(jobStore, job, localTempDir)
        returnValues = self.run(fileStore)
        #Check if the target graph has created
        #any cycles of dependencies 
        self.checkTargetGraphAcylic()
        #Set the promised value jobStoreFileIDs
        self._setFileIDsForPromisedValues(jobStore, job.jobStoreID, set())
        #Store the return values for any promised return value
        self._setReturnValuesForPromises(self, returnValues, jobStore)
        #Turn the graph into a graph of jobs in the jobStore
        self._serialiseTargetGraph(job, jobStore)
        #Change dir back to cwd dir, if changed by target (this is a safety issue)
        if os.getcwd() != baseDir:
            os.chdir(baseDir)
        #Finish up the stats
        if stats != None:
            stats = ET.SubElement(stats, "target")
            stats.attrib["time"] = str(time.time() - startTime)
            totalCpuTime, totalMemoryUsage = getTotalCpuTimeAndMemoryUsage()
            stats.attrib["clock"] = str(totalCpuTime - startClock)
            stats.attrib["class"] = ".".join((self.__class__.__name__,))
            stats.attrib["memory"] = str(totalMemoryUsage)
        #Return any logToMaster logging messages
        return fileStore.loggingMessages
    
    ####################################################
    #Method used to resolve the module in which an inherited target instances
    #class is defined
    ####################################################
    
    @staticmethod
    def _resolveMainModule( moduleName ):
        """
        Returns a tuple of two elements, the first element being the path 
        to the directory containing the given
        module and the second element being the name of the module. 
        If the given module name is "__main__",
        then that is translated to the actual file name of the top-level 
        script without .py or .pyc extensions. The
        caller can then add the first element of the returned tuple to 
        sys.path and load the module from there. See also worker.loadTarget().
        """
        # looks up corresponding module in sys.modules, gets base name, drops .py or .pyc
        moduleDirPath, moduleName = os.path.split(os.path.abspath(sys.modules[moduleName].__file__))
        if moduleName.endswith('.py'):
            moduleName = moduleName[:-3]
        elif moduleName.endswith('.pyc'):
            moduleName = moduleName[:-4]
        else:
            raise RuntimeError(
                "Can only handle main modules loaded from .py or .pyc files, but not '%s'" %
                moduleName)
        return moduleDirPath, moduleName

class FunctionWrappingTarget(Target):
    """
    Target used to wrap a function.
    
    Function can not be nested function or class function, currently.
    *args and **kwargs are used as the arguments to the function.
    """
    def __init__(self, fn, *args, **kwargs):
        cpu = kwargs.pop("cpu") if "cpu" in kwargs else sys.maxint
        memory = kwargs.pop("memory") if "memory" in kwargs else sys.maxint
        Target.__init__(self, memory=memory, cpu=cpu)
        self.fnModuleDirPath, self.fnModuleName = self._resolveMainModule(fn.__module__)
        self.fnName = str(fn.__name__)
        self._args=args
        self._kwargs=kwargs
        
    def _getFunc( self ):
        if self.fnModuleDirPath not in sys.path:
            sys.path.append( self.fnModuleDirPath )
        return getattr( importlib.import_module( self.fnModuleName ), self.fnName )

    def run(self, fileStor):
        func = self._getFunc( )
        #Now run the wrapped function
        return func(*self._args, **self._kwargs)

class TargetFunctionWrappingTarget(FunctionWrappingTarget):
    """
    Target used to wrap a function.
    A target function is a function which takes as its first argument a reference
    to the wrapping target. 
    
    To enable the target function to get access to the Target.FileStore
    instance (see Target.Run), it is made a variable of the wrapping target, so in the wrapped
    target function the attribute "fileStore" of the first argument (the target) is
    an instance of the Target.FileStore class. 
    """
    def run(self, fileStore):
        func = self._getFunc( )
        self.fileStore = fileStore
        return func(*((self,) + tuple(self._args)), **self._kwargs)

class PromisedTargetReturnValue():
    """
    References a return value from a Target's run function. Let T be a target. 
    Instances of PromisedTargetReturnValue are created by
    T.rv(i), where i is an integer reference to a return value of T's run function
    (casting the return value as a tuple). 
    When passed to the constructor of a different Target the PromisedTargetReturnValue
    will be replaced by the actual referenced return value after the Target's run function 
    has finished (see Target._switchOutPromisedTargetReturnValues). 
    This mechanism allows a return values from one Target's run method to be input
    argument to Target before the former Target's run function has been executed.
    """ 
    def __init__(self):
        self.jobStoreFileID = None #The None value is
        #replaced with a real jobStoreFileID by the Target object.
        
    def loadValue(self, jobStore):
        """
        Unpickles the promised value and returns it. 
        """
        assert self.jobStoreFileID != None 
        with jobStore.readFileStream(self.jobStoreFileID) as fileHandle:
            value = cPickle.load(fileHandle) #If this doesn't work, then it is likely the Target that is promising value has not yet been run.
            if isinstance(value, PromisedTargetReturnValue):
                raise RuntimeError("A nested PromisedTargetReturnValue has been found.") #We do not allow the return of PromisedTargetReturnValue instance from the run function
            return value

    def _storeValue(self, valueToStore, jobStore):
        """
        Pickle the promised value. This is done by the target.
        """
        assert self.jobStoreFileID != None
        with jobStore.updateFileStream(self.jobStoreFileID) as fileHandle:
            cPickle.dump(valueToStore, fileHandle, cPickle.HIGHEST_PROTOCOL)
