import errno
import logging
import os
from threading import Thread, RLock
from thread import get_ident
import time
import sys

from circus import zmq
from zmq.eventloop import ioloop

from circus.controller import Controller
from circus.exc import AlreadyExist
from circus import logger
from circus.watcher import Watcher
from circus.util import debuglog, _setproctitle
from circus.config import get_config
from circus.plugins import get_plugin_cmd
from circus.sockets import CircusSocket, CircusSockets


class Arbiter(object):
    """Class used to control a list of watchers.

    Options:

    - **watchers** -- a list of Watcher objects
    - **endpoint** -- the controller ZMQ endpoint
    - **pubsub_endpoint** -- the pubsub endpoint
    - **stats_endpoint** -- the stats endpoint. If not provided,
      the *circusd-stats* process will not be launched.
    - **check_delay** -- the delay between two controller points
      (default: 1 s)
    - **prereload_fn** -- callable that will be executed on each reload
      (default: None)
    - **context** -- if provided, the zmq context to reuse.
      (default: None)
    - **loop**: if provided, a :class:`zmq.eventloop.ioloop.IOLoop` instance
       to reuse. (default: None)
    - **plugins** -- a list of plugins. Each item is a mapping with:

        - **use** -- Fully qualified name that points to the plugin class
        - every other value is passed to the plugin in the **config** option
    - **sockets** -- a mapping of sockets. Each key is the socket name,
      and each value a :class:`CircusSocket` class. (default: None)
    - **warmup_delay** -- a delay in seconds between two watchers startup.
      (default: 0)
    - **httpd** -- If True, a circushttpd process is run (default: False)
    - **httpd_host** -- the circushttpd host (default: localhost)
    - **httpd_port** -- the circushttpd port (default: 8080)
    - **debug** -- if True, adds a lot of debug info in the stdout (default:
      False)
    - **stream_backend** -- the backend that will be used for the streaming
      process. Can be *thread* or *gevent*. When set to *gevent* you need
      to have *gevent* and *gevent_zmq* installed.
      All watchers will use this setup unless stated otherwise in the
      watcher configuration. (default: thread)
    - **proc_name** -- the arbiter process name
    """
    def __init__(self, watchers, endpoint, pubsub_endpoint, check_delay=1.,
                 prereload_fn=None, context=None, loop=None,
                 stats_endpoint=None, plugins=None, sockets=None,
                 warmup_delay=0, httpd=False, httpd_host='localhost',
                 httpd_port=8080, debug=False, stream_backend='thread',
                 ssh_server=None, proc_name='circusd'):
        self.stream_backend = stream_backend
        self.watchers = watchers
        self.endpoint = endpoint
        self.check_delay = check_delay
        self.prereload_fn = prereload_fn
        self.pubsub_endpoint = pubsub_endpoint
        self.proc_name = proc_name

        # initialize zmq context
        self.context = context or zmq.Context.instance()
        self.loop = loop or ioloop.IOLoop()
        self.ctrl = Controller(endpoint, self.context, self.loop, self,
                               check_delay)

        self.pid = os.getpid()
        self._watchers_names = {}
        self.alive = True
        self._lock = RLock()
        self.debug = debug
        if self.debug:
            stdout_stream = stderr_stream = {'class': 'StdoutStream'}
        else:
            stdout_stream = stderr_stream = None

        # initializing circusd-stats as a watcher when configured
        self.stats_endpoint = stats_endpoint
        if self.stats_endpoint is not None:
            cmd = "%s -c 'from circus import stats; stats.main()'" % \
                sys.executable
            cmd += ' --endpoint %s' % self.endpoint
            cmd += ' --pubsub %s' % self.pubsub_endpoint
            cmd += ' --statspoint %s' % self.stats_endpoint
            if ssh_server is not None:
                cmd += ' --ssh %s' % ssh_server
            if debug:
                cmd += ' --log-level DEBUG'
            stats_watcher = Watcher('circusd-stats', cmd, use_sockets=True,
                                    singleton=True,
                                    stdout_stream=stdout_stream,
                                    stderr_stream=stderr_stream,
                                    stream_backend=self.stream_backend,
                                    copy_env=True, copy_path=True)
            self.watchers.append(stats_watcher)

        # adding the httpd
        if httpd:
            cmd = ("%s -c 'from circus.web import circushttpd; "
                   "circushttpd.main()'") % sys.executable
            cmd += ' --endpoint %s' % self.endpoint
            cmd += ' --fd $(circus.sockets.circushttpd)'
            if ssh_server is not None:
                cmd += ' --ssh %s' % ssh_server
            httpd_watcher = Watcher('circushttpd', cmd, use_sockets=True,
                                    singleton=True,
                                    stdout_stream=stdout_stream,
                                    stderr_stream=stderr_stream,
                                    stream_backend=self.stream_backend,
                                    copy_env=True, copy_path=True)
            self.watchers.append(httpd_watcher)
            httpd_socket = CircusSocket(name='circushttpd', host=httpd_host,
                                        port=httpd_port)

            # adding the socket
            if sockets is None:
                sockets = [httpd_socket]
            else:
                sockets.append(httpd_socket)

        # adding each plugin as a watcher
        if plugins is not None:
            for plugin in plugins:
                fqnd = plugin['use']
                name = 'plugin:%s' % fqnd.replace('.', '-')
                cmd = get_plugin_cmd(plugin, self.endpoint,
                                     self.pubsub_endpoint, self.check_delay,
                                     ssh_server, debug=self.debug)
                plugin_watcher = Watcher(name, cmd, priority=1, singleton=True,
                                         stdout_stream=stdout_stream,
                                         stderr_stream=stderr_stream,
                                         stream_backend=self.stream_backend,
                                         copy_env=True, copy_path=True)
                self.watchers.append(plugin_watcher)

        self.sockets = CircusSockets(sockets)
        self.warmup_delay = warmup_delay

    def get_socket(self, name):
        for i in self.sockets:
            if i.name == name:
                return i

        return None

    @classmethod
    def get_socket_config(cls, config, name):
        for i in config.get('sockets', []):
            if i['name'] == name:
                return i
        return None

    @classmethod
    def get_watcher_config(cls, config, name):
        for i in config.get('watchers', []):
            if i['name'] == name:
                return i
        return None

    @classmethod
    def cfg2dict(cls, cfg):
        return dict(
            endpoint=cfg['endpoint'],
            pubsub_endpoint=cfg['pubsub_endpoint'],
            check_delay=cfg.get('check_delay', 1.),
            prereload_fn=cfg.get('prereload_fn'),
            stats_endpoint=cfg.get('stats_endpoint'),
            plugins=cfg.get('plugins'),
            warmup_delay=cfg.get('warmup_delay', 0),
            httpd=cfg.get('httpd', False),
            httpd_host=cfg.get('httpd_host', 'localhost'),
            httpd_port=cfg.get('httpd_port', 8080),
            debug=cfg.get('debug', False),
            stream_backend=cfg.get('stream_backend', 'thread'),
            ssh_server=cfg.get('ssh_server', None),
    )

    def reload_from_config(self, config_file=None):
        cfg = get_config(config_file if config_file else self.config_file)

        # if arbiter is changed, reload everything
        if self.cfg2dict(cfg) != self.cfg:
            return self.load_from_config(config_file if config_file else self.config_file)

        current_socket_names = set([i.name for i in self.sockets])
        new_socket_names = set([i['name'] for i in cfg.get('sockets', [])])
        added_socket_names = new_socket_names - current_socket_names
        deleted_socket_names = current_socket_names - new_socket_names
        maybechanged_socket_names = current_socket_names - deleted_socket_names
        changed_socket_names = set([])
        watcher_names_with_changed_socket = set([])
        watcher_names_with_deleted_socket = set([])

        # get changed sockets
        for n in maybechanged_socket_names:
            s = self.get_socket(n)
            if s.cfg2dict(self.get_socket_config(cfg, n)) != s.cfg:
                changed_socket_names.add(n)

                # just delete the socket and add it again
                deleted_socket_names.add(n)
                added_socket_names.add(n)

                # Get the watchers whichs use these, so they could be deleted and added also
                for w in self.iter_watchers:
                    if 'circus.sockets.%s' % n.lower() in w.cmd:
                        watcher_names_with_changed_socket.add(w.name)

        # get deleted sockets
        for n in deleted_socket_names:
            s = self.get_socket(n)
            s.close()
            # Get the watchers whichs use these, these should not be active anymore
            for w in self.iter_watchers():
                if 'circus.sockets.%s' % n.lower() in w.cmd:
                    watcher_names_with_deleted_socket.add(w.name)
            self.sockets.remove(s)

        # get added sockets
        for n in added_socket_names:
            s = CircusSocket.load_from_config(self.get_socket_config(cfg, n))
            s.bind_and_listen()
            self.sockets.append(s)

        if added_socket_names or deleted_socket_names:
            # make sure all existing watchers get the new sockets in their attributes and get the old removed
            for watcher in self.iter_watchers():
                watcher.initialize(self.evpub_socket, self.sockets, self)

        current_watcher_names = set([i.name for i in self.iter_watchers()])
        new_watcher_names = set([i['name'] for i in cfg.get('watchers', [])])
        added_watcher_names = (new_watcher_names - current_watcher_names) | watcher_names_with_changed_socket
        deleted_watcher_names = current_watcher_names - new_watcher_names - watcher_names_with_changed_socket
        maybechanged_watcher_names = current_watcher_names - deleted_watcher_names
        changed_watcher_names = set([])

        if watcher_names_with_deleted_socket and watcher_names_with_deleted_socket not in new_watcher_names:
            raise ValueError('Watchers %s uses a socket which is deleted' % watcher_names_with_deleted_socket)

        #get changed watchers
        for n in maybechanged_watcher_names:
            w = self.get_watcher(n)
            if w.cfg2dict(self.get_watcher_config(cfg, n)) != w.cfg:
#                if changed_keys == 'numprocesses':
                if False:
                    # if only the number of processes is changed, just changes this
                    w.set_numprocesses(int(cfg['numprocesses']))
                else:
                    # Other thing are changed. Just delete and add the watcher.
                    changed_watcher_names.add(n)
                    deleted_watcher_names.add(n)
                    added_watcher_names.add(n)

        # get deleted watchers
        for n in deleted_watcher_names:
            w = self.get_watcher(n)
            w.stop()
            del self._watchers_names[w.name.lower()]
            self.watchers.remove(w)

        # get added watchers
        for n in added_watcher_names:
            w = Watcher.load_from_config(self.get_watcher_config(cfg, n))
            w.initialize(self.evpub_socket, self.sockets, self)
            w.start()
            self.watchers.append(w)
            self._watchers_names[w.name.lower()] = w

    @classmethod
    def load_from_config(cls, config_file):
        cfg = get_config(config_file)

        watchers = []
        for watcher in cfg.get('watchers', []):
            watchers.append(Watcher.load_from_config(watcher))

        sockets = []
        for socket in cfg.get('sockets', []):
            sockets.append(CircusSocket.load_from_config(socket))

        # creating arbiter
        arbiter = cls(watchers, cfg['endpoint'], cfg['pubsub_endpoint'],
                      check_delay=cfg.get('check_delay', 1.),
                      prereload_fn=cfg.get('prereload_fn'),
                      stats_endpoint=cfg.get('stats_endpoint'),
                      plugins=cfg.get('plugins'), sockets=sockets,
                      warmup_delay=cfg.get('warmup_delay', 0),
                      httpd=cfg.get('httpd', False),
                      httpd_host=cfg.get('httpd_host', 'localhost'),
                      httpd_port=cfg.get('httpd_port', 8080),
                      debug=cfg.get('debug', False),
                      stream_backend=cfg.get('stream_backend', 'thread'),
                      ssh_server=cfg.get('ssh_server', None))

        # store the cfg which will be used, so it can be used later for checking if the cfg has been changed
        arbiter.cfg = arbiter.cfg2dict(cfg)

        arbiter.config_file = config_file

        return arbiter

    def iter_watchers(self, reverse=True):
        watchers = [(watcher.priority, watcher) for watcher in self.watchers]
        watchers.sort(reverse=reverse)
        for __, watcher in watchers:
            yield watcher

    @debuglog
    def initialize(self):
        # set process title
        _setproctitle(self.proc_name)

        # event pub socket
        self.evpub_socket = self.context.socket(zmq.PUB)
        self.evpub_socket.bind(self.pubsub_endpoint)
        self.evpub_socket.linger = 0

        # initialize sockets
        if len(self.sockets) > 0:
            self.sockets.bind_and_listen_all()
            logger.info("sockets started")

        # initialize watchers
        for watcher in self.iter_watchers():
            self._watchers_names[watcher.name.lower()] = watcher
            watcher.initialize(self.evpub_socket, self.sockets, self)

    @debuglog
    def start(self):
        """Starts all the watchers.

        The start command is an infinite loop that waits
        for any command from a client and that watches all the
        processes and restarts them if needed.
        """
        logger.info("Starting master on pid %s", self.pid)

        self.initialize()

        # start controller
        self.ctrl.start()

        try:
            # initialize processes
            logger.debug('Initializing watchers')
            for watcher in self.iter_watchers():
                watcher.start()
                time.sleep(self.warmup_delay)

            logger.info('Arbiter now waiting for commands')

            while True:
                try:
                    self.loop.start()
                except zmq.ZMQError as e:
                    if e.errno == errno.EINTR:
                        continue
                    else:
                        raise
                else:
                    break
        finally:
            self.ctrl.stop()
            self.evpub_socket.close()

    def stop(self):
        if self.alive:
            self.stop_watchers(stop_alive=True)

        self.loop.stop()

        # close sockets
        self.sockets.close_all()

    def reap_processes(self):
        # map watcher to pids
        watchers_pids = {}
        for watcher in self.iter_watchers():
            if not watcher.stopped:
                for process in watcher.processes.values():
                    watchers_pids[process.pid] = watcher

        # detect dead children
        while True:
            try:
                # wait for our child (so it's not a zombie)
                pid, status = os.waitpid(-1, os.WNOHANG)
                if not pid:
                    break

                if pid in watchers_pids:
                    watcher = watchers_pids[pid]
                    watcher.reap_process(pid, status)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    time.sleep(0.001)
                    continue
                elif e.errno == errno.ECHILD:
                    # process already reaped
                    return
                else:
                    raise

    def manage_watchers(self):
        if not self.alive:
            return

        with self._lock:
            # manage and reap processes
            self.reap_processes()
            for watcher in self.iter_watchers():
                watcher.manage_processes()

    @debuglog
    def reload(self, graceful=True):
        """Reloads everything.

        Run the :func:`prereload_fn` callable if any, then gracefuly
        reload all watchers.
        """
        if self.prereload_fn is not None:
            self.prereload_fn(self)

        # reopen log files
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.acquire()
                handler.stream.close()
                handler.stream = open(handler.baseFilename, handler.mode)
                handler.release()

        # gracefully reload watchers
        for watcher in self.iter_watchers():
            watcher.reload(graceful=graceful)
            time.sleep(self.warmup_delay)

    def numprocesses(self):
        """Return the number of processes running across all watchers."""
        return sum([len(watcher) for watcher in self.watchers])

    def numwatchers(self):
        """Return the number of watchers."""
        return len(self.watchers)

    def get_watcher(self, name):
        """Return the watcher *name*."""
        return self._watchers_names[name]

    def statuses(self):
        return dict([(watcher.name, watcher.status())
                     for watcher in self.watchers])

    def add_watcher(self, name, cmd, **kw):
        """Adds a watcher.

        Options:

        - **name**: name of the watcher to add
        - **cmd**: command to run.
        - all other options defined in the Watcher constructor.
        """
        if name in self._watchers_names:
            raise AlreadyExist("%r already exist" % name)

        if not name:
            return ValueError("command name shouldn't be empty")

        watcher = Watcher(name, cmd, **kw)
        watcher.initialize(self.evpub_socket, self.sockets, self)
        self.watchers.append(watcher)
        self._watchers_names[watcher.name.lower()] = watcher
        return watcher

    def rm_watcher(self, name):
        """Deletes a watcher.

        Options:

        - **name**: name of the watcher to delete
        """
        logger.debug('Deleting %r watcher', name)

        # remove the watcher from the list
        watcher = self._watchers_names.pop(name)
        del self.watchers[self.watchers.index(watcher)]

        # stop the watcher
        watcher.stop()

    def start_watchers(self):
        for watcher in self.iter_watchers():
            watcher.start()
            time.sleep(self.warmup_delay)

    def stop_watchers(self, stop_alive=False):
        if not self.alive:
            return

        if stop_alive:
            logger.info('Arbiter exiting')
            self.alive = False

        for watcher in self.iter_watchers(reverse=False):
            watcher.stop()

    def restart(self):
        self.stop_watchers()
        self.start_watchers()


class ThreadedArbiter(Arbiter, Thread):

    def __init__(self, *args, **kw):
        Thread.__init__(self)
        Arbiter.__init__(self, *args, **kw)

    def start(self):
        return Thread.start(self)

    def run(self):
        return Arbiter.start(self)

    def stop(self):
        Arbiter.stop(self)
        if get_ident() != self.ident:
            self.join()
