from contextlib import contextmanager
import logging
import marshal as pickler
#import cPickle as pickler
#import pickle as pickler
#import json as pickler    
import random
import shutil
import os
import tempfile
from toil.lib.bioio import absSymPath
from toil.jobStores.abstractJobStore import AbstractJobStore, NoSuchJobException, \
    NoSuchFileException
from toil.batchJob import BatchJob

logger = logging.getLogger( __name__ )

class FileJobStore(AbstractJobStore):
    """Represents the toil using a network file system. For doc-strings
    of functions see AbstractJobStore.
    """

    def __init__(self, jobStoreDir, config=None):
        #This is root directory in which everything in the store is kept
        self.jobStoreDir = absSymPath(jobStoreDir)
        logger.info("Jobstore directory is: %s", self.jobStoreDir)
        self.tempFilesDir = os.path.join(self.jobStoreDir, "tmp")
        if not os.path.exists(self.jobStoreDir):
            os.mkdir(self.jobStoreDir)
            os.mkdir(self.tempFilesDir)
        #Parameters for creating temporary files
        self.validDirs = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        self.levels = 2
        super( FileJobStore, self ).__init__( config=config )
        
    def deleteJobStore(self):
        if os.path.exists(self.jobStoreDir):
            shutil.rmtree(self.jobStoreDir)
    
    ##########################################
    #The following methods deal with creating/loading/updating/writing/checking for the
    #existence of jobs
    ########################################## 
    
    def create(self, command, memory, cpu, disk, updateID=None,
               predecessorNumber=0):
        #The absolute path to the batchjob directory.
        absJobDir = tempfile.mkdtemp(prefix="batchjob", dir=self._getTempSharedDir())
        #Sub directory to put temporary files associated with the batchjob in
        os.mkdir(os.path.join(absJobDir, "g"))
        #Make the batchjob
        batchjob = BatchJob(command=command, memory=memory, cpu=cpu, disk=disk,
                  jobStoreID=self._getRelativePath(absJobDir), 
                  remainingRetryCount=self._defaultTryCount( ), 
                  updateID=updateID,
                  predecessorNumber=predecessorNumber)
        #Write batchjob file to disk
        self.update(batchjob)
        return batchjob
    
    def exists(self, jobStoreID):
        return os.path.exists(self._getJobFileName(jobStoreID))
    
    def getPublicUrl( self,  jobStoreFileID):
        self._checkJobStoreFileID(jobStoreFileID)
        jobStorePath = self._getAbsPath(jobStoreFileID)
        if os.path.exists(jobStorePath):
            return 'file:'+jobStorePath
        else:
            raise NoSuchFileException(jobStoreFileID)

    def getSharedPublicUrl( self,  FileName):
        jobStorePath = self.jobStoreDir+'/'+FileName
        if os.path.exists(jobStorePath):
            return 'file:'+jobStorePath
        else:
            raise NoSuchFileException(FileName)

    def load(self, jobStoreID):
        self._checkJobStoreId(jobStoreID)
        #Load a valid version of the batchjob
        jobFile = self._getJobFileName(jobStoreID)
        with open(jobFile, 'r') as fileHandle:
            batchjob = BatchJob.fromDict(pickler.load(fileHandle))
        #The following cleans up any issues resulting from the failure of the 
        #batchjob during writing by the batch system.
        if os.path.isfile(jobFile + ".new"):
            logger.warn("There was a .new file for the batchjob: %s", jobStoreID)
            os.remove(jobFile + ".new")
            batchjob.setupJobAfterFailure(self.config)
        return batchjob
    
    def update(self, batchjob):
        #The batchjob is serialised to a file suffixed by ".new"
        #The file is then moved to its correct path.
        #Atomicity guarantees use the fact the underlying file systems "move"
        #function is atomic. 
        with open(self._getJobFileName(batchjob.jobStoreID) + ".new", 'w') as f:
            pickler.dump(batchjob.toDict(), f)
        #This should be atomic for the file system
        os.rename(self._getJobFileName(batchjob.jobStoreID) + ".new", self._getJobFileName(batchjob.jobStoreID))
    
    def delete(self, jobStoreID):
        #The jobStoreID is the relative path to the directory containing the batchjob,
        #removing this directory deletes the batchjob.
        if self.exists(jobStoreID):
            shutil.rmtree(self._getAbsPath(jobStoreID))
 
    def jobs(self):
        #Walk through list of temporary directories searching for jobs
        for tempDir in self._tempDirectories():
            for i in os.listdir(tempDir):
                if i.startswith( 'batchjob' ):
                    yield self.load(self._getRelativePath(os.path.join(tempDir, i)))
 
    ##########################################
    #Functions that deal with temporary files associated with jobs
    ##########################################    
    
    def writeFile(self, jobStoreID, localFilePath):
        self._checkJobStoreId(jobStoreID)
        fd, absPath = self._getJobTempFile(jobStoreID)
        shutil.copyfile(localFilePath, absPath)
        os.close(fd)
        return self._getRelativePath(absPath)

    def updateFile(self, jobStoreFileID, localFilePath):
        self._checkJobStoreFileID(jobStoreFileID)
        shutil.copyfile(localFilePath, self._getAbsPath(jobStoreFileID))
    
    def readFile(self, jobStoreFileID, localFilePath):
        self._checkJobStoreFileID(jobStoreFileID)
        shutil.copyfile(self._getAbsPath(jobStoreFileID), localFilePath)
    
    def deleteFile(self, jobStoreFileID):
        if not self.fileExists(jobStoreFileID):
            return
        os.remove(self._getAbsPath(jobStoreFileID))
        
    def fileExists(self, jobStoreFileID):
        absPath = self._getAbsPath(jobStoreFileID)
        if not os.path.exists(absPath):
            return False
        if not os.path.isfile(absPath):
            raise NoSuchFileException("Path %s is not a file in the jobStore" % jobStoreFileID) 
        return True
    
    @contextmanager
    def writeFileStream(self, jobStoreID):
        self._checkJobStoreId(jobStoreID)
        fd, absPath =  self._getJobTempFile(jobStoreID)
        with open(absPath, 'w') as f:
            yield f, self._getRelativePath(absPath)
        os.close(fd) #Close the os level file descript

    @contextmanager
    def updateFileStream(self, jobStoreFileID):
        self._checkJobStoreFileID(jobStoreFileID)
        # File objects are context managers (CM) so we could simply return what open returns.
        # However, it is better to wrap it in another CM so as to prevent users from accessing
        # the file object directly, without a with statement.
        with open(self._getAbsPath(jobStoreFileID), 'w') as f:
            yield f
    
    def getEmptyFileStoreID(self, jobStoreID):
        with self.writeFileStream(jobStoreID) as ( fileHandle, jobStoreFileID ):
            return jobStoreFileID
    
    @contextmanager
    def readFileStream(self, jobStoreFileID):
        self._checkJobStoreFileID(jobStoreFileID)
        with open(self._getAbsPath(jobStoreFileID), 'r') as f:
            yield f
            
    ##########################################
    #The following methods deal with shared files, i.e. files not associated 
    #with specific jobs.
    ##########################################  

    @contextmanager
    def writeSharedFileStream(self, sharedFileName):
        assert self._validateSharedFileName( sharedFileName )
        with open( os.path.join( self.jobStoreDir, sharedFileName ), 'w' ) as f:
            yield f

    @contextmanager
    def readSharedFileStream(self, sharedFileName):
        assert self._validateSharedFileName( sharedFileName )
        with open(os.path.join(self.jobStoreDir, sharedFileName), 'r') as f:
            yield f
             
    def writeStatsAndLogging(self, statsAndLoggingString):
        #Temporary files are placed in the set of temporary files/directoies
        fd, tempStatsFile = tempfile.mkstemp(prefix="stats", suffix=".new", dir=self._getTempSharedDir())
        with open(tempStatsFile, "w") as f:
            f.write(statsAndLoggingString)
        os.close(fd)
        os.rename(tempStatsFile, tempStatsFile[:-4]) #This operation is atomic
        
    def readStatsAndLogging( self, statsAndLoggingCallBackFn):
        numberOfFilesProcessed = 0
        for tempDir in self._tempDirectories():
            for tempFile in os.listdir(tempDir):
                if tempFile.startswith( 'stats' ):
                    absTempFile = os.path.join(tempDir, tempFile)
                    if not tempFile.endswith( '.new' ):
                        with open(absTempFile, 'r') as fH:
                            statsAndLoggingCallBackFn(fH)
                        numberOfFilesProcessed += 1
                    os.remove(absTempFile)
        return numberOfFilesProcessed
    
    ##########################################
    #Private methods
    ##########################################   
        
    def _getAbsPath(self, relativePath):
        """
        :rtype : string, string is the absolute path to a file path relative
        to the self.tempFilesDir.
        """
        return os.path.join(self.tempFilesDir, relativePath)
    
    def _getRelativePath(self, absPath):
        """
        absPath  is the absolute path to a file in the store,.
        
        :rtype : string, string is the path to the absPath file relative to the 
        self.tempFilesDir
        
        """
        return absPath[len(self.tempFilesDir)+1:]
    
    def _getJobFileName(self, jobStoreID):
        """
        :rtype : string, string is the file containing the serialised Batchjob.Batchjob instance
        for the given batchjob.
        """
        return os.path.join(self._getAbsPath(jobStoreID), "batchjob")

    def _getJobTempFile(self, jobStoreID):
        """
        :rtype : file-descriptor, string, string is absolute path to a temporary file within
        the given batchjob's (referenced by jobStoreID's) temporary file directory. The file-descriptor
        is integer pointing to open operating system file handle. Should be closed using os.close()
        after writing some material to the file.
        """
        fD, absPath = tempfile.mkstemp(suffix=".tmp", 
                                dir=os.path.join(self._getAbsPath(jobStoreID), "g"))
        return fD, absPath
    
    def _checkJobStoreId(self, jobStoreID):
        """
        Raises a NoSuchJobException if the jobStoreID does not exist.
        """
        if not self.exists(jobStoreID):
            raise NoSuchJobException("JobStoreID %s does not exist" % jobStoreID)
    
    def _checkJobStoreFileID(self, jobStoreFileID):
        """
        Raises NoSuchFileException if the jobStoreFileID does not exist or is not a file.
        """
        absPath = os.path.join(self.tempFilesDir, jobStoreFileID)
        if not os.path.exists(absPath):
            raise NoSuchFileException("File %s does not exist in jobStore" % jobStoreFileID)
        if not os.path.isfile(absPath):
            raise NoSuchFileException("Path %s is not a file in the jobStore" % jobStoreFileID) 
    
    def _getTempSharedDir(self):
        """
        Gets a temporary directory in the hierarchy of directories in self.tempFilesDir.
        This directory may contain multiple shared jobs/files.
        
        :rtype : string, path to temporary directory in which to place files/directories.
        """
        tempDir = self.tempFilesDir
        for i in xrange(self.levels):
            tempDir = os.path.join(tempDir, random.choice(self.validDirs))
            if not os.path.exists(tempDir):
                try:
                    os.mkdir(tempDir)
                except os.error:
                    if not os.path.exists(tempDir): #In the case that a collision occurs and
                        #it is created while we wait then we ignore
                        raise
        return tempDir
     
    def _tempDirectories(self):
        """
        :rtype : an iterator to the temporary directories containing jobs/stats files
        in the hierarchy of directories in self.tempFilesDir
        """
        def _dirs(path, levels):
            if levels > 0:
                for subPath in os.listdir(path):
                    for i in _dirs(os.path.join(path, subPath), levels-1):
                        yield i
            else:
                yield path
        for tempDir in _dirs(self.tempFilesDir, self.levels):
            yield tempDir
