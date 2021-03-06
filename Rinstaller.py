import os
import sys
import time
import logging
import json
import hashlib
import tempfile
import shutil
import stat
import math
import importlib
import threading
import optparse
import io
from typing import Optional, Dict, Any, Union, List

loglevel = logging.INFO

class _FileUpdateType:
    NEW = 1
    DELETE = 2
    MODIFY = 3

class Updater:

    def __init__(self,
                 url: str,
                 instance: str,
                 targetLocation: str,
                 localInstanceInfo: Optional[Dict[str, Any]] = None,
                 retrieveInfo: bool = True,
                 timeout: float = 20.0):

        self.fileName = os.path.basename(__file__)

        self.updaterLock = threading.Lock()
        
        self.version = 0
        self.rev = 0
        self.instance = instance
        self.instanceLocation = targetLocation

        # set update server configuration
        if not url.lower().startswith("https"):
            raise ValueError("Only 'https' is allowed.")
        self.url = url
        self.timeout = timeout

        # needed to keep track of the newest version
        self.newestVersion = self.version
        self.newestRev = self.rev
        self.newestFiles = None  # type: Optional[Dict[str, str]]
        self.newestSymlinks = None  # type: Optional[List[str]]
        self.lastChecked = 0
        self.repoInfo = None  # type: Dict[str, Any]
        self.repoInstanceLocation = None  # type: Optional[str]
        self.instanceInfo = None  # type: Dict[str, Any]
        self.repo_version = 1
        self.max_redirections = 10

        if localInstanceInfo is None:
            self.localInstanceInfo = {"files": {}}
        else:
            self.localInstanceInfo = localInstanceInfo

        
        self.chunkSize = 4096

        if retrieveInfo:
            if not self._getNewestVersionInformation():
                raise ValueError("Not able to get newest repository information.")

    def _acquireLock(self):
        """
        Internal function that acquires the lock.
        """
        logging.debug("[%s]: Acquire lock." % self.fileName)
        self.updaterLock.acquire()

    def _releaseLock(self):
        """
        # Internal function that releases the lock.
        """
        logging.debug("[%s]: Release lock." % self.fileName)
        self.updaterLock.release()

    def _checkFilesToUpdate(self) -> Optional[Dict[str, int]]:
        """
        Internal function that checks which files are new and which files have to be updated.

        :return: a dict of files that are affected by this update (and how) or None
        """
        
        utcTimestamp = int(time.time())
        if (utcTimestamp - self.lastChecked) > 60 or self.newestFiles is None or self.newestSymlinks is None:
            if self._getNewestVersionInformation() is False:
                logging.error("[%s]: Not able to get version information for checking files." % self.fileName)
                return None

        counterUpdate = 0
        counterNew = 0
        counterDelete = 0
        fileList = self.newestFiles.keys()

        filesToUpdate = dict()
        for clientFile in fileList:

            if os.path.exists(os.path.join(self.instanceLocation, clientFile)):

                f = open(os.path.join(self.instanceLocation, clientFile), 'rb')
                sha256Hash = self._sha256File(f)
                f.close()

                
                if sha256Hash == self.newestFiles[clientFile]:
                    logging.debug("[%s]: Not changed: '%s'" % (self.fileName, clientFile))
                    continue
                else:
                    logging.debug("[%s]: New version: '%s'" % (self.fileName, clientFile))
                    filesToUpdate[clientFile] = _FileUpdateType.MODIFY
                    counterUpdate += 1

            
            else:
                logging.debug("[%s]: New file: '%s'" % (self.fileName, clientFile))
                filesToUpdate[clientFile] = _FileUpdateType.NEW
                counterNew += 1

        
        for clientFile in self.localInstanceInfo["files"].keys():

            if clientFile not in fileList:
                logging.debug("[%s]: Delete file: '%s'" % (self.fileName, clientFile))
                filesToUpdate[clientFile] = _FileUpdateType.DELETE
                counterDelete += 1

        logging.info("[%s]: Files to modify: %d; New files: %d; Files to delete: %d"
                     % (self.fileName, counterUpdate, counterNew, counterDelete))

        return filesToUpdate

    def _checkFilePermissions(self, filesToUpdate: Dict[str, int]) -> bool:
        for clientFile in filesToUpdate.keys():

            
            if filesToUpdate[clientFile] == _FileUpdateType.MODIFY:

                if not os.access(os.path.join(self.instanceLocation, clientFile), os.W_OK):
                    logging.error("[%s]: File '%s' is not writable." % (self.fileName, clientFile))
                    return False

                logging.debug("[%s]: File '%s' is writable." % (self.fileName, clientFile))

            elif filesToUpdate[clientFile] == _FileUpdateType.NEW:
                logging.debug("[%s]: Checking write permissions for new file: '%s'"
                              % (self.fileName, clientFile))

                folderStructure = clientFile.split("/")

                if len(folderStructure) == 1:
                    if not os.access(self.instanceLocation, os.W_OK):
                        logging.error("[%s]: Folder './' is not writable." % self.fileName)
                        return False

                    logging.debug("[%s]: Folder './' is writable." % self.fileName)

                else:
                    tempPart = ""
                    for filePart in folderStructure:

                        if os.path.exists(os.path.join(self.instanceLocation, tempPart, filePart)):

                            if not os.access(os.path.join(self.instanceLocation, tempPart, filePart), os.W_OK):
                                logging.error("[%s]: Folder '%s' is not writable."
                                              % (self.fileName, os.path.join(tempPart, filePart)))
                                return False

                            logging.debug("[%s]: Folder '%s' is writable."
                                          % (self.fileName, os.path.join(tempPart, filePart)))

                            tempPart = os.path.join(tempPart, filePart)

            elif filesToUpdate[clientFile] == _FileUpdateType.DELETE:
                if not os.access(os.path.join(self.instanceLocation, clientFile), os.W_OK):
                    logging.error("[%s]: File '%s' is not writable (deletable)."
                                  % (self.fileName, clientFile))
                    return False

                logging.debug("[%s]: File '%s' is writable (deletable)."
                              % (self.fileName, clientFile))

            else:
                raise ValueError("Unknown file update type.")

        return True

    def _createSubDirectories(self, fileLocation: str, targetDirectory: str) -> bool:
       
        folderStructure = fileLocation.split("/")
        if len(folderStructure) != 1:

            try:
                i = 0
                tempPart = ""
                while i < (len(folderStructure) - 1):

                    if not os.path.exists(os.path.join(targetDirectory, tempPart, folderStructure[i])):
                        logging.debug("[%s]: Creating directory '%s'."
                                      % (self.fileName, os.path.join(targetDirectory, tempPart, folderStructure[i])))

                        os.mkdir(os.path.join(targetDirectory, tempPart, folderStructure[i]))

                    
                    elif not os.path.isdir(os.path.join(targetDirectory, tempPart, folderStructure[i])):
                        raise ValueError("Location '%s' already exists and is not a directory."
                                         % (os.path.join(tempPart, folderStructure[i])))

                    else:
                        logging.debug("[%s]: Directory '%s' already exists."
                                      % (self.fileName, os.path.join(targetDirectory, tempPart, folderStructure[i])))

                    tempPart = os.path.join(tempPart, folderStructure[i])
                    i += 1

            except Exception as e:
                logging.exception("[%s]: Creating directory structure for '%s' failed."
                                  % (self.fileName, fileLocation))
                return False

        return True

    def _deleteSubDirectories(self, fileLocation: str, targetDirectory: str) -> bool:
        folderStructure = fileLocation.split("/")
        del folderStructure[-1]

        try:
            i = len(folderStructure) - 1
            while 0 <= i:

                tempDir = ""
                for j in range(i + 1):
                    tempDir = os.path.join(tempDir, folderStructure[j])

                if os.listdir(os.path.join(targetDirectory, tempDir)):
                    break

                logging.debug("[%s]: Deleting directory '%s'."
                              % (self.fileName, os.path.join(targetDirectory, tempDir)))

                os.rmdir(os.path.join(targetDirectory, tempDir))
                i -= 1

        except Exception as e:
            logging.exception("[%s]: Deleting directory structure for '%s' failed."
                              % (self.fileName, fileLocation))
            return False

        return True

    def _downloadFile(self, file_location: str, file_hash: str) -> Optional[io.BufferedRandom]:
        logging.info("[%s]: Downloading file: '%s'" % (self.fileName, file_location))

        # create temporary file
        try:
            fileHandle = tempfile.TemporaryFile(mode='w+b')

        except Exception as e:
            logging.exception("[%s]: Creating temporary file failed." % self.fileName)
            return None

        # Download file from server.
        redirect_ctr = 0
        while True:

            if redirect_ctr > self.max_redirections:
                logging.error("[%s]: Too many redirections during download. Something is wrong with the repository."
                              % self.fileName)
                return None

            try:
                url = os.path.join(self.url, self.repoInstanceLocation, file_location)
                with requests.get(url,
                                  verify=True,
                                  stream=True,
                                  timeout=self.timeout) as r:

                
                    r.raise_for_status()

                    fileSize = -1
                    maxChunks = 0
                    try:
                        fileSize = int(r.headers.get('content-type'))

                    except Exception as e:
                        fileSize = -1

                    
                    showStatus = False
                    if fileSize > 0:
                        showStatus = True
                        maxChunks = int(math.ceil(float(fileSize) / float(self.chunkSize)))

                    chunkCount = 0
                    printedPercentage = 0
                    for chunk in r.iter_content(chunk_size=self.chunkSize):
                        if not chunk:
                            continue
                        fileHandle.write(chunk)

                        chunkCount += 1
                        if showStatus:
                            if chunkCount > maxChunks:
                                showStatus = False
                                logging.warning("[%s]: Content information of received header flawed. Stopping "
                                                % self.fileName
                                                + "to show download status.")
                                continue

                            else:
                                percentage = int((float(chunkCount) / float(maxChunks)) * 100)
                                if (percentage / 10) > printedPercentage:
                                    printedPercentage = percentage / 10

                                    logging.info("[%s]: Download: %d%%" % (self.fileName, printedPercentage * 10))

            except Exception as e:
                logging.exception("[%s]: Downloading file '%s' from the server failed."
                                  % (self.fileName, file_location))
                return None
            if file_location not in self.newestSymlinks:
                break

            logging.info("[%s]: File '%s' is symlink." % (self.fileName, file_location))

            
            fileHandle.seek(0)
            
            base_path = os.path.dirname(file_location)
            file_location = os.path.join(base_path, fileHandle.readline().decode("ascii").strip())
            fileHandle.seek(0)

            logging.info("[%s]: Downloading new location: %s" % (self.fileName, file_location))
            redirect_ctr += 1

        
        fileHandle.seek(0)
        sha256Hash = self._sha256File(fileHandle)
        fileHandle.seek(0)

        if sha256Hash != file_hash:
            logging.error("[%s]: Temporary file does not have the correct hash." % self.fileName)
            logging.debug("[%s]: Temporary file: %s" % (self.fileName, sha256Hash))
            logging.debug("[%s]: Repository: %s" % (self.fileName, file_hash))
            return None

        logging.info("[%s]: Successfully downloaded file: '%s'" % (self.fileName, file_location))
        return fileHandle

    def _sha256File(self, fileHandle: Union[io.TextIOBase, io.BufferedIOBase]) -> str:
        
        fileHandle.seek(0)
        sha256 = hashlib.sha256()
        while True:
            data = fileHandle.read(128)
            if not data:
                break
            sha256.update(data)
        return sha256.hexdigest()

    def _getInstanceInformation(self) -> bool:
        
        if self.repoInfo is None or self.repoInstanceLocation is None:
            try:
                if self._getRepositoryInformation() is False:
                    raise ValueError("Not able to get newest repository information.")

            except Exception as e:
                logging.exception("[%s]: Retrieving newest repository information failed." % self.fileName)
                return False

        logging.debug("[%s]: Downloading instance information." % self.fileName)

        instanceInfoString = ""
        try:
            url = os.path.join(self.url, self.repoInstanceLocation, "instanceInfo.json")
            with requests.get(url,
                              verify=True,
                              timeout=self.timeout) as r:
                r.raise_for_status()
                instanceInfoString = r.text

        except Exception as e:
            logging.exception("[%s]: Getting instance information failed." % self.fileName)
            return False

        try:
            self.instanceInfo = json.loads(instanceInfoString)

            if not isinstance(self.instanceInfo["version"], float):
                raise ValueError("Key 'version' is not of type float.")

            if not isinstance(self.instanceInfo["rev"], int):
                raise ValueError("Key 'rev' is not of type int.")

            if not isinstance(self.instanceInfo["dependencies"], dict):
                raise ValueError("Key 'dependencies' is not of type dict.")
                if not isinstance(self.instanceInfo["symlinks"], list):
                    raise ValueError("Key 'symlinks' is not of type list.")

        except Exception as e:
            logging.exception("[%s]: Parsing instance information failed." % self.fileName)
            return False

        return True

    def _getRepositoryInformation(self) -> bool:
        
        logging.debug("[%s]: Downloading repository information." % self.fileName)

        repoInfoString = ""
        try:
            url = os.path.join(self.url, "repoInfo.json")
            with requests.get(url,
                              verify=True,
                              timeout=self.timeout) as r:
                r.raise_for_status()
                repoInfoString = r.text

        except Exception as e:
            logging.exception("[%s]: Getting repository information failed." % self.fileName)
            return False

        try:
            self.repoInfo = json.loads(repoInfoString)

            if not isinstance(self.repoInfo, dict):
                raise ValueError("Received repository information is not of type dict.")

            if "instances" not in self.repoInfo.keys():
                raise ValueError("Received repository information has no information about the instances.")

            if self.instance not in self.repoInfo["instances"].keys():
                raise ValueError("Instance '%s' is not managed by used repository." % self.instance)

            if "version" in self.repoInfo.keys():
                self.repo_version = self.repoInfo["version"]

            logging.debug("[%s]: Repository version: %d" % (self.fileName, self.repo_version))

        except Exception as e:
            logging.exception("[%s]: Parsing repository information failed." % self.fileName)
            return False

        self.repoInstanceLocation = str(self.repoInfo["instances"][self.instance]["location"])

        return True

    def _getNewestVersionInformation(self) -> bool:
        """
        Internal function that gets the newest version information from the online repository.

        :return: True or False
        """
        try:
            if self._getInstanceInformation() is False:
                raise ValueError("Not able to get newest instance information.")

        except Exception as e:
            logging.exception("[%s]: Retrieving newest instance information failed." % self.fileName)
            return False

        # Parse version information.
        try:
            version = float(self.instanceInfo["version"])
            rev = int(self.instanceInfo["rev"])
            newestFiles = self.instanceInfo["files"]
            if "symlinks" in self.instanceInfo.keys():
                newestSymlinks = self.instanceInfo["symlinks"]
            else:
                newestSymlinks = []

            if not isinstance(newestFiles, dict):
                raise ValueError("Key 'files' is not of type dict.")

            if not isinstance(newestSymlinks, list):
                raise ValueError("Key 'symlinks' is not of type list.")

        except Exception as e:
            logging.exception("[%s]: Parsing version information failed." % self.fileName)
            return False

        logging.debug("[%s]: Newest version information: %.3f-%d." % (self.fileName, version, rev))
        if (version > self.newestVersion
           or (rev > self.newestRev and version == self.newestVersion)
           or self.newestFiles is None
           or self.newestSymlinks is None):

            # update newest known version information
            self.newestVersion = version
            self.newestRev = rev
            self.newestFiles = newestFiles
            self.newestSymlinks = newestSymlinks

        self.lastChecked = int(time.time())
        return True

    def getInstanceInformation(self) -> Dict[str, Any]:
        self._acquireLock()
        utcTimestamp = int(time.time())
        if (utcTimestamp - self.lastChecked) > 60 or self.instanceInfo is None:

            if not self._getInstanceInformation():
                self._releaseLock()
                raise ValueError("Not able to get newest instance information.")

        self._releaseLock()
        return self.instanceInfo

    def getRepositoryInformation(self) -> Dict[str, Any]:
        self._acquireLock()
        utcTimestamp = int(time.time())
        if (utcTimestamp - self.lastChecked) > 60 or self.repoInfo is None:

            if not self._getRepositoryInformation():
                self._releaseLock()
                raise ValueError("Not able to get newest repository information.")

        self._releaseLock()
        return self.repoInfo

    def updateInstance(self) -> bool:
        self._acquireLock()

        # check all files that have to be updated
        filesToUpdate = self._checkFilesToUpdate()

        if filesToUpdate is None:
            logging.error("[%s] Checking files for update failed." % self.fileName)
            self._releaseLock()
            return False

        if len(filesToUpdate) == 0:
            logging.info("[%s] No files have to be updated." % self.fileName)
            self._releaseLock()
            return True

        # check file permissions of the files that have to be updated
        if self._checkFilePermissions(filesToUpdate) is False:
            logging.info("[%s] Checking file permissions failed." % self.fileName)
            self._releaseLock()
            return False

        # download all files that have to be updated
        downloadedFileHandles = dict()
        for fileToUpdate in filesToUpdate.keys():

            # only download file if it is new or has to be modified
            if (filesToUpdate[fileToUpdate] == _FileUpdateType.NEW
               or filesToUpdate[fileToUpdate] == _FileUpdateType.MODIFY):
                downloadedFileHandle = self._downloadFile(fileToUpdate, self.newestFiles[fileToUpdate])

                if downloadedFileHandle is None:
                    logging.error("[%s]: Downloading files from the repository failed. Aborting update process."
                                  % self.fileName)

                    for fileHandle in downloadedFileHandles.keys():
                        downloadedFileHandles[fileHandle].close()

                    self._releaseLock()
                    return False

                else:
                    downloadedFileHandles[fileToUpdate] = downloadedFileHandle

       
        for fileToUpdate in filesToUpdate.keys():

            if filesToUpdate[fileToUpdate] == _FileUpdateType.DELETE:

                # remove old file.
                try:
                    logging.debug("[%s]: Deleting file '%s'." % (self.fileName, fileToUpdate))
                    os.remove(os.path.join(self.instanceLocation, fileToUpdate))

                except Exception as e:
                    logging.exception("[%s]: Deleting file '%s' failed." % (self.fileName, fileToUpdate))
                    self._releaseLock()
                    return False

                self._deleteSubDirectories(fileToUpdate, self.instanceLocation)
                continue

            elif filesToUpdate[fileToUpdate] == _FileUpdateType.NEW:
                self._createSubDirectories(fileToUpdate, self.instanceLocation)

            try:
                logging.debug("[%s]: Copying file '%s' to AlertR instance directory." % (self.fileName, fileToUpdate))
                dest = open(os.path.join(self.instanceLocation, fileToUpdate), 'wb')
                shutil.copyfileobj(downloadedFileHandles[fileToUpdate], dest)
                dest.close()

            except Exception as e:
                logging.exception("[%s]: Copying file '%s' failed." % (self.fileName, fileToUpdate))
                self._releaseLock()
                return False

            f = open(os.path.join(self.instanceLocation, fileToUpdate), 'rb')
            sha256Hash = self._sha256File(f)
            f.close()
            if sha256Hash != self.newestFiles[fileToUpdate]:
                logging.error("[%s]: Hash of file '%s' is not correct after copying." % (self.fileName, fileToUpdate))
                self._releaseLock()
                return False

            if fileToUpdate in ["alertRclient.py",
                                "alertRserver.py",
                                "alertRupdate.py",
                                "graphExport.py",
                                "manageUsers.py"]:

                logging.debug("[%s]: Changing permissions of '%s'." % (self.fileName, fileToUpdate))

                try:
                    os.chmod(os.path.join(self.instanceLocation, fileToUpdate),
                             stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

                except Exception as e:
                    logging.exception("[%s]: Changing permissions of '%s' failed." % (self.fileName, fileToUpdate))
                    self._releaseLock()
                    return False
            elif fileToUpdate in ["config/config.xml.template"]:

                logging.debug("[%s]: Changing permissions of '%s'." % (self.fileName, fileToUpdate))

                try:
                    os.chmod(os.path.join(self.instanceLocation, fileToUpdate),
                             stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)

                except Exception as e:
                    logging.exception("[%s]: Changing permissions of '%s' failed." % (self.fileName, fileToUpdate))
                    self._releaseLock()
                    return False

        for fileHandle in downloadedFileHandles.keys():
            downloadedFileHandles[fileHandle].close()

        self._releaseLock()
        return True

    def setInstance(self, instance: str, retrieveInfo: bool = True):
        self.instance = instance
        self.instanceInfo = None
        self.lastChecked = 0
        self.repoInfo = None
        self.repoInstanceLocation = None

        if retrieveInfo:
            if not self._getNewestVersionInformation():
                raise ValueError("Not able to get newest repository information.")


def check_dependencies(dependencies: Dict[str, Any]) -> bool:

    fileName = os.path.basename(__file__)

    if "pip" in dependencies.keys():

        for pip in dependencies["pip"]:

            importName = pip["import"]
            packet = pip["packet"]

            # only get version if it exists
            version = None
            if "version" in pip.keys():
                version = pip["version"]

            # try to import needed module
            temp = None
            try:
                logging.info("[%s]: Checking module '%s'." % (fileName, importName))
                temp = importlib.import_module(importName)

            except Exception as e:
                logging.error("[%s]: Module '%s' not installed." % (fileName, importName))
                print("")
                print("The needed module '%s' is not installed. " % importName, end="")
                print("You can install the module by executing ", end="")
                print("'pip3 install %s' " % packet, end="")
                print("(if you do not have installed pip, you can install it ", end="")
                print("on Debian like systems by executing ", end="")
                print("'apt-get install python3-pip').")
                return False

            if version is not None:

                versionCorrect = True
                versionCheckFailed = False
                installedVersion = "Information Not Available"

                # Try to extract version from package.
                try:
                    installedVersion = temp.__version__.split(".")
                    neededVersion = version.split(".")

                    maxLength = 0
                    if len(installedVersion) > len(neededVersion):
                        maxLength = len(installedVersion)

                    else:
                        maxLength = len(neededVersion)

            
                    for i in range(maxLength):
                        if int(installedVersion[i]) > int(neededVersion[i]):
                            break

                        elif int(installedVersion[i]) < int(neededVersion[i]):
                            versionCorrect = False
                            break

                except Exception as e:
                    logging.error("[%s]: Could not verify installed version of module '%s'." % (fileName, importName))
                    versionCheckFailed = True

                
                if versionCheckFailed is True:
                    print("")
                    print("Could not automatically verify the installed ", end="")
                    print("version of the module '%s'. " % importName, end="")
                    print("You have to verify the version yourself.")
                    print("")
                    print("Installed version: %s" % installedVersion)
                    print("Needed version: %s" % version)
                    print("")
                    print("Do you have a version installed that satisfies ", end="")
                    print("the needed version?")

                    if not user_confirmation():
                        versionCorrect = False

                    else:
                        versionCorrect = True

            
                if versionCorrect is False:
                    print("")
                    print("The needed version '%s' " % version, end="")
                    print("of module '%s' is not satisfied " % importName, end="")
                    print("(you have version '%s' " % installedVersion, end="")
                    print("installed).")
                    print("Please update your installed version of the pip ", end="")
                    print("packet '%s'." % packet)
                    return False

    if "other" in dependencies.keys():

        for other in dependencies["other"]:

            importName = other["import"]

            version = None
            if "version" in other.keys():
                version = other["version"]

            temp = None
            try:
                logging.info("[%s]: Checking module '%s'." % (fileName, importName))
                temp = importlib.import_module(importName)

            except Exception as e:
                logging.error("[%s]: Module '%s' not installed." % (fileName, importName))
                print("")
                print("The needed module '%s' is not " % importName, end="")
                print("installed. You need to install it before ", end="")
                print("you can use this AlertR instance.")
                return False

            if version is not None:

                versionCorrect = True
                versionCheckFailed = False
                installedVersion = "Information Not Available"

                try:
                    installedVersion = temp.__version__.split(".")
                    neededVersion = version.split(".")

                    maxLength = 0
                    if len(installedVersion) > len(neededVersion):
                        maxLength = len(installedVersion)

                    else:
                        maxLength = len(neededVersion)
                    for i in range(maxLength):
                        if int(installedVersion[i]) > int(neededVersion[i]):
                            break

                        elif int(installedVersion[i]) < int(neededVersion[i]):
                            versionCorrect = False
                            break

                except Exception as e:
                    logging.error("[%s]: Could not verify installed version of module '%s'." % (fileName, importName))
                    versionCheckFailed = True

                if versionCheckFailed is True:
                    print("")
                    print("Could not automatically verify the installed ", end="")
                    print("version of the module '%s'. " % importName, end="")
                    print("You have to verify the version yourself.")
                    print("")
                    print("Installed version: %s" % installedVersion)
                    print("Needed version: %s" % version)
                    print("")
                    print("Do you have a version installed that satisfies ", end="")
                    print("the needed version?")

                    if not user_confirmation():
                        versionCorrect = False

                    else:
                        versionCorrect = True

                if versionCorrect is False:
                    print("")
                    print("The needed version '%s' " % version, end="")
                    print("of module '%s' is not satisfied " % importName, end="")
                    print("(you have version '%s' " % installedVersion, end="")
                    print("installed).")
                    print("Please update your installed version.")
                    return False

    return True


def check_requests_available() -> bool:
    """
    Checks if the module "requests" is available in the correct version.

    :return: True if available
    """

    import_name = "requests"
    version = requests_min_version

    # try to import needed module
    temp = None
    try:
        logging.info("[%s]: Checking module '%s' installed." % (fileName, import_name))
        temp = importlib.import_module(import_name)

    except Exception as e:
        logging.error("[%s]: Module '%s' not installed." % (fileName, import_name))
        return False

    version_correct = False
    version_check_failed = False
    installed_version = "Information Not Available"


    try:
        installed_version = temp.__version__.split(".")
        needed_version = version.split(".")

        max_length = 0
        if len(installed_version) > len(needed_version):
            max_length = len(installed_version)

        else:
            max_length = len(needed_version)

        for i in range(max_length):
            if int(installed_version[i]) > int(needed_version[i]):
                version_correct = True
                break

            elif int(installed_version[i]) < int(needed_version[i]):
                version_correct = False
                break

    except Exception as e:
        logging.error("[%s]: Could not verify installed version of module '%s'." % (fileName, import_name))
        version_check_failed = True

    if version_check_failed:
        print("")
        print("Could not automatically verify the installed version of the module '%s'." % import_name)
        print("You have to verify the version yourself.")
        print("")
        print("Installed version: %s" % installed_version)
        print("Needed version: %s" % version)
        print("")
        print("Do you have a version installed that satisfies the needed version?")

        if not user_confirmation():
            version_correct = False

        else:
            version_correct = True

    return version_correct


def list_all_instances(url: str) -> bool:

    updater_obj = Updater(url, "server", "", retrieveInfo=False)
    try:
        repo_info = updater_obj.getRepositoryInformation()
    except Exception as e:
        print(e)
        repo_info = None
    if repo_info is None:
        return False

    temp = list(repo_info["instances"].keys())
    temp.sort()

    print("")

    for instance in temp:

        
        updater_obj.setInstance(instance, retrieveInfo=False)
        instance_info = updater_obj.getInstanceInformation()

        print(repo_info["instances"][instance]["name"])
        print("-"*len(repo_info["instances"][instance]["name"]))
        print("Instance:")
        print(instance)
        print("")
        print("Type:")
        print(repo_info["instances"][instance]["type"])
        print("")
        print("Version:")
        print("%.3f-%d" % (instance_info["version"], instance_info["rev"]))
        print("")
        print("Dependencies:")

        # print instance dependencies
        idx = 1
        if "pip" in instance_info["dependencies"].keys():
            for pip in instance_info["dependencies"]["pip"]:
                import_name = pip["import"]
                packet = pip["packet"]
                print("%d: %s (pip packet: %s)" % (idx, import_name, packet), end="")
                if "version" in pip.keys():
                    print(" (lowest version: %s)" % pip["version"])
                else:
                    print("")
                idx += 1

        if "other" in instance_info["dependencies"].keys():
            for other in instance_info["dependencies"]["other"]:
                import_name = other["import"]
                print("%d: %s" % (idx, import_name), end="")
                if "version" in other.keys():
                    print("(lowest version: %s)" % other["version"])
                else:
                    print("")
                idx += 1

        if idx == 1:
            print("None")

        print("")
        print("Description:")
        print(repo_info["instances"][instance]["desc"])
        print("")
        print("")


def output_failure_and_exit():
    print("")
    print("INSTALLATION FAILED!")
    print("To see the reason take a look at the installation process output.", end="")
    print("You can change the log level in the file to 'DEBUG'", end="")
    print("and repeat the installation process to get more detailed information.")
    sys.exit(1)

def user_confirmation() -> bool:

    while True:
        try:
            localInput = input("(y/n): ")
        except KeyboardInterrupt:
            print("Bye.")
            sys.exit(0)
        except Exception:
            continue

        if localInput.strip().upper() == "Y":
            return True
        elif localInput.strip().upper() == "N":
            return False
        else:
            print("Invalid input.")


if __name__ == '__main__':

    # initialize logging
    logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=loglevel)

    fileName = os.path.basename(__file__)

    # parsing command line options
    parser = optparse.OptionParser()

    parser.formatter = optparse.TitledHelpFormatter()

    installationGroup.add_option("-t",
                                 "--target",
                                 dest="targetDirectory",
                                 action="store",
                                 help="The target directory into which the chosen AlertR instance"
                                      + " should be installed. (Required)",
                                 default=None)
    installationGroup.add_option("-f",
                                 "--force",
                                 dest="force",
                                 action="store_true",
                                 help="Do not check the dependencies. Just install it. (Optional)",
                                 default=False)

    listGroup = optparse.OptionGroup(parser,
                                     "Show information about the online repository")
    listGroup.add_option("-l",
                         "--list",
                         dest="list",
                         action="store_true",
                         help="List all available AlertR instances in the repository.",
                         default=False)

    parser.add_option_group(installationGroup)
    parser.add_option_group(listGroup)

    (options, args) = parser.parse_args()

    
    if check_requests_available():
        import requests

    else:
        print("")
        print("The installation process needs the module 'requests' at least in version '%s' installed."
              % requests_min_version)
        print("You can install the module by executing 'pip3 install requests'.")
        print("If you do not have installed pip, you can install it on Debian like systems by executing ", end="")
        print("'apt-get install python3-pip'.")
        sys.exit(1)

    
    if options.list:
        if list_all_instances(url) is False:
            print("")
            print("Could not list repository information.")
            sys.exit(1)
        sys.exit(0)

    
    elif options.instance is not None and options.targetDirectory is not None:

        instance = options.instance
        targetLocation = options.targetDirectory

        
        if targetLocation[:1] != "/":
            targetLocation = os.path.join(os.path.dirname(os.path.abspath(__file__)), targetLocation)

        
        if os.path.exists(targetLocation) is False or os.path.isdir(targetLocation) is False:
            print("")
            print("Chosen target location does not exist.")
            sys.exit(1)

        
        updater_obj = Updater(url, "server", targetLocation, retrieveInfo=False)
        try:
            repo_info = updater_obj.getRepositoryInformation()
        except Exception as e:
            print(e)
            repo_info = None
        if repo_info is None:
            print("")
            print("Could not download repository information from repository.")
            sys.exit(1)

        # get the correct case of the instance to install
        found = False
        for repo_key in repo_info["instances"].keys():
            if repo_key.upper() == instance.upper():
                instance = repo_key
                found = True
                break
        
        if not found:
            print("")
            print("Chosen AlertR instance '%s' does not exist in repository." % instance)
            sys.exit(1)

        
        updater_obj.setInstance(instance, retrieveInfo=False)
        instance_info = updater_obj.getInstanceInformation()

        if instance_info is None:
            print("")
            print("Could not download instance information from repository.")
            sys.exit(1)

        # extract needed data from instance information
        version = float(instance_info["version"])
        rev = int(instance_info["rev"])
        dependencies = instance_info["dependencies"]

        logging.info("[%s]: Checking the dependencies." % fileName)

        
        if options.force is False:
            if not check_dependencies(dependencies):
                sys.exit(1)

        else:
            logging.info("[%s]: Ignoring dependency check. Forcing installation." % fileName)

        
        if updater_obj.updateInstance() is False:
            logging.error("[%s]: Installation failed." % fileName)
            output_failure_and_exit()

        else:
            
            try:
                with open(os.path.join(targetLocation, "instanceInfo.json"), 'w') as fp:
                    fp.write(json.dumps(instance_info))

            except Exception:
                logging.exception("[%s]: Not able to store 'instanceInfo.json'." % fileName)
                output_failure_and_exit()

            print("")
            print("INSTALLATION SUCCESSFUL!")
            print("Please configure this AlertR instance before you start it.")

    
    else:
        print("Use --help to get all available options.")