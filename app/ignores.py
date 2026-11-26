"""Missing photos the user decided to ignore (not part of the album, deleted
from Facebook...), stored in ignored-missing.tsv in the data folder:
    set_id  photo_id  reason  date
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

COLUMNS = ("set_id", "photo_id", "reason", "date")


class Ignores:
    def __init__(self, path: Optional[str]):
        self.path = path
        self.lock = threading.Lock()
        self.rows: dict = {}          # (set_id, photo_id) -> (reason, date)
        self._mtime = None

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        m = os.stat(self.path).st_mtime
        if m == self._mtime:
            return
        rows = {}
        with open(self.path, encoding="utf-8") as fh:
            for i, line in enumerate(fh.read().splitlines()):
                p = line.split("\t")
                if i == 0 or len(p) < 2 or not p[0].strip():
                    continue
                rows[(p[0].strip(), p[1].strip())] = (p[2] if len(p) > 2 else "", p[3] if len(p) > 3 else "")
        self.rows, self._mtime = rows, m

    def _write(self):
        if not self.path:
            raise OSError("no writable data folder")
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\t".join(COLUMNS) + "\n")
            for (sid, pid), (reason, date) in sorted(self.rows.items()):
                clean = reason.replace("\t", " ").replace("\n", " ")
                fh.write(f"{sid}\t{pid}\t{clean}\t{date}\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self._mtime = os.stat(self.path).st_mtime

    def snapshot(self) -> dict:
        """{set_id: {photo_id: reason}}"""
        with self.lock:
            self._load()
            out: dict = {}
            for (sid, pid), (reason, _) in self.rows.items():
                out.setdefault(sid, {})[pid] = reason
            return out

    def add(self, sid: str, pid: str, reason: str):
        with self.lock:
            self._load()
            clean = " ".join(reason.replace("\t", " ").split())[:200]
            self.rows[(sid, pid)] = (clean, time.strftime("%Y-%m-%d %H:%M"))
            self._write()

    def remove(self, sid: str, pid: str):
        with self.lock:
            self._load()
            self.rows.pop((sid, pid), None)
            self._write()
