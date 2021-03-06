from abc import ABCMeta, abstractmethod
from contextlib import contextmanager
import re
import xml.etree.cElementTree as ET

class NoSuchJobException( Exception ):
    def __init__( self, jobStoreID ):
        super( NoSuchJobException, self ).__init__( "The batchjob '%s' does not exist" % jobStoreID )

class ConcurrentFileModificationException( Exception ):
    def __init__( self, jobStoreFileID ):
        super( ConcurrentFileModificationException, self ).__init__(
            'Concurrent update to file %s detected.' % jobStoreFileID )

class NoSuchFileException( Exception ):
    def __init__( self, fileJobStoreID ):
        super( NoSuchFileException, self ).__init__( "The file '%s' does not exist" % fileJobStoreID )

class AbstractJobStore( object ):
    """ 
    Represents the physical storage for the jobs and associated files in a toil.
    """
    __metaclass__ = ABCMeta

    def __init__( self, config=None ):
        """
        FIXME: describe purpose and post-condition

        :param config: If config is not None then the
        given configuration object will be written to the shared file "config.xml" which can
        later be retrieved using the readSharedFileStream. If this file already exists
        it will be overwritten. If config is None, 
        the shared file "config.xml" is assumed to exist and is retrieved. 
        """
        if config is None:
            with self.readSharedFileStream( "config.xml" ) as fileHandle:
                self.__config = ET.parse( fileHandle ).getroot( )
        else:
            with self.writeSharedFileStream( "config.xml" ) as fileHandle:
                ET.ElementTree( config ).write( fileHandle )
            self.__config = config

    @property
    def config( self ):
        return self.__config
    
    @abstractmethod
    def deleteJobStore( self ):
        """
        Removes the jobStore from the disk/store. Careful!
        """
        raise NotImplementedError( )
    
    ##Cleanup functions
    
    def clean(self):
        """
        Function to cleanup the state of a jobStore after a restart.
        Fixes jobs that might have been partially updated.
        """
        #Collate any jobs that were in the process of being created/deleted
        jobsToDelete = set()
        for batchjob in self.jobs():
            for updateID in batchjob.jobsToDelete:
                jobsToDelete.add(updateID)
            
        #Delete the jobs that should be delete
        if len(jobsToDelete) > 0:
            for batchjob in self.jobs():
                if batchjob.updateID in jobsToDelete:
                    self.delete(batchjob.jobStoreID)
        
        #Cleanup the state of each batchjob
        for batchjob in self.jobs():
            changed = False #Flag to indicate if we need to update the batchjob
            #on disk
            
            if len(batchjob.jobsToDelete) != 0:
                batchjob.jobsToDelete = set()
                changed = True
                
            #While jobs at the end of the stack are already deleted remove
            #those jobs from the stack (this cleans up the case that the batchjob
            #had successors to run, but had not been updated to reflect this)
            while len(batchjob.stack) > 0:
                jobs = [ command[0] for command in batchjob.stack[-1] if self.exists(command[0]) ]
                if len(jobs) < len(batchjob.stack[-1]):
                    changed = True
                    if len(jobs) > 0:
                        batchjob.stack[-1] = jobs
                        break
                    else:
                        batchjob.stack.pop()
                else:
                    break
                          
            #This cleans the old log file which may 
            #have been left if the batchjob is being retried after a batchjob failure.
            if batchjob.logJobStoreFileID != None:
                batchjob.clearLogFile(self)
                changed = True
            
            if changed: #Update, but only if a change has occurred
                self.update(batchjob)
        
        #Remove any crufty stats/logging files from the previous run
        self.readStatsAndLogging(lambda x : None)
    
    ##########################################
    #The following methods deal with creating/loading/updating/writing/checking for the
    #existence of jobs
    ##########################################  

    @abstractmethod
    def create( self, command, memory, cpu, disk, updateID=None,
                predecessorNumber=0 ):
        """
        Creates a batchjob, adding it to the store.
        
        Command, memory, cpu, updateID, predecessorNumber 
        are all arguments to the batchjob's constructor.

        :rtype : batchjob.Batchjob
        """
        raise NotImplementedError( )

    @abstractmethod
    def exists( self, jobStoreID ):
        """
        Returns true if the batchjob is in the store, else false.

        :rtype : bool
        """
        raise NotImplementedError( )

    @abstractmethod
    def getPublicUrl( self,  FileName):
        """
        Returns a publicly accessible URL to the given file in the batchjob store.
        The returned URL starts with 'http:',  'https:' or 'file:'.
        The returned URL may expire as early as 1h after its been returned.
        Throw an exception if the file does not exist.
        :param jobStoreFileID:
        :return:
        """
        raise NotImplementedError()

    @abstractmethod
    def getSharedPublicUrl( self,  jobStoreFileID):
        """
        Returns a publicly accessible URL to the given file in the batchjob store.
        The returned URL starts with 'http:',  'https:' or 'file:'.
        The returned URL may expire as early as 1h after its been returned.
        Throw an exception if the file does not exist.
        :param jobStoreFileID:
        :return:
        """
        raise NotImplementedError()

    @abstractmethod
    def load( self, jobStoreID ):
        """
        Loads a batchjob for the given jobStoreID and returns it.

        :rtype : batchjob.Batchjob

        :raises: NoSuchJobException if there is no batchjob with the given jobStoreID
        """
        raise NotImplementedError( )

    @abstractmethod
    def update( self, batchjob ):
        """
        Persists the batchjob in this store atomically.
        """
        raise NotImplementedError( )

    @abstractmethod
    def delete( self, jobStoreID ):
        """
        Removes from store atomically, can not then subsequently call load(), write(), update(),
        etc. with the batchjob.

        This operation is idempotent, i.e. deleting a batchjob twice or deleting a non-existent batchjob
        will succeed silently.
        """
        raise NotImplementedError( )
    
    def jobs(self):
        """
        Returns iterator on the jobs in the store.
        
        :rtype : iterator
        """
        raise NotImplementedError( )

    ##########################################
    #The following provide an way of creating/reading/writing/updating files 
    #associated with a given batchjob.
    ##########################################  

    @abstractmethod
    def writeFile( self, jobStoreID, localFilePath ):
        """
        Takes a file (as a path) and places it in this batchjob store. Returns an ID that can be used
        to retrieve the file at a later time. jobStoreID is the id of the batchjob from which the file
        is being created. When delete(batchjob) is called all files written with the given
        batchjob.jobStoreID will be removed from the jobStore.
        """
        raise NotImplementedError( )

    @abstractmethod
    def updateFile( self, jobStoreFileID, localFilePath ):
        """
        Replaces the existing version of a file in the jobStore. Throws an exception if the file
        does not exist.

        :raises ConcurrentFileModificationException: if the file was modified concurrently during
        an invocation of this method
        """
        raise NotImplementedError( )

    @abstractmethod
    def readFile( self, jobStoreFileID, localFilePath ):
        """
        Copies the file referenced by jobStoreFileID to the given local file path. The version
        will be consistent with the last copy of the file written/updated.
        """
        raise NotImplementedError( )

    @abstractmethod
    def deleteFile( self, jobStoreFileID ):
        """
        Deletes the file with the given ID from this batchjob store.
        This operation is idempotent, i.e. deleting a file twice or deleting a non-existent file
        will succeed silently.
        """
        raise NotImplementedError( )
    
    @abstractmethod
    def fileExists(self, jobStoreFileID ):
        """
        :rtype : True if the jobStoreFileID exists in the jobStore, else False
        """
        raise NotImplementedError()

    @abstractmethod
    @contextmanager
    def writeFileStream( self, jobStoreID ):
        """
        Similar to writeFile, but returns a context manager yielding a tuple of 1) a file handle
        which can be written to and 2) the ID of the resulting file in the batchjob store. The yielded
        file handle does not need to and should not be closed explicitly.
        """
        raise NotImplementedError( )

    @abstractmethod
    @contextmanager
    def updateFileStream( self, jobStoreFileID ):
        """
        Similar to updateFile, but returns a context manager yielding a file handle which can be
        written to. The yielded file handle does not need to and should not be closed explicitly.

        :raises ConcurrentFileModificationException: if the file was modified concurrently during
        an invocation of this method
        """
        raise NotImplementedError( )

    @abstractmethod
    def getEmptyFileStoreID( self, jobStoreID ):
        """
        :rtype : string, the ID of a new, empty file. 
        
        Call to fileExists(getEmptyFileStoreID(jobStoreID)) will return True.
        """
        raise NotImplementedError( )

    @abstractmethod
    @contextmanager
    def readFileStream( self, jobStoreFileID ):
        """
        Similar to readFile, but returns a context manager yielding a file handle which can be
        read from. The yielded file handle does not need to and should not be closed explicitly.
        """
        raise NotImplementedError( )
    
    ##########################################
    #The following methods deal with shared files, i.e. files not associated 
    #with specific jobs.
    ##########################################  

    sharedFileNameRegex = re.compile( r'^[a-zA-Z0-9._-]+$' )

    # FIXME: Rename to updateSharedFileStream

    @abstractmethod
    @contextmanager
    def writeSharedFileStream( self, sharedFileName ):
        """
        Returns a context manager yielding a writable file handle to the global file referenced
        by the given name.

        :param sharedFileName: A file name matching AbstractJobStore.fileNameRegex, unique within
        the physical storage represented by this batchjob store

        :raises ConcurrentFileModificationException: if the file was modified concurrently during
        an invocation of this method
        """
        raise NotImplementedError( )

    @abstractmethod
    @contextmanager
    def readSharedFileStream( self, sharedFileName ):
        """
        Returns a context manager yielding a readable file handle to the global file referenced
        by the given name.
        """
        raise NotImplementedError( )

    @abstractmethod
    def writeStatsAndLogging( self, statsAndLoggingString ):
        """
        Adds the given statistics/logging string to the store of statistics info.
        """
        raise NotImplementedError( )

    @abstractmethod
    def readStatsAndLogging( self, statsAndLoggingCallBackFn):
        """
        Reads stats/logging strings accumulated by "writeStatsAndLogging" function. 
        For each stats/logging file calls the statsAndLoggingCallBackFn with 
        an open, readable file-handle that can be used to parse the stats.
        Returns the number of stat/logging strings processed. 
        Stats/logging files are only read once and are removed from the 
        file store after being written to the given file handle.
        """
        raise NotImplementedError( )

    ## Helper methods for subclasses

    def _defaultTryCount( self ):
        return int( self.config.attrib[ "try_count" ] )

    @classmethod
    def _validateSharedFileName( cls, sharedFileName ):
        return bool( cls.sharedFileNameRegex.match( sharedFileName ) )
