"""Bounded, visitor-scoped progress snapshots; no prompts or tool payloads."""

import threading
import time

from ..progress import TurnProgress


class ProgressStore:
    def __init__(self, *, capacity=256, per_owner=16, ttl=600, clock=time.monotonic):
        self.capacity, self.per_owner, self.ttl = capacity, per_owner, ttl
        self.clock = clock
        self._items = {}
        self._lock = threading.Lock()

    def _prune(self):
        now = self.clock()
        for key, (created, _) in list(self._items.items()):
            if now - created >= self.ttl:
                del self._items[key]

    def begin(self, owner, session, turn):
        progress = TurnProgress()
        key = owner, session, turn
        with self._lock:
            self._prune()
            # An overlapping request must not replace another turn's progress.
            if key not in self._items:
                owned = [item for item in self._items if item[0] == owner]
                if len(owned) >= self.per_owner:
                    del self._items[owned[0]]
                if len(self._items) >= self.capacity:
                    del self._items[next(iter(self._items))]
                self._items[key] = self.clock(), progress
        return progress

    def get(self, owner, session, turn):
        with self._lock:
            self._prune()
            item = self._items.get((owner, session, turn))
            return item[1].snapshot() if item else None

    def forget(self, owner, session):
        with self._lock:
            for key in list(self._items):
                if key[:2] == (owner, session):
                    del self._items[key]

    def close(self):
        with self._lock:
            self._items.clear()
