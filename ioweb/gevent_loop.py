from queue import Empty
import time
import sys
import logging
from collections import deque
from threading import Thread

from urllib3 import PoolManager
import gevent

from .urllib3_transport import Urllib3Transport
from .util import debug
from .response import Response
from .error import NetworkError
from .urllib3_custom import CustomPoolManager

network_logger = logging.getLogger('ioweb.network')


class GeventLoop(object):
    def __init__(
            self, taskq, resultq, threads=3, resultq_size_limit=None,
            shutdown_event=None,
            pause=None,
            setup_handler_hook=None
        ):
        if resultq_size_limit is None:
            resultq_size_limit = threads * 2
        self.setup_handler_hook = setup_handler_hook
        self.taskq = taskq
        self.resultq = resultq
        self.resultq_size_limit = resultq_size_limit
        self.idle_handlers = set()
        self.active_handlers = set()
        self.registry = {}
        self.shutdown_event = shutdown_event
        self.pause = pause
        self.threads = threads
        self.pool = CustomPoolManager()
        for _ in range(threads):
            ref = object()
            self.idle_handlers.add(ref)
            self.registry[ref] = {
                'transport': Urllib3Transport(pool=self.pool),
                'request': None,
                'response': None,
                'start': None,
            }

    def run(self):
        task = None

        while not self.shutdown_event.is_set():
            if self.pause.pause_event.is_set():
                if (
                        task is None
                        and not len(self.active_handlers)
                        and len(self.idle_handlers) == self.threads
                    ):
                    self.pause.process_pause()

            if (
                    task is None
                    and
                    self.resultq.qsize() < self.resultq_size_limit
                ):
                try:
                    task = self.taskq.get(False)
                except Empty:
                    pass

            # TODO: convert idle_handlers into queue, blocking wait on
            # next idle handler if task available
            if task:
                if len(self.idle_handlers):
                    self.start_request_thread(task)
                    task = None
                else:
                    time.sleep(0.01)
            else:
                time.sleep(0.01)

    def start_request_thread(self, req):
        ref = self.idle_handlers.pop()
        transport = self.registry[ref]['transport']
        res = Response()
        transport.prepare_request(req, res)
        self.active_handlers.add(ref)
        if req.retry_count > 0:
            retry_str = ' [retry #%d]' % req.retry_count
        else:
            retry_str = ''
        network_logger.debug(
            'GET %s%s', req['url'], retry_str
        )
        self.registry[ref].update({
            'request': req,
            'response': res,
            'start': time.time(),
        })

        if self.setup_handler_hook:
            self.setup_handler_hook(transport, req)

        gevent.spawn(
            self.thread_network,
            ref,
            transport,
            req,
            res
        )

    def thread_network(self, ref, transport, req, res):
        try:
            try:
                transport.request(req, res)
            except NetworkError as ex:
                #logging.exception('asdf')
                error = ex
            else:
                error = None
            self.handle_completed_request(
                ref,
                transport,
                req,
                res,
                error
            )
        finally:
            self.free_handler(ref)

    def free_handler(self, ref):
        self.active_handlers.remove(ref)
        self.idle_handlers.add(ref)
        self.registry[ref]['request'] = None
        self.registry[ref]['response'] = None
        self.registry[ref]['start'] = None

    def handle_completed_request(self, ref, transport, req, res, err):
        transport.prepare_response(req, res, err)
        self.resultq.put({
            'request': req,
            'response': res,
        })
