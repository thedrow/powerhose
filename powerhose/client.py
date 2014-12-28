# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
import threading
from Queue import Queue
from collections import defaultdict
import errno
import contextlib

import zmq

from powerhose.exc import TimeoutError, ExecutionError, NoWorkerError
from powerhose.job import Job
from powerhose.util import (send, recv, DEFAULT_FRONTEND, logger,
                            extract_result, timed)


DEFAULT_TIMEOUT = 5.
DEFAULT_TIMEOUT_MOVF = 7.5
DEFAULT_TIMEOUT_OVF = 1


class Client(object):
    """Class to call a Powerhose cluster.

    Options:

    - **frontend**: ZMQ socket to call.
    - **timeout**: maximum allowed time for a job to run.
      Defaults to 1s.
    - **timeout_max_overflow**: maximum timeout overflow allowed.
      Defaults to 1.5s
    - **timeout_overflows**: number of times in a row the timeout value
      can be overflowed per worker. The client keeps a counter of
      executions that were longer than the regular timeout but shorter
      than **timeout_max_overflow**. When the number goes over
      **timeout_overflows**, the usual TimeoutError is raised.
      When a worker returns on time, the counter is reset.
    """
    def __init__(self, frontend=DEFAULT_FRONTEND, timeout=DEFAULT_TIMEOUT,
                 timeout_max_overflow=DEFAULT_TIMEOUT_MOVF,
                 timeout_overflows=DEFAULT_TIMEOUT_OVF,
                 debug=False, ctx=None):
        self.kill_ctx = ctx is None
        self.ctx = ctx or zmq.Context()
        self.frontend = frontend
        self.master = self.ctx.socket(zmq.REQ)
        self.master.connect(frontend)
        logger.debug('Client connected to %s' % frontend)
        self.poller = zmq.Poller()
        self.poller.register(self.master, zmq.POLLIN)
        self.timeout = timeout * 1000
        self.lock = threading.Lock()
        self.timeout_max_overflow = timeout_max_overflow * 1000
        self.timeout_overflows = timeout_overflows
        self.timeout_counters = defaultdict(int)
        self.debug = debug

    def execute(self, job, timeout=None):
        """Runs the job

        Options:

        - **job**: Job to be performed. Can be a :class:`Job`
          instance or a string. If it's a string a :class:`Job` instance
          will be automatically created out of it.
        - **timeout**: maximum allowed time for a job to run.
          If not provided, uses the one defined in the constructor.

        If the job fails after the timeout, raises a :class:`TimeoutError`.

        This method is thread-safe and uses a lock. If you need to execute a
        lot of jobs simultaneously on a broker, use the :class:`Pool` class.

        """
        if timeout is None:
            timeout = self.timeout_max_overflow

        try:
            duration, res = timed(self.debug)(self._execute)(job, timeout)
            worker_pid, res, data = res

            # if we overflowed we want to increment the counter
            # if not we reset it
            if duration * 1000 > self.timeout:
                self.timeout_counters[worker_pid] += 1

                # XXX well, we have the result but we want to timeout
                # nevertheless because that's been too much overflow
                if self.timeout_counters[worker_pid] > self.timeout_overflows:
                    raise TimeoutError(timeout / 1000)
            else:
                self.timeout_counters[worker_pid] = 0

            if not res:
                if data == 'No worker':
                    raise NoWorkerError()
                raise ExecutionError(data)
        except Exception:
            # logged, connector replaced.
            logger.exception('Failed to execute the job.')
            raise

        return data

    def ping(self, timeout=1.):
        """Can be used to simply ping the broker to make sure
        it's responsive.


        Returns the broker PID"""
        with self.lock:
            send(self.master, 'PING')
            while True:
                try:
                    socks = dict(self.poller.poll(timeout * 1000))
                    break
                except zmq.ZMQError as e:
                    if e.errno != errno.EINTR:
                        return None

        if socks.get(self.master) == zmq.POLLIN:
            res = recv(self.master)
            try:
                res = int(res)
            except TypeError:
                pass
            return res

        return None

    def close(self):
        #self.master.close()
        self.master.setsockopt(zmq.LINGER, 0)
        self.master.close()

        if self.kill_ctx:
            self.ctx.destroy(0)

    def _execute(self, job, timeout=None):
        if isinstance(job, str):
            job = Job(job)

        if timeout is None:
            timeout = self.timeout_max_overflow

        with self.lock:
            send(self.master, job.serialize())

            while True:
                try:
                    socks = dict(self.poller.poll(timeout))
                    break
                except zmq.ZMQError as e:
                    if e.errno != errno.EINTR:
                        raise

        if socks.get(self.master) == zmq.POLLIN:
            return extract_result(recv(self.master))

        raise TimeoutError(timeout / 1000)


class Pool(object):
    """The pool class manage several :class:`Client` instances
    and publish the same interface,

    Options:

    - **size**: size of the pool. Defaults to 10.
    - **frontend**: ZMQ socket to call.
    - **timeout**: maximum allowed time for a job to run.
      Defaults to 5s.
    - **timeout_max_overflow**: maximum timeout overflow allowed
    - **timeout_overflows**: number of times in a row the timeout value
      can be overflowed per worker. The client keeps a counter of
      executions that were longer than the regular timeout but shorter
      than **timeout_max_overflow**. When the number goes over
      **timeout_overflows**, the usual TimeoutError is raised.
      When a worker returns on time, the counter is reset.
    """
    def __init__(self, size=10, frontend=DEFAULT_FRONTEND,
                 timeout=DEFAULT_TIMEOUT,
                 timeout_max_overflow=DEFAULT_TIMEOUT_MOVF,
                 timeout_overflows=DEFAULT_TIMEOUT_OVF,
                 debug=False, ctx=None):
        self._connectors = Queue()
        self.frontend = frontend
        self.timeout = timeout
        self.timeout_overflows = timeout_overflows
        self.timeout_max_overflow = timeout_max_overflow
        self.debug = debug
        self.ctx = ctx or zmq.Context()

        for i in range(size):
            self._connectors.put(self._create_client())

    def _create_client(self):
        return Client(self.frontend, self.timeout,
                      self.timeout_max_overflow, self.timeout_overflows,
                      debug=self.debug, ctx=self.ctx)

    @contextlib.contextmanager
    def _connector(self, timeout):
        connector = self._connectors.get(timeout=timeout)
        try:
            yield connector
        except Exception:
            # connector replaced
            try:
                connector.close()
            finally:
                self._connectors.put(self._create_client())
            raise
        else:
            self._connectors.put(connector)

    def execute(self, job, timeout=None):
        with self._connector(timeout) as connector:
            return connector.execute(job, timeout)

    def close(self):
        self.ctx.destroy(0)

    def ping(self, timeout=.1):
        with self._connector(self.timeout) as connector:
            return connector.ping(timeout)
