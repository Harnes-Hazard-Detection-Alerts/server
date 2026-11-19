"""Bounded per-process login throttling for the local backend."""
from collections import OrderedDict
from threading import BoundedSemaphore, Lock
from time import monotonic


class LoginLimits:
    def __init__(self):
        self.lock = Lock()
        self.clients = OrderedDict()
        self.workers = BoundedSemaphore(2)

    def allow(self, address):
        now = monotonic()
        with self.lock:
            hits = [stamp for stamp in self.clients.pop(address, []) if now - stamp < 60]
            allowed = len(hits) < 10
            if allowed:
                hits.append(now)
            self.clients[address] = hits
            while len(self.clients) > 1024:
                self.clients.popitem(last=False)
            return allowed
