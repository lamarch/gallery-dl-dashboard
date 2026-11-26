"""Acknowledged log events (warnings / problems the user has seen).

Stored in acknowledged-events.json in the data folder:
    {"keys": {"<event key>": "<when acknowledged>"}}
Event.key is stable across restarts (same log -> same keys), so an
acknowledged event stays acknowledged; new events are never covered.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional


class Acks:
    def __init__(self, path: Optional[str]):
        self.path = path
        self.lock = threading.Lock()
        self.keys: dict = {}
        self._mtime = None

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        m = os.stat(self.path).st_mtime
        if m == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.keys = dict(json.load(fh).get("keys") or {})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self._mtime = m

    def _write(self):
        if not self.path:
            raise OSError("no writable data folder: acknowledgements cannot be saved")
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"keys": self.keys}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self._mtime = os.stat(self.path).st_mtime

    def checker(self):
        """-> function(event) -> bool, consistent for one page render."""
        with self.lock:
            self._load()
            keys = frozenset(self.keys)
        return lambda ev: ev.key in keys

    def ack(self, keys):
        with self.lock:
            self._load()
            now = time.strftime("%Y-%m-%d %H:%M")
            for k in keys:
                if k:
                    self.keys[k] = now
            self._write()

    def unack(self, keys):
        with self.lock:
            self._load()
            for k in keys:
                self.keys.pop(k, None)
            self._write()
