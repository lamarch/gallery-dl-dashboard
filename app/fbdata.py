"""Facebook album data extracted from saved pages: facebook-albums.json in the
data folder. Only album ID, title, count and the page's title are kept; the
uploaded page itself is never written to disk.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional


class FbStore:
    def __init__(self, path: Optional[str]):
        self.path = path
        self.lock = threading.Lock()
        self.albums: dict = {}       # set_id -> {"title","count","approximate","page","imported"}
        self._mtime = None

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        m = os.stat(self.path).st_mtime
        if m == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.albums = dict(json.load(fh).get("albums") or {})
        except (OSError, ValueError, AttributeError):
            pass
        self._mtime = m

    def _write(self):
        if not self.path:
            raise OSError("no writable data folder: the import cannot be saved")
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"albums": self.albums}, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self._mtime = os.stat(self.path).st_mtime

    def snapshot(self) -> dict:
        with self.lock:
            self._load()
            return {k: dict(v) for k, v in self.albums.items()}

    def counts(self) -> dict:
        """{set_id: (count, approximate)} for albums with a count."""
        return {k: (v["count"], bool(v.get("approximate")))
                for k, v in self.snapshot().items() if v.get("count") is not None}

    def titles(self) -> dict:
        return {k: v["title"] for k, v in self.snapshot().items() if v.get("title")}

    def import_page(self, page) -> dict:
        """Merge an FbPage: its albums replace older data for the same IDs."""
        with self.lock:
            self._load()
            now = time.strftime("%Y-%m-%d %H:%M")
            new = updated = 0
            for sid, a in page.albums.items():
                old = self.albums.get(sid)
                rec = {"title": a.title, "count": a.count, "approximate": a.approximate,
                       "page": page.page_title or "?", "imported": now}
                if old is None:
                    new += 1
                elif (old.get("title"), old.get("count")) != (a.title, a.count):
                    updated += 1
                self.albums[sid] = rec
            self._write()
            return {"albums": len(page.albums), "new": new, "updated": updated}

    def forget_page(self, page_title: str):
        with self.lock:
            self._load()
            self.albums = {k: v for k, v in self.albums.items() if v.get("page") != page_title}
            self._write()
