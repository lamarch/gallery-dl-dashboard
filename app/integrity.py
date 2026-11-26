"""Deep, read-only integrity check of the downloaded library.

Answers "is everything on disk, complete and readable?":

* per file: real format vs extension, structure complete (JPEG EOI, PNG IEND,
  MP4 boxes...), full image decode (JPEG at 1/8 scale: every scan is still
  parsed, so truncation/corruption is caught), size, dimensions;
* per album (grouped by the JSON set_id, so renamed folders still work):
  the next_photo_id chain and the 'num' positions written by gallery-dl reveal
  photos that are missing, with their IDs when known;
* JSON sidecars: present, parseable, consistent with filename and folder;
* duplicates: same photo ID twice, identical image data (JPEG hashed from the
  first scan marker, so EXIF rewrites by fix_order do not hide duplicates),
  and visually near-identical images (difference hash);
* the gallery-dl archive vs the disk: an archived photo whose file is gone
  will never be downloaded again.

Files are only ever opened for reading. Results are cached per file
(path + size + mtime + inode) in SQLite, so later passes only check changes.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .logparse import END_OK, END_REASONS

try:
    from PIL import Image, features
    Image.MAX_IMAGE_PIXELS = 300_000_000
    HAVE_PIL = True
    PIL_AVIF = ("avif" in getattr(features, "modules", {})
                and bool(features.check_module("avif")))
except Exception:  # pragma: no cover - Pillow is in requirements
    HAVE_PIL = False
    PIL_AVIF = False

CHECK_VERSION = 2            # bump when the per-file checks change
TRANSIENT = 300              # seconds: very new files may still lack their JSON

EXT_FMT = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".gif": "gif",
           ".webp": "webp", ".avif": "avif", ".heic": "heic",
           ".mp4": "mp4", ".m4v": "mp4", ".mov": "mp4", ".m4a": "mp4"}
MEDIA_EXT = set(EXT_FMT)
VIDEO_FMT = {"mp4"}
SID_IN_DIR = re.compile(r"\(([^()]+)\)\s*$")
ID_IN_NAME = re.compile(r"_(\d+)\.[^.]+$")


# =============================================================================
# Per-file checks
# =============================================================================
@dataclass
class FileResult:
    path: str
    size: int
    mtime_ns: int
    ino: int
    fmt: str = "unknown"          # detected from content
    ok: bool = True
    problem: Optional[str] = None
    note: Optional[str] = None
    chash: Optional[str] = None   # content hash (see module doc)
    dhash: Optional[int] = None   # 64-bit difference hash
    width: Optional[int] = None
    height: Optional[int] = None
    # embedded capture date, as written by fix_order (read-only extraction)
    dt_kind: Optional[str] = None     # exif|xmp|qt
    dt_value: Optional[str] = None    # "YYYY:MM:DD HH:MM:SS" (exif: local, qt: UTC, xmp: ISO)
    dt_offset: Optional[str] = None   # "+02:00" (exif OffsetTimeOriginal)
    dt_subsec: Optional[str] = None   # exif SubSecTimeOriginal
    ctime_ns: int = 0


def sniff(head: bytes) -> str:
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"GIF8":
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
            return "heic"
        return "mp4"
    if head.lstrip()[:1] in (b"<",):
        return "html"
    return "unknown"


def jpeg_scan_offset(data: bytes) -> Optional[int]:
    """Offset of the first SOS marker (start of image data), or None."""
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0xDA:
            return i
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def _structure_problem(fmt: str, data: bytes) -> Optional[str]:
    size = len(data)
    if fmt == "jpeg":
        if not data.rstrip(b"\x00\r\n ").endswith(b"\xff\xd9"):
            return "JPEG truncated (no end marker)"
        if jpeg_scan_offset(data) is None:
            return "JPEG has no image data (no scan marker)"
    elif fmt == "png":
        if not data.endswith(b"IEND\xaeB`\x82"):
            return "PNG truncated (no IEND chunk)"
    elif fmt == "gif":
        if not data.endswith(b";"):
            return "GIF truncated (no trailer)"
    elif fmt == "webp":
        riff = struct.unpack("<I", data[4:8])[0] + 8
        if riff > size:
            return f"WEBP truncated (header says {riff} bytes, file has {size})"
    return None


def _dhash(im) -> int:
    g = im.convert("L").resize((9, 8), Image.BILINEAR)
    px = list(g.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def _check_image(res: FileResult, data: bytes, ext_fmt: Optional[str]):
    fmt = res.fmt
    prob = _structure_problem(fmt, data)
    if fmt == "jpeg":
        sos = jpeg_scan_offset(data)
        res.chash = hashlib.sha1(data[sos:] if sos else data).hexdigest()
    else:
        res.chash = hashlib.sha1(data).hexdigest()
    if not HAVE_PIL:
        res.note = "not decoded (Pillow missing)"
    elif fmt in ("avif", "heic") and not PIL_AVIF:
        res.note = f"{fmt.upper()} not decoded (unsupported here)"
    else:
        try:
            with Image.open(io.BytesIO(data)) as im:
                res.width, res.height = im.size
                if fmt == "jpeg":
                    im.draft("RGB", (max(1, im.size[0] // 8), max(1, im.size[1] // 8)))
                im.load()
                if fmt == "gif":
                    frames = getattr(im, "n_frames", 1)
                    if frames > 1:
                        im.seek(frames - 1)
                        im.load()
                        im.seek(0)
                res.dhash = _dhash(im)
                if fmt in ("jpeg", "png", "webp"):
                    _read_exif_date(res, im)
        except Exception as e:  # truncated/corrupt data, unsupported variant...
            msg = str(e).splitlines()[0][:120] if str(e) else e.__class__.__name__
            prob = prob or f"cannot be decoded ({msg})"
    if prob:
        res.ok, res.problem = False, prob


def _check_video(res: FileResult, fh, size: int):
    """Walk the top-level boxes; hash the media data ('mdat') only, so that
    metadata rewrites (dates) do not change the content hash."""
    h = hashlib.sha1()
    pos, first = 0, True
    while pos < size:
        fh.seek(pos)
        hdr = fh.read(8)
        if len(hdr) < 8:
            res.ok, res.problem = False, "video truncated (partial box header)"
            return
        length, kind = struct.unpack(">I4s", hdr)
        head_len = 8
        if length == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                res.ok, res.problem = False, "video truncated (partial box header)"
                return
            length, head_len = struct.unpack(">Q", ext)[0], 16
        elif length == 0:
            length = size - pos
        if length < head_len:
            res.ok, res.problem = False, "video corrupt (invalid box size)"
            return
        if first and kind != b"ftyp":
            res.ok, res.problem = False, "not an MP4/MOV (no ftyp box)"
            return
        first = False
        if pos + length > size:
            res.ok, res.problem = False, "video truncated (last box goes past end of file)"
            return
        if kind == b"mdat":
            fh.seek(pos + head_len)
            left = length - head_len
            while left > 0:
                chunk = fh.read(min(left, 1 << 20))
                if not chunk:
                    break
                h.update(chunk)
                left -= len(chunk)
        pos += length
    res.chash = h.hexdigest()
    res.dt_value = _mp4_create_date(fh, size)
    if res.dt_value:
        res.dt_kind = "qt"
    res.note = "structure checked (videos are not decoded)"


RE_XMP_DATE = re.compile(rb'photoshop:DateCreated(?:="([^"]+)"|>([^<]+)<)')


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("ascii", "replace")
    v = str(v).strip().strip("\x00").strip()
    return v or None


def _read_exif_date(res: FileResult, im):
    try:
        exif = im.getexif()
        ifd = exif.get_ifd(0x8769)            # Exif sub-IFD
        dto = _clean(ifd.get(0x9003))         # DateTimeOriginal
        if dto:
            res.dt_kind, res.dt_value = "exif", dto
            res.dt_offset = _clean(ifd.get(0x9011))   # OffsetTimeOriginal
            res.dt_subsec = _clean(ifd.get(0x9291))   # SubSecTimeOriginal
    except Exception:
        pass


def _mp4_create_date(fh, size) -> Optional[str]:
    """moov/mvhd creation_time (UTC, seconds since 1904) as 'YYYY:MM:DD HH:MM:SS'."""
    import datetime as _dt
    try:
        pos = 0
        while pos + 8 <= size:
            fh.seek(pos)
            length, kind = struct.unpack(">I4s", fh.read(8))
            hl = 8
            if length == 1:
                length, hl = struct.unpack(">Q", fh.read(8))[0], 16
            elif length == 0:
                length = size - pos
            if length < hl:
                return None
            if kind == b"moov":
                end, cpos = pos + length, pos + hl
                while cpos + 8 <= end:
                    fh.seek(cpos)
                    clen, ckind = struct.unpack(">I4s", fh.read(8))
                    if clen < 8:
                        return None
                    if ckind == b"mvhd":
                        ver = fh.read(4)[0]
                        raw = fh.read(8 if ver == 1 else 4)
                        secs = int.from_bytes(raw, "big")
                        if secs == 0:
                            return None
                        d = _dt.datetime(1904, 1, 1) + _dt.timedelta(seconds=secs)
                        return d.strftime("%Y:%m:%d %H:%M:%S")
                    cpos += clen
                return None
            pos += length
    except (OSError, struct.error, IndexError, OverflowError):
        return None
    return None


def check_file(path: str, st: os.stat_result) -> FileResult:
    res = FileResult(path, st.st_size, st.st_mtime_ns, st.st_ino)
    res.ctime_ns = st.st_ctime_ns
    ext = os.path.splitext(path)[1].lower()
    ext_fmt = EXT_FMT.get(ext)
    if st.st_size == 0:
        res.ok, res.problem = False, "empty file"
        return res
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
            res.fmt = sniff(head)
            if res.fmt in VIDEO_FMT:
                _check_video(res, fh, st.st_size)
            else:
                fh.seek(0)
                data = fh.read()
                if len(data) != st.st_size:
                    res.ok, res.problem = False, "file changed while reading"
                    return res
                if res.fmt in ("unknown", "html"):
                    res.ok = False
                    res.problem = ("an HTML page, not a photo" if res.fmt == "html"
                                   else "unknown content (not an image or video)")
                    res.chash = hashlib.sha1(data).hexdigest()
                else:
                    _check_image(res, data, ext_fmt)
                    if res.fmt == "gif":
                        m = RE_XMP_DATE.search(data)
                        if m:
                            res.dt_kind, res.dt_value = "xmp", (m.group(1) or m.group(2)).decode("ascii", "replace")
    except OSError as e:
        res.ok, res.problem = False, f"unreadable ({e.__class__.__name__})"
        return res
    if ext_fmt and res.fmt not in ("unknown", "html") and res.fmt != ext_fmt:
        mismatch = f"content is {res.fmt.upper()} but extension is {ext}"
        if res.ok:
            res.ok, res.problem = False, mismatch
        else:
            res.problem += f"; {mismatch}"
    return res


# =============================================================================
# Cache
# =============================================================================
class Cache:
    COLS = ("path", "size", "mtime_ns", "ino", "ctime_ns", "version", "fmt", "ok", "problem",
            "note", "chash", "dhash", "width", "height",
            "dt_kind", "dt_value", "dt_offset", "dt_subsec")

    def __init__(self, path: Optional[str]):
        self.path = path
        self.error = None
        try:
            if path:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self.con = sqlite3.connect(path or ":memory:", check_same_thread=False)
        except (sqlite3.Error, OSError) as e:
            self.error = f"cache not writable ({e.__class__.__name__}), using memory"
            self.con = sqlite3.connect(":memory:", check_same_thread=False)
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(files)")]
        if cols and tuple(cols) != self.COLS:       # older layout: start over
            self.con.execute("DROP TABLE files")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, size INT, "
            "mtime_ns INT, ino INT, ctime_ns INT, version INT, fmt TEXT, ok INT, problem TEXT, "
            "note TEXT, chash TEXT, dhash TEXT, width INT, height INT, "
            "dt_kind TEXT, dt_value TEXT, dt_offset TEXT, dt_subsec TEXT)")
        self.con.commit()
        self.pending = 0

    def get(self, path: str, st) -> Optional[FileResult]:
        # ctime is part of the key: an in-place rewrite that keeps size, mtime
        # (exiftool -P) and inode still changes the ctime
        row = self.con.execute(
            "SELECT * FROM files WHERE path=? AND size=? AND mtime_ns=? AND ino=? "
            "AND ctime_ns=? AND version=?",
            (path, st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns, CHECK_VERSION)).fetchone()
        if row is None:
            return None
        d = dict(zip(self.COLS, row))
        r = FileResult(d["path"], d["size"], d["mtime_ns"], d["ino"], d["fmt"], bool(d["ok"]),
                       d["problem"], d["note"], d["chash"],
                       int(d["dhash"], 16) if d["dhash"] else None, d["width"], d["height"],
                       d["dt_kind"], d["dt_value"], d["dt_offset"], d["dt_subsec"], d["ctime_ns"])
        return r

    def put(self, r: FileResult):
        self.con.execute(
            "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.path, r.size, r.mtime_ns, r.ino, r.ctime_ns, CHECK_VERSION, r.fmt, int(r.ok),
             r.problem, r.note, r.chash, f"{r.dhash:016x}" if r.dhash is not None else None,
             r.width, r.height, r.dt_kind, r.dt_value, r.dt_offset, r.dt_subsec))
        self.pending += 1
        if self.pending >= 200:
            self.commit()

    def commit(self):
        self.con.commit()
        self.pending = 0

    def forget_missing(self, present: set):
        gone = [p for (p,) in self.con.execute("SELECT path FROM files") if p not in present]
        self.con.executemany("DELETE FROM files WHERE path=?", ((p,) for p in gone))
        self.commit()


# =============================================================================
# JSON metadata
# =============================================================================
@dataclass
class Meta:
    id: Optional[str] = None
    set_id: Optional[str] = None
    num: Optional[int] = None
    next_id: Optional[str] = None
    first_id: Optional[str] = None
    date: Optional[str] = None
    title: Optional[str] = None
    username: Optional[str] = None
    problem: Optional[str] = None


def read_meta(path: str) -> Meta:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        return Meta(problem=f"JSON unreadable ({e.__class__.__name__})")
    if not isinstance(d, dict):
        return Meta(problem="JSON is not an object")
    num = d.get("num")
    m = Meta(id=str(d.get("id") or "") or None, set_id=str(d.get("set_id") or "") or None,
             num=num if isinstance(num, int) and not isinstance(num, bool) else None,
             next_id=str(d.get("next_photo_id") or "") or None,
             first_id=str(d.get("first_photo_id") or "") or None,
             date=d.get("date") if isinstance(d.get("date"), str) else None,
             title=d.get("title") or None, username=d.get("username") or None)
    if not m.id:
        m.problem = "JSON has no photo 'id'"
    elif not m.set_id:
        m.problem = "JSON has no 'set_id'"
    elif not m.date:
        m.problem = "JSON has no 'date'"
    return m


# =============================================================================
# Report
# =============================================================================
@dataclass
class FileEntry:
    path: str
    name: str
    folder: str              # path relative to root
    ext: str
    size: int
    ctime: float
    result: Optional[FileResult] = None
    meta: Optional[Meta] = None
    has_json: bool = False
    key: str = ""            # opaque id for thumbnails

    @property
    def photo_id(self) -> Optional[str]:
        if self.meta and self.meta.id:
            return self.meta.id
        m = ID_IN_NAME.search(self.name)
        return m.group(1) if m else None


@dataclass
class Issue:
    severity: str            # bad|warn|info
    kind: str
    text: str
    entries: list = field(default_factory=list)


@dataclass
class AlbumCheck:
    set_id: str
    folders: list = field(default_factory=list)
    title: Optional[str] = None
    username: Optional[str] = None
    media: int = 0
    bytes: int = 0
    checked: int = 0
    max_num: Optional[int] = None
    num_gaps: list = field(default_factory=list)          # [(start, end)]
    missing_ids: list = field(default_factory=list)       # known IDs from the chain
    missing_estimate: int = 0
    chain: str = "n/a"       # ok|broken|n/a|in progress
    issues: list = field(default_factory=list)
    bad_files: list = field(default_factory=list)          # FileEntry
    in_progress: bool = False
    first_missing: bool = False
    archived_missing: list = field(default_factory=list)
    not_archived: int = 0
    listed_at: Optional[float] = None     # when this album's files were listed
    # what the gallery-dl log says about this album
    log_passes: int = 0
    log_end: Optional[str] = None          # end reason of the last pass
    log_seen: int = 0                      # distinct photo IDs seen in all passes
    log_expected: Optional[int] = None     # photos in the last pass that reached the end
    log_not_on_disk: list = field(default_factory=list)   # [(photo_id, log status)]
    log_extra_on_disk: list = field(default_factory=list)  # on disk, not in last full pass

    @property
    def log_confirmed(self) -> bool:
        """The last pass in the log reached the end of the album."""
        return self.log_passes > 0 and self.log_end in END_OK

    @property
    def scanned(self) -> bool:
        return self.checked >= self.media

    @property
    def verdict(self) -> str:
        if not self.scanned:
            return "scanning"
        sev = {i.severity for i in self.issues}
        if "bad" in sev:
            return "problems"
        if "warn" in sev:
            return "warnings"
        if self.in_progress:
            return "in progress"
        return "verified"


@dataclass
class Report:
    started: float
    finished: Optional[float] = None
    files: int = 0
    checked: int = 0
    bytes: int = 0
    albums: dict = field(default_factory=dict)             # set_id -> AlbumCheck
    loose: list = field(default_factory=list)             # Issues not tied to an album
    id_dups: list = field(default_factory=list)            # [[FileEntry,...]] same id, same album
    cross_album_ids: list = field(default_factory=list)    # [[FileEntry,...]] same id, other albums
    archive_entries: Optional[int] = None
    archive_error: Optional[str] = None
    by_key: dict = field(default_factory=dict)             # thumbnail key -> path
    formats: dict = field(default_factory=dict)
    partial: bool = False                                  # first pass still running
    dates: object = None                                   # dates.DatesReport

    def counts(self) -> dict:
        c = defaultdict(int)
        for a in self.albums.values():
            c[a.verdict] += 1
        return dict(c)

    def problem_files(self) -> int:
        return sum(len(a.bad_files) for a in self.albums.values())


def _ranges(nums: list[int]) -> list[tuple]:
    out = []
    for n in sorted(nums):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def read_archive(path: Optional[str]):
    """-> (set of (set_id, photo_id), error)."""
    if not path or not os.path.exists(path):
        return None, None
    for uri in (f"file:{path}?mode=ro", f"file:{path}?mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(uri, uri=True, timeout=5)
            rows = con.execute("SELECT entry FROM archive").fetchall()
            con.close()
            break
        except sqlite3.Error as e:
            err = f"{e.__class__.__name__}: {e}"
    else:
        return None, f"cannot read archive ({err})"
    keys = set()
    for (entry,) in rows:
        if entry.startswith("facebook"):
            rest = entry[len("facebook"):]
            sid, sep, pid = rest.rpartition("_")
            if sep and pid.isdigit():
                keys.add((sid, pid))
    return keys, None


def build_report(entries: list[FileEntry], json_only: list[FileEntry], other: list[FileEntry],
                 archive: Optional[set], archive_error: Optional[str],
                 current_set: Optional[str], started: float, now: float,
                 log: Optional[dict] = None, present_now=None,
                 album_started: Optional[dict] = None) -> Report:
    """`log`: {set_id: {"passes", "last_end", "last_ids", "photos": {id: (status, ts)}}}
    as built by status.log_snapshot(). `started` is when the files were listed:
    photos the log saw after that cannot be on the list yet and are ignored."""
    rep = Report(started=started)
    rep.files = len(entries)
    rep.archive_entries = len(archive) if archive is not None else None
    rep.archive_error = archive_error

    def album_of(e: FileEntry) -> str:
        if e.meta and e.meta.set_id:
            return e.meta.set_id
        m = SID_IN_DIR.search(os.path.basename(e.folder))
        return m.group(1) if m else f"?{e.folder}"

    def album(sid) -> AlbumCheck:
        a = rep.albums.get(sid)
        if a is None:
            a = rep.albums[sid] = AlbumCheck(sid)
        return a

    groups = defaultdict(list)
    for e in entries:
        groups[album_of(e)].append(e)
        rep.by_key[e.key] = e.path
        rep.bytes += e.size
        if e.result:
            rep.checked += 1
            rep.formats[e.result.fmt] = rep.formats.get(e.result.fmt, 0) + 1

    for sid, es in groups.items():
        a = album(sid)
        a.in_progress = sid == current_set
        a.media = len(es)
        a.bytes = sum(e.size for e in es)
        a.checked = sum(1 for e in es if e.result)
        a.folders = sorted({e.folder for e in es})
        metas = [e.meta for e in es if e.meta and not e.meta.problem]
        a.title = next((m.title for m in metas if m.title), None)
        a.username = next((m.username for m in metas if m.username), None)
        if len(a.folders) > 1:
            a.issues.append(Issue("warn", "split", f"album spread over {len(a.folders)} folders"))

        # ---- files -------------------------------------------------------------
        for e in es:
            if e.result and not e.result.ok:
                a.bad_files.append(e)
        if a.bad_files:
            a.issues.append(Issue("bad", "bad_file",
                                  f"{len(a.bad_files)} damaged or wrong-format file(s)", a.bad_files))
        no_json = [e for e in es if not e.has_json and now - e.ctime > TRANSIENT]
        if no_json:
            a.issues.append(Issue("warn", "no_json", f"{len(no_json)} file(s) without JSON metadata", no_json))
        bad_meta = [e for e in es if e.meta and e.meta.problem]
        if bad_meta:
            a.issues.append(Issue("warn", "bad_json", f"{len(bad_meta)} JSON file(s) unreadable or incomplete", bad_meta))
        mismatch = []
        for e in es:
            if not e.meta or e.meta.problem:
                continue
            m = ID_IN_NAME.search(e.name)
            if m and m.group(1) != e.meta.id:
                mismatch.append(e)
            dsid = SID_IN_DIR.search(os.path.basename(e.folder))
            if dsid and dsid.group(1) != e.meta.set_id and e not in mismatch:
                mismatch.append(e)
        if mismatch:
            a.issues.append(Issue("warn", "meta_mismatch",
                                  f"{len(mismatch)} file(s) whose JSON names another photo or album", mismatch))

        # ---- duplicates inside the album -----------------------------------------
        by_id = defaultdict(list)
        for e in es:
            if e.photo_id:
                by_id[e.photo_id].append(e)
        dups = [v for v in by_id.values() if len(v) > 1]
        if dups:
            rep.id_dups.extend(dups)
            a.issues.append(Issue("warn", "id_dup", f"{len(dups)} photo(s) saved more than once",
                                  [x for v in dups for x in v]))

        # ---- completeness: num positions + next_photo_id chain ------------------
        by_meta = {m.id: m for m in metas}
        nums = [m.num for m in metas if m.num]
        if nums:
            a.max_num = max(nums)
            present = set(nums)
            gaps = [n for n in range(1, a.max_num + 1) if n not in present]
            a.num_gaps = _ranges(gaps)
        firsts = {m.first_id for m in metas if m.first_id}
        if any(m.next_id for m in metas):
            missing, heads_pointed = [], set()
            for m in metas:
                if m.next_id and m.next_id != m.id:
                    if m.next_id in by_meta:
                        heads_pointed.add(m.next_id)
                    else:
                        missing.append((m, m.next_id))
            # the newest photo of the album being downloaded points to the next
            # photo gallery-dl has not reached yet: not a gap
            if a.in_progress and missing and nums:
                last = max(missing, key=lambda x: x[0].num or 0)
                if (last[0].num or 0) == a.max_num:
                    missing.remove(last)
            a.missing_ids = sorted({nid for _, nid in missing})
            a.chain = "broken" if missing else ("in progress" if a.in_progress else "ok")
        for f in firsts:
            if f not in by_meta and not a.in_progress:
                a.first_missing = True
                if f not in a.missing_ids:
                    a.missing_ids.insert(0, f)
        if not nums and not any(m.next_id for m in metas) and metas:
            a.issues.append(Issue("info", "no_order", "no position data in JSON: completeness cannot be proven"))

        # ---- archive ------------------------------------------------------------
        if archive is not None and not sid.startswith("?"):
            have = set(by_id)
            a.not_archived = sum(1 for pid in have if (sid, pid) not in archive)

    # archived photos whose file is gone (would never be downloaded again)
    if archive is not None:
        on_disk = defaultdict(set)
        for sid, es in groups.items():
            on_disk[sid] = {e.photo_id for e in es if e.photo_id}
        lost = defaultdict(list)
        for sid, pid in archive:
            if pid not in on_disk.get(sid, ()):
                lost[sid].append(pid)
        # The archive is read now, the file listing is older (start of the pass):
        # photos downloaded since are in the archive but not in the listing.
        # Look at the disk again before calling anything missing.
        if lost and present_now is not None:
            for sid in list(lost):
                here = present_now(sid)
                lost[sid] = [pid for pid in lost[sid] if pid not in here]
                if not lost[sid]:
                    del lost[sid]
        for sid, pids in lost.items():
            a = album(sid)
            a.archived_missing = sorted(pids)
            a.issues.append(Issue("bad", "archived_missing",
                                  f"{len(pids)} photo(s) in the archive but not on disk: "
                                  f"gallery-dl will NOT download them again"))

    for sid, a in rep.albums.items():
        a.listed_at = (album_started or {}).get(sid, started)

    # ---- log vs disk -----------------------------------------------------------
    disk_ids = {sid: {e.photo_id for e in es if e.photo_id} for sid, es in groups.items()}
    for sid, L in (log or {}).items():
        if not sid:
            continue
        a = album(sid)
        a.log_passes = L["passes"]
        a.log_end = L["last_end"]
        a.log_seen = len(L["photos"])
        have = disk_ids.get(sid, set())
        in_prog = sid == current_set
        if in_prog:
            a.in_progress = True
        cutoff = (album_started or {}).get(sid, started)
        for pid, (st, ts) in L["photos"].items():
            if pid in have or ts > cutoff - 60:
                continue
            if st == "pending" and in_prog:
                continue
            a.log_not_on_disk.append((pid, st))
        by_st = defaultdict(list)
        for pid, st in a.log_not_on_disk:
            by_st[st].append(pid)
        if by_st["downloaded"]:
            a.issues.append(Issue("bad", "log_downloaded_missing",
                                  f"{len(by_st['downloaded'])} photo(s) downloaded per the log but not on disk"))
        if by_st["skipped"]:
            a.issues.append(Issue("bad", "log_skipped_missing",
                                  f"{len(by_st['skipped'])} photo(s) gallery-dl skipped as already "
                                  f"downloaded (file or archive), but not on disk"))
        failed = by_st["missing"] + by_st["failed"] + by_st["unknown"]
        if failed:
            a.issues.append(Issue("bad", "log_failed",
                                  f"{len(failed)} photo(s) gallery-dl could not download (see Events)"))
        if by_st["pending"]:
            a.issues.append(Issue("warn", "log_pending",
                                  f"{len(by_st['pending'])} photo(s) seen by gallery-dl but the pass "
                                  f"stopped before downloading them"))
        if in_prog or a.log_end == "running":
            continue
        if a.log_end in END_OK:
            a.log_expected = len(L["last_ids"])
            extra = have - set(L["last_ids"])
            a.log_extra_on_disk = sorted(extra)
            if extra:
                a.issues.append(Issue("info", "log_extra",
                                      f"{len(extra)} photo(s) on disk were not seen in the last full "
                                      f"pass (removed from Facebook since, or moved to another album)"))
        elif a.log_end == "jump":
            a.issues.append(Issue("warn", "log_jump",
                                  "last pass stopped by gallery-dl's 'jump' heuristic: "
                                  "the end of the album is not confirmed"))
        else:
            a.issues.append(Issue("warn", "log_incomplete",
                                  f"last pass did not reach the end of the album "
                                  f"({END_REASONS.get(a.log_end, a.log_end)})"))

    # ---- missing photos: JSON positions/chain + log --------------------------------
    for a in rep.albums.values():
        known = list(dict.fromkeys(a.missing_ids + [pid for pid, _ in a.log_not_on_disk]))
        a.missing_ids = known
        gap_count = sum(b - s + 1 for s, b in a.num_gaps)
        a.missing_estimate = max(gap_count, len(known))
        if a.missing_estimate:
            txt = f"{a.missing_estimate} photo(s) missing from this album"
            if a.num_gaps:
                txt += " (positions " + ", ".join(
                    f"{s}" if s == b else f"{s}–{b}" for s, b in a.num_gaps[:8]) + \
                    ("…" if len(a.num_gaps) > 8 else "") + ")"
            if known:
                txt += f"; {len(known)} ID(s) known"
            a.issues.append(Issue("bad", "missing", txt))

    # ---- orphans --------------------------------------------------------------
    for e in json_only:
        if now - e.ctime > TRANSIENT:
            sid = album_of(e)
            album(sid).issues.append(Issue("warn", "json_only", f"JSON without its file: {e.name}", [e]))
    part = [e for e in other if e.ext == ".part"]
    junk = [e for e in other if e.ext != ".part"]
    for e in part:
        sid = album_of(e)
        if sid != current_set:
            album(sid).issues.append(Issue("warn", "part", f"unfinished download: {e.name}", [e]))
    if junk:
        rep.loose.append(Issue("info", "other_files", f"{len(junk)} other file(s) (not media/JSON)", junk))

    # ---- the same photo ID in several albums (information only) ----------------
    by_pid = defaultdict(list)
    for e in entries:
        if e.photo_id:
            by_pid[e.photo_id].append(e)
    for v in by_pid.values():
        if len({album_of(x) for x in v}) > 1:
            rep.cross_album_ids.append(v)
    rep.finished = now
    return rep


# =============================================================================
# Background scanner
# =============================================================================
class IntegrityScanner:
    def __init__(self, root: str, cache_path: Optional[str], archive_path: Optional[str],
                 max_mbps: float = 40.0, rescan_every: float = 900.0,
                 current_set=lambda: None, clock=time.time, log_snapshot=None,
                 rebuild_every: float = 60.0, fix_tz=None, fix_tz_name: str = "UTC",
                 fix_step: float = 1.0):
        self.root = root
        self.cache = Cache(cache_path)
        self.archive_path = archive_path
        self.max_bps = max(1.0, max_mbps) * 1e6
        self.rescan_every = rescan_every
        self.current_set = current_set
        self.clock = clock
        self.log_snapshot = log_snapshot
        self.rebuild_every = rebuild_every
        self.fix_tz = fix_tz
        self.fix_tz_name = fix_tz_name
        self.fix_step = fix_step
        self._last_lists = None
        self._album_started: dict = {}   # set_id -> time of its last re-check
        self.scan_lock = threading.Lock()
        self.report: Optional[Report] = None
        self.progress = {"phase": "waiting", "done": 0, "total": 0, "started": None, "passes": 0}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.poke = threading.Event()
        self.last_error = None

    def run(self):
        while not self.stop.is_set():
            try:
                self.scan_once()
                self.last_error = None
            except Exception as e:  # keep the dashboard alive
                self.last_error = f"{e.__class__.__name__}: {e}"
            self.progress["phase"] = "idle"
            # between passes, refresh the report every minute from the latest log
            # (no file is re-read)
            deadline = time.time() + self.rescan_every
            while not self.stop.is_set() and time.time() < deadline:
                if self.poke.wait(min(self.rebuild_every, max(0.0, deadline - time.time()))):
                    break
                try:
                    self.rebuild()
                except Exception as e:
                    self.last_error = f"{e.__class__.__name__}: {e}"
            self.poke.clear()

    def rebuild(self):
        """Re-compare the last file listing with the current log."""
        if self._last_lists is not None:
            self._publish(*self._last_lists, partial=False)

    def album_folders(self, sid: str) -> list:
        """Folders of an album right now: '... (sid)' under root/<account>/,
        plus folders where the last listing found its files."""
        found = set()
        suffix = f"({sid})"
        try:
            for acc in os.scandir(self.root):
                if acc.is_dir(follow_symlinks=False):
                    for alb in os.scandir(acc.path):
                        if alb.is_dir(follow_symlinks=False) and alb.name.rstrip().endswith(suffix):
                            found.add(alb.path)
        except OSError:
            pass
        if self._last_lists:
            for e in self._last_lists[0]:
                if e.meta and e.meta.set_id == sid:
                    found.add(os.path.dirname(e.path))
        return sorted(found)

    def present_now(self, sid: str) -> set:
        """Photo IDs of an album's media files currently on disk (names only)."""
        ids = set()
        for folder in self.album_folders(sid):
            try:
                for f in os.scandir(folder):
                    if os.path.splitext(f.name)[1].lower() in MEDIA_EXT:
                        m = ID_IN_NAME.search(f.name)
                        if m:
                            ids.add(m.group(1))
            except OSError:
                pass
        return ids

    def _walk(self, folders=None):
        """List files. folders=None: the whole tree; else only these folders
        (not recursive), used to re-check one album."""
        media, json_only, other = [], [], []
        if folders is None:
            walker = ((dp, sorted(ns)) for dp, ds, ns in os.walk(self.root) if not ds.sort())
        else:
            def walker_gen():
                for dp in folders:
                    try:
                        yield dp, sorted(os.listdir(dp))
                    except OSError:
                        continue
            walker = walker_gen()
        for dirpath, names in walker:
            present = set(names)
            folder = os.path.relpath(dirpath, self.root)
            for name in names:
                if name in (".albumprops", ".albumprops.tmp"):
                    continue          # immich-folder-album-creator settings, not media
                path = os.path.join(dirpath, name)
                base, ext = os.path.splitext(name)
                ext = ext.lower()
                try:
                    st = os.stat(path, follow_symlinks=False)
                except OSError:
                    continue
                if not os.path.isfile(path):
                    continue
                e = FileEntry(path, name, folder, ext, st.st_size, st.st_ctime,
                              key=hashlib.sha1(os.fsencode(path)).hexdigest()[:20])
                e._st = st
                if ext == ".json":
                    if base not in present:
                        json_only.append(e)
                elif ext in MEDIA_EXT:
                    e.has_json = name + ".json" in present
                    media.append(e)
                else:
                    other.append(e)
        return media, json_only, other

    def _read_metas(self, media, meta_cache, new_meta_cache):
        for e in media:
            if e.has_json:
                jp = e.path + ".json"
                try:
                    jst = os.stat(jp)
                    k = (jp, jst.st_mtime_ns, jst.st_size)
                    e.meta = meta_cache.get(k) or read_meta(jp)
                    new_meta_cache[k] = e.meta
                except OSError:
                    e.has_json = False

    def rescan_album(self, sid: str) -> bool:
        """Re-list one album's folders now, check new/changed files, rebuild
        the report. Waits for a running full pass to finish."""
        with self.scan_lock:
            if self._last_lists is None:
                return False
            t = time.time()
            folders = set(self.album_folders(sid))
            media, json_only, other, started = self._last_lists
            nm, nj, no = self._walk(sorted(folders))
            cache = getattr(self, "_meta_cache", {})
            self._read_metas(nm, cache, cache)
            for e in nm:
                r = self.cache.get(e.path, e._st)
                if r is None:
                    r = check_file(e.path, e._st)
                    self.cache.put(r)
                e.result = r
            for e in nj:
                e.meta = read_meta(e.path)
            self.cache.commit()
            keep = lambda e: os.path.dirname(e.path) not in folders  # noqa: E731
            self._last_lists = ([e for e in media if keep(e)] + nm,
                                [e for e in json_only if keep(e)] + nj,
                                [e for e in other if keep(e)] + no, started)
            self._album_started[sid] = t
            self._publish(*self._last_lists, partial=False)
            return True

    def scan_once(self):
        with self.scan_lock:
            self._scan_once()

    def _scan_once(self):
        started = time.time()
        p = self.progress
        p.update(phase="listing files", done=0, total=0, started=started)
        media, json_only, other = self._walk()
        # 1. metadata first (cheap): album grouping and completeness need it
        p.update(phase="reading metadata", total=len(media))
        meta_cache = getattr(self, "_meta_cache", {})
        new_meta_cache = {}
        self._read_metas(media, meta_cache, new_meta_cache)
        # 2. file contents (cached; throttled to spare the disk)
        p.update(phase="checking files")
        t0, budget = time.monotonic(), 0.0
        for i, e in enumerate(media):
            if self.stop.is_set():
                return
            st = e._st
            r = self.cache.get(e.path, st)
            if r is None:
                r = check_file(e.path, st)
                self.cache.put(r)
                budget += st.st_size
                ahead = budget / self.max_bps - (time.monotonic() - t0)
                if ahead > 0:
                    time.sleep(min(ahead, 5))
            e.result = r
            p["done"] = i + 1
            if self.report is None or (time.time() - (self._published or 0) > 30):
                self._publish(media, json_only, other, started, partial=True)
        for e in json_only:
            e.meta = read_meta(e.path)
        self.cache.commit()
        self.cache.forget_missing({e.path for e in media})
        self._meta_cache = new_meta_cache
        p["phase"] = "comparing"
        self._last_lists = (media, json_only, other, started)
        self._album_started = {}          # the full listing is newer than any album re-check
        self._publish(media, json_only, other, started, partial=False)
        p["passes"] += 1

    _published: Optional[float] = None

    def _publish(self, media, json_only, other, started, partial):
        """Build a report; during the first pass, unchecked files count as 'scanning'."""
        if partial and self.report is not None and self.report.finished and not self.report.partial:
            return   # keep showing the last complete pass while re-checking
        archive, aerr = read_archive(self.archive_path)
        log = self.log_snapshot() if self.log_snapshot else None
        rep = build_report(media, json_only, other, archive, aerr,
                           self.current_set(), started, self.clock(), log,
                           present_now=self.present_now, album_started=dict(self._album_started))
        rep.partial = partial
        from .dates import build_dates
        from datetime import timezone as _tz
        rep.dates = build_dates(media, self.fix_tz or _tz.utc, self.fix_tz_name, self.fix_step,
                                rep.albums, self.current_set())
        with self.lock:
            self.report = rep
        self._published = time.time()
