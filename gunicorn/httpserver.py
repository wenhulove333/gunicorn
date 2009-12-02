# -*- coding: utf-8 -*-
#
# Copyright 2008,2009 Benoit Chesneau <benoitc@e-engura.org>
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at#
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import errno
import fcntl
import logging
import os
import select
import signal
import socket
import sys
import tempfile
import time

from gunicorn.httprequest import HTTPRequest
from gunicorn import socketserver
from gunicorn.util import NullHandler

class Worker(object):
    
    def __init__(self, nr, tmp):
        self.nr = nr
        self.tmp = tmp
        
    def __eq__(self, v):
        return self.nr == v

class HTTPServer(object):
    
    LISTENERS = []
    
    PIPE = []
    
    WORKERS = {}
    
    def __init__(self, app, worker_processes, timeout=60, init_listeners=[], 
                 pidfile=None, logging_handler=None, **opts):
            
        self.opts = opts
        self.app = app
        self.timeout = timeout
        self.pidfile = pidfile
        self.worker_processes = worker_processes
        if logging_handler is None:
            logging_handler = NullHandler()
        self.logger = logging.getLogger("gunicorn")
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(logging_handler)
        
        # start to listen
        self.init_listeners = init_listeners
        if not self.init_listeners:
                self.init_listeners = [(('localhost', 8000), {})]
                
        for address, opts in self.init_listeners:
            self.listen(address, opts)
            
        self.master_pid = os.getpid()
        self.maintain_worker_count()
            
            
    def listen(self, addr, opts):
        tries = self.opts.get('tries', 5)
        delay = self.opts.get('delay', 0.5)
        
        for i in range(tries):
            try:
                sock = socketserver.TCPServer(addr, **opts)
                self.LISTENERS.append(sock)
            except socket.error, e:
                if e[0] == errno.EADDRINUSE:
                    self.logger.error("adding listener failed address: %s" % str(addr))
                if i < tries:
                    self.logger.error("retrying in %s seconds." % str(delay))
                time.sleep(delay)
            break
            
    def join(self):
        # this pipe will be used to wake up the master when signal occurs
        self.init_pipe()
        respawn = True
        while True:
            try:
                #if respawn:
                    #self.maintain_worker_count()
                os.waitpid(-1, os.WNOHANG)
                self.master_sleep()
            except Exception, e:
                self.logger.error("Unhandled exception [%s]" % str(e))
            except KeyboardInterrupt:
                self.kill_workers(signal.SIGQUIT)
                sys.exit()

            
            
    def master_sleep(self):
        while True:
            ready = select.select([self.PIPE[0]], [], [], 1)
            if ready and ready[0]: break
        try:
            while True:
                data = os.read(self.PIPE[0], 4096)
                if len(data) < 4096: return
        except errno.EAGAIN, errno.EINTR:
            pass

    def init_worker_process(self, worker):
        for w in self.WORKERS:
            if w != worker:
                try:
                    w.tmp.close()
                except:
                    continue
            else:
                continue
        [fcntl.fcntl(sock.fileno(), fcntl.F_SETFD, fcntl.FD_CLOEXEC) for sock in self.LISTENERS]
        fcntl.fcntl(worker.tmp.fileno(), fcntl.F_SETFD, fcntl.FD_CLOEXEC)


    def process_client(self, listener, conn, addr):
        """ do nothing just echo message"""
        req = HTTPRequest(conn, addr, listener.getsockname())
        environ = req.read()
        
        req.write(str(environ))
        req.close()
        
    def worker_loop(self, worker):
        pid = os.fork()

        if pid == 0:
            worker_pid = os.getpid()
            yield worker_pid
            self.init_worker_process(worker)
            alive = worker.tmp.fileno()
            m = 0
            ready = self.LISTENERS
            try:
                while alive:
                    m = 0 if m == 1 else 1
                    os.fchmod(alive, m)
                
                    for sock in ready:
                        try:
                            self.process_client(sock, *sock.accept_nonblock())
                        except errno.EAGAIN, errno.ECONNABORTED:
                            pass
                            
                        m = 0 if m == 1 else 1
                        os.fchmod(alive, m)
                    
                    m = 0 if m == 1 else 1   
                    os.fchmod(alive, m)

                    while True:
                        try:
                            fd_sets = select.select(self.LISTENERS, [], self.PIPE, self.timeout)
                            if fd_sets and fd_sets[0]:
                                ready = [fd_sets[0]]
                                break
                        except errno.EINTR:
                            ready = self.LISTENERS
                        except Exception, e:
                            self.logger.error("Unhandled exception in worker %s [%s]" % (worker_pid, e))
                            pass
                    
            except KeyboardInterrupt:
                sys.exit()
            except Exception, e:
                self.logger.error("Unhandled exception in worker %s [%s]" % (worker_pid, e))
    
    def kill_workers(self, sig):
        """kill all workers with signal sig """
        for pid in self.WORKERS.keys():
            self.kill_worker(pid, sig)
        
    def kill_worker(self, pid, sig):
        """ kill one worker with signal """
        worker = self.WORKERS[pid]
        try:
            os.kill(pid, sig) 
        finally:
            worker.tmp.close()
            del self.WORKERS[pid]
        
        
    def spawn_missing_workers(self):
        for i in range(self.worker_processes):
            if i in self.WORKERS.values():
                continue
            
            worker = Worker(i, os.tmpfile())
            for worker_pid in self.worker_loop(worker):
                self.WORKERS[worker_pid] = worker
    
    def maintain_worker_count(self):
        if (len(self.WORKERS.keys()) - self.worker_processes) < 0:
            self.spawn_missing_workers()
            
        for pid, w in self.WORKERS.items():
            if w.nr >= self.worker_processes:
                self.kill_worker(pid, signal.SIGQUIT)   

    def init_pipe(self):
        if self.PIPE:
          [io.close() for io in self.PIPE]
        self.PIPE = os.pipe()
        [fcntl.fcntl(io, fcntl.F_SETFD, fcntl.FD_CLOEXEC) for io in self.PIPE]