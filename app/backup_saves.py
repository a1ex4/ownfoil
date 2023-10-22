import threading
import logging
import socket
import time
from ftplib import FTP, error_perm
import time

from utils import *
import logging

# logger = logging
# logger.basicConfig(format='%(asctime)s - %(levelname)s %(name)s: %(message)s', level=logging.INFO)

logger = logging.getLogger("save_manager")

def mk_local_dir(dir):
    Path(dir).mkdir(parents=True, exist_ok=True)


def setInterval(interval, times = -1):
    # This will be the actual decorator,
    # with fixed interval and times parameter
    def outer_wrap(function):
        # This will be the function to be
        # called
        def wrap(*args, **kwargs):
            stop = threading.Event()

            # This is another function to be executed
            # in a different thread to simulate setInterval
            def inner_wrap():
                i = 0
                while i != times and not stop.isSet():
                    stop.wait(interval)
                    function(*args, **kwargs)
                    i += 1

            t = threading.Timer(0, inner_wrap)
            t.daemon = True
            t.start()
            # time.sleep(1)
            # t.join(1)
            stop.set()
            return stop
        return wrap
    return outer_wrap


class PyFTPclient:
    def __init__(self, host, port = 21, login = 'anonymous', passwd = 'anonymous', monitor_interval = 10):
        self.host = host
        self.port = port
        self.login = login
        self.passwd = passwd
        self.monitor_interval = monitor_interval
        self.ptr = None
        self.max_attempts = 15
        self.waiting = True
        self._connected = False

        self.ftp = FTP()
        # self.ftp.set_debuglevel(2)
        self.ftp.set_pasv(True)

        self.connect()
        if self._connected:
            self.ftp.voidcmd('TYPE I')

    def connect(self):
        # logger.debug('connecting')
        try:
            self.ftp.connect(self.host, self.port)
            self.ftp.login(self.login, self.passwd)
            # optimize socket params for download task
            self.ftp.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.ftp.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 75)
            self.ftp.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            self._connected = True
        except OSError:
            logger.info(f'Device {self.host} is unavailable, aborting.')
            self._connected = False

    def reconnect(self):
        self._connected = False
        self.waiting = True
        self.ftp.close()
        logger.info('Lost connection, attempting reconnect in 10s...')
        time.sleep(10)
        logger.debug('Attempt reconnecting after 10s')
        self.connect()

    def get_files(self, path):
        try:
            files = self.ftp.nlst(path)
            return files
        except error_perm:
            logger.error(f'The path {path} does not exists on the remote')
            return []

    def retrieve_saves(self, local_save_folder, remote_folder, children = []):
        logger.debug(f'Retrieving saves from {remote_folder} to {local_save_folder}...')
        files = children if len(children) else self.get_files(remote_folder)
        # Create local folder if it's not empty on remote
        if len(files):
            mk_local_dir(local_save_folder)

        downloaded_files = 0
        for file in files[:]:
            children = self.get_files(file)
            # Found a file, download
            if children == [file]:
                logger.debug(f'Retrieving {file}')
                res = self.DownloadFile(file, local_save_folder + file.replace(remote_folder, ''))
                if res:
                    downloaded_files += res
            # Empty folder, ignore
            elif not len(children):
                logger.debug(f'{file} is an empty folder, ignoring')
                continue
            # Folder with files, recurse
            else:
                local_folder = local_save_folder + file.replace(remote_folder, '')
                downloaded_files += self.retrieve_saves(local_folder, file, children)
        return downloaded_files
    
    def DownloadFile(self, dst_filename, local_filename = None):
        res = ''
        if local_filename is None:
            local_filename = dst_filename

        with open(local_filename, 'w+b') as f:
            self.ptr = f.tell()

            @setInterval(self.monitor_interval)
            def monitor():
                if not self.waiting:
                    i = f.tell()
                    if self.ptr < i:
                        logger.info("%d  -  %0.1f Kb/s" % (i, (i-self.ptr)/(1024*self.monitor_interval)))
                        self.ptr = i
                    else:
                        self.ftp.close()
            while True:
                try:
                    dst_filesize = self.ftp.size(dst_filename)
                    break
                except:
                    self.reconnect()

            if not dst_filesize:
                logger.debug(f'Downloaded file {dst_filename} is empty.')
                return 1

            mon = monitor()
            while dst_filesize > f.tell():
                try:
                    self.connect()
                    self.waiting = False
                    # retrieve file from position where we were disconnected
                    res = self.ftp.retrbinary('RETR %s' % dst_filename, f.write) if f.tell() == 0 else \
                              self.ftp.retrbinary('RETR %s' % dst_filename, f.write, rest=f.tell())

                except Exception as e:
                    self.max_attempts -= 1
                    if self.max_attempts == 0:
                        mon.set()
                        logger.exception('')
                        raise
                    self.reconnect()


            mon.set() #stop monitor
            # self.ftp.close()

            if not res.startswith('226'):
                logger.error(f'Downloaded file {dst_filename} is not full.')
                # os.remove(local_filename)
                return None

            return 1

def backup_saves():
    try:
        switches = config['saves']['switches']
    except KeyError:
        logger.error('Error getting Switch configuration, check the save manager configuration.')
        return
    for switch_conf in switches:
        host = switch_conf['host']
        port = int(switch_conf['port'])
        user = switch_conf.get('user', 'anonymous')
        password = switch_conf.get('pass', 'anonymous')
        switch_ftp = PyFTPclient(host, port, user, password)
        if not switch_ftp._connected:
            continue
        logger.info(f'Successfully connected to Switch device on host {host}.')
        for folder in switch_conf['folders']:
            logger.info(f'Retrieving saves from from {folder["remote"]} to {folder["local"]}')
            start_time = time.time()
            r = switch_ftp.retrieve_saves(config['root_dir'] + '/' + folder['local'], folder['remote'])
            logger.info(f'Retrieved {r} saves in {"{:.3f}s".format(time.time() - start_time)} - from {folder["remote"]} to {folder["local"]}')


# PyFTPclient license from https://github.com/keepitsimple/pyFTPclient/blob/master/LICENSE
# The MIT License (MIT)

# Copyright (c) 2013 keepitsimple

# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.