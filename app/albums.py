"""albums.txt (gallery-dl -I input file) reader.

gallery-dl's -I comments a line out ("# " prefix) after the URL was
downloaded with exit status 0. Note: an album with skipped photos
("Failed to find photo download URL ... Skipping.") still gets commented out.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .logparse import sanitize_url, set_id_from_url


@dataclass
class InputAlbum:
    pos: int              # 1-based position among album URLs
    line: int             # 1-based line number in the file
    set_id: Optional[str]
    url: str              # sanitized
    done: bool            # commented out by gallery-dl


def _looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://")) or "facebook.com/" in s


def read_albums(path: str) -> tuple[list[InputAlbum], Optional[str]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        return [], f"cannot read {path}: {e.__class__.__name__}"
    out = []
    for n, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("-"):
            continue
        done = False
        if s.startswith("#"):
            inner = s.lstrip("#").strip()
            if not _looks_like_url(inner):
                continue          # a real comment
            s, done = inner, True
        if " #" in s or "\t#" in s:
            s = s.split(" #")[0].split("\t#")[0].strip()
        out.append(InputAlbum(len(out) + 1, n, set_id_from_url(s), sanitize_url(s), done))
    return out, None


def input_mtime(path: str) -> Optional[float]:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def merge_albums(albums: list) -> list:
    """One entry per album: lines pointing to the same set_id (a plain line
    and a "...&setextract" re-download line, or a duplicate) are merged; the
    album is done only when all its lines are. Lines that are not albums
    (no set_id: profile URLs...) are left out."""
    out: dict = {}
    for a in albums:
        if not a.set_id:
            continue
        m = out.get(a.set_id)
        if m is None:
            out[a.set_id] = InputAlbum(a.pos, a.line, a.set_id, a.url, a.done)
        else:
            m.done = m.done and a.done
    return list(out.values())


def non_album_lines(albums: list) -> int:
    return sum(1 for a in albums if not a.set_id)
