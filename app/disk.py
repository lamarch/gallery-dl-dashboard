"""Light, read-only disk scan: counts, sizes and names per album folder.

Only os.scandir/stat and ONE small JSON read per album (for the real title
and account name). Image contents are never opened here.
Layout (gallery-dl facebook defaults):
    <root>/<account display name>/<album title> (<set_id>)/<files>
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

MEDIA = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".m4v", ".mov", ".m4a", ".avif"}
SID = re.compile(r"\(([^()]+)\)\s*$")


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


@dataclass
class AlbumDir:
    set_id: Optional[str]
    folder: str                   # folder name as on disk
    account_folder: str
    path: str
    media: int = 0
    json: int = 0
    part: int = 0
    other: int = 0
    media_without_json: int = 0
    json_without_media: int = 0
    bytes: int = 0
    last_change: float = 0.0      # max ctime (mtime is rewritten by fix_order)
    title: Optional[str] = None   # from JSON
    username: Optional[str] = None
    user_id: Optional[str] = None    # Facebook account ID of the album owner (JSON)
    media_ids: set = field(default_factory=set)


@dataclass
class DiskSnapshot:
    taken: float
    albums: dict                  # set_id -> AlbumDir (or "?folder" key)
    total_media: int = 0
    total_bytes: int = 0
    free_bytes: Optional[int] = None
    fs_bytes: Optional[int] = None
    error: Optional[str] = None
    duration: float = 0.0


RE_ID = re.compile(r"_(\d+)\.[^.]+$")


def scan(root: str, fs_path: Optional[str] = None) -> DiskSnapshot:
    t0 = time.monotonic()
    snap = DiskSnapshot(time.time(), {})
    try:
        st = os.statvfs(fs_path or root)
        snap.free_bytes = st.f_bavail * st.f_frsize
        snap.fs_bytes = st.f_blocks * st.f_frsize
    except OSError:
        pass
    try:
        accounts = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError as e:
        snap.error = f"cannot read {root}: {e.__class__.__name__}"
        return snap
    for acc in accounts:
        try:
            album_entries = [e for e in os.scandir(acc.path) if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for alb in album_entries:
            m = SID.search(alb.name)
            a = AlbumDir(m.group(1) if m else None, alb.name, acc.name, alb.path)
            names = set()
            first_json = None
            try:
                for f in os.scandir(alb.path):
                    if not f.is_file(follow_symlinks=False):
                        continue
                    names.add(f.name)
                    try:
                        fst = f.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    a.last_change = max(a.last_change, fst.st_ctime)
                    ext = os.path.splitext(f.name)[1].lower()
                    if ext == ".json":
                        a.json += 1
                        if first_json is None:
                            first_json = f.path
                    elif ext == ".part":
                        a.part += 1
                    elif ext in MEDIA:
                        a.media += 1
                        a.bytes += fst.st_size
                        mid = RE_ID.search(f.name)
                        if mid:
                            a.media_ids.add(mid.group(1))
                    else:
                        a.other += 1
            except OSError:
                pass
            for n in names:
                base, ext = os.path.splitext(n)
                if ext.lower() == ".json":
                    if base not in names:
                        a.json_without_media += 1
                elif ext.lower() in MEDIA and n + ".json" not in names:
                    a.media_without_json += 1
            if first_json:
                try:
                    with open(first_json, encoding="utf-8") as fh:
                        d = json.load(fh)
                    a.title = d.get("title") or None
                    a.username = d.get("username") or None
                    uid = str(d.get("user_id") or "")
                    a.user_id = uid if uid.isdigit() else None
                    a.set_id = a.set_id or d.get("set_id")
                except (OSError, ValueError, AttributeError):
                    pass
            key = a.set_id or f"?{acc.name}/{alb.name}"
            if key in snap.albums:   # same set in two folders: merge counts
                other = snap.albums[key]
                for k in ("media", "json", "part", "other", "bytes",
                          "media_without_json", "json_without_media"):
                    setattr(other, k, getattr(other, k) + getattr(a, k))
                other.media_ids |= a.media_ids
                other.folder += f" + {a.folder}"
            else:
                snap.albums[key] = a
            snap.total_media += a.media
            snap.total_bytes += a.bytes
    snap.duration = time.monotonic() - t0
    return snap
