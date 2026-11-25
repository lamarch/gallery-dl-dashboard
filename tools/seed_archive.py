#!/usr/bin/env python3
"""Seed a gallery-dl download archive from files already on disk.

Why: Facebook serves many photos from '.avif' URLs that are really JPEGs.
gallery-dl checks "does the file exist?" with the URL extension ('.avif'),
never finds the '.jpg', and downloads the photo again on every rerun.
A download archive keyed on album + photo ID avoids that, and keeps working
after folders are renamed.

Matching gallery-dl config (extractor.facebook):
    "archive": "/downloads/logs/fb-archive.sqlite3",
    "archive-format": "{set_id}_{id}"
gallery-dl then builds keys as  "facebook" + "<set_id>_<id>"  (archive.py),
which is exactly what this script writes.

Safety rules -- when in doubt, a file is NOT archived (it will simply be
downloaded again, which is today's behaviour):
  * the image must exist next to a valid JSON sidecar ("<image>.json")
  * the JSON must say category=facebook and contain a numeric 'id' and a
    non-empty 'set_id'
  * the photo ID must appear in the filename, and the album folder must end
    with "(<set_id>)" (gallery-dl's default folder layout)
  * the file must be non-empty and structurally complete for its type
    (JPEG ends with an EOI marker, PNG with IEND, MP4 boxes add up, ...)

Default is a DRY RUN. Nothing is written without --write.
With --write, an existing archive is first copied to "<archive>.bak-<time>".
Stop the gallery-dl container before using --write.

Standard library only. Run it inside the gallery-dl container (same uid,
same paths) or with any Python >= 3.8.
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import time
from collections import Counter

PREFIX = "facebook"            # gallery-dl 'archive-prefix' default = category
TABLE = "archive"              # gallery-dl default table name
SID_IN_DIR = re.compile(r"\(([^()]+)\)$")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".m4a"}


def archive_key(set_id, photo_id):
    return f"{PREFIX}{set_id}_{photo_id}"


# --------------------------------------------------------------------------
# File completeness checks (read only the head and the tail of each file)
# --------------------------------------------------------------------------
def _tail(fh, size, n=64):
    fh.seek(max(0, size - n))
    return fh.read()


def check_jpeg(fh, size):
    if fh.read(3) != b"\xff\xd8\xff":
        return "not a JPEG (bad header)"
    tail = _tail(fh, size).rstrip(b"\x00\r\n ")
    if not tail.endswith(b"\xff\xd9"):
        return "JPEG truncated (no end marker)"
    return None


def check_png(fh, size):
    if fh.read(8) != b"\x89PNG\r\n\x1a\n":
        return "not a PNG (bad header)"
    if not _tail(fh, size, 12).endswith(b"IEND\xaeB`\x82"):
        return "PNG truncated (no IEND chunk)"
    return None


def check_gif(fh, size):
    if fh.read(4) != b"GIF8":
        return "not a GIF (bad header)"
    if not _tail(fh, size, 1).endswith(b";"):
        return "GIF truncated (no trailer)"
    return None


def check_webp(fh, size):
    head = fh.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return "not a WEBP (bad header)"
    riff = struct.unpack("<I", head[4:8])[0] + 8
    if riff > size:
        return f"WEBP truncated (header says {riff} bytes, file has {size})"
    if size - riff > 1:  # one padding byte is allowed
        return f"WEBP has unexpected trailing data ({size - riff} bytes)"
    return None


def check_isobmff(fh, size):
    """MP4/MOV/M4A: top-level boxes must exactly cover the file."""
    pos, seen_ftyp, boxes = 0, False, 0
    while pos < size:
        fh.seek(pos)
        hdr = fh.read(8)
        if len(hdr) < 8:
            return "video truncated (partial box header)"
        length, kind = struct.unpack(">I4s", hdr)
        if length == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                return "video truncated (partial box header)"
            length = struct.unpack(">Q", ext)[0]
        elif length == 0:
            length = size - pos
        if length < 8:
            return "video corrupt (invalid box size)"
        if boxes == 0:
            if kind != b"ftyp":
                return "not an MP4/MOV (no ftyp box)"
            seen_ftyp = True
        pos += length
        boxes += 1
    if pos != size:
        return "video truncated (last box goes past end of file)"
    if not seen_ftyp:
        return "not an MP4/MOV"
    return None


CHECKS = {".jpg": check_jpeg, ".jpeg": check_jpeg, ".png": check_png,
          ".gif": check_gif, ".webp": check_webp}
for _e in VIDEO_EXT:
    CHECKS[_e] = check_isobmff


def check_media(path):
    ext = os.path.splitext(path)[1].lower()
    check = CHECKS.get(ext)
    if check is None:
        return f"unsupported extension '{ext}'"
    try:
        size = os.path.getsize(path)
        if size == 0:
            return "empty file"
        with open(path, "rb") as fh:
            return check(fh, size)
    except OSError as e:
        return f"unreadable ({e.__class__.__name__})"


# --------------------------------------------------------------------------
def scan(root):
    """Yield (status, key_or_None, path, reason)."""
    for dirpath, _dirs, names in os.walk(root):
        present = set(names)
        folder = os.path.basename(dirpath)
        m = SID_IN_DIR.search(folder)
        dir_sid = m.group(1) if m else None
        for name in sorted(names):
            base, ext = os.path.splitext(name)
            path = os.path.join(dirpath, name)
            if ext.lower() == ".json":
                if base not in present:
                    yield "skip", None, path, "JSON without media file"
                continue
            if ext.lower() == ".part":
                yield "skip", None, path, "unfinished download (.part)"
                continue
            if ext.lower() not in IMAGE_EXT | VIDEO_EXT:
                yield "skip", None, path, f"not a media file ('{ext}')"
                continue
            if name + ".json" not in present:
                yield "skip", None, path, "no JSON sidecar"
                continue
            try:
                with open(path + ".json", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as e:
                yield "skip", None, path, f"JSON unreadable ({e.__class__.__name__})"
                continue
            if not isinstance(data, dict):
                yield "skip", None, path, "JSON is not an object"
                continue
            if data.get("category") != "facebook":
                yield "skip", None, path, "JSON category is not 'facebook'"
                continue
            pid = str(data.get("id") or "")
            sid = str(data.get("set_id") or "")
            if not pid.isdigit():
                yield "skip", None, path, "JSON 'id' missing or not numeric"
                continue
            if not sid:
                yield "skip", None, path, "JSON 'set_id' missing"
                continue
            if not re.search(rf"(?<!\d){re.escape(pid)}(?!\d)", base):
                yield "skip", None, path, "photo ID not in filename"
                continue
            if dir_sid != sid:
                yield "skip", None, path, ("folder does not end with "
                                           "'(<set_id>)' of the JSON")
                continue
            problem = check_media(path)
            if problem:
                yield "skip", None, path, problem
                continue
            yield "ok", archive_key(sid, pid), path, ""


def open_archive(path):
    con = sqlite3.connect(path, timeout=5)
    con.isolation_level = None
    cur = con.cursor()
    try:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} "
                    f"(entry TEXT PRIMARY KEY) WITHOUT ROWID")
    except sqlite3.OperationalError:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} "
                    f"(entry TEXT PRIMARY KEY)")
    return con


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Seed a gallery-dl archive from existing Facebook "
                    "downloads (dry run unless --write).")
    ap.add_argument("--root", required=True,
                    help="gallery-dl facebook folder, e.g. /downloads/facebook")
    ap.add_argument("--archive", required=True,
                    help="archive file, e.g. /downloads/logs/fb-archive.sqlite3")
    ap.add_argument("--write", action="store_true",
                    help="actually write the archive (default: dry run)")
    ap.add_argument("--report", default=None,
                    help="write a TSV of every skipped file and why")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    if not os.path.isdir(args.root):
        sys.exit(f"not a folder: {args.root}")

    t0 = time.monotonic()
    keys, dup_keys, reasons, skipped = set(), 0, Counter(), []
    for status, key, path, reason in scan(args.root):
        if status == "ok":
            if key in keys:
                dup_keys += 1
            keys.add(key)
        else:
            reasons[reason] += 1
            skipped.append((path, reason))

    print(f"Scanned in {time.monotonic() - t0:.1f}s")
    print(f"  {len(keys):7d}  photos/videos eligible for the archive")
    if dup_keys:
        print(f"  {dup_keys:7d}  extra copies of an already counted photo "
              f"(same album + photo ID in two places)")
    print(f"  {sum(reasons.values()):7d}  files NOT archived "
          f"(will be downloaded again if gallery-dl meets them):")
    for reason, n in reasons.most_common():
        print(f"           {n:7d}  {reason}")

    if args.report:
        with open(args.report, "w", encoding="utf-8",
                  errors="backslashreplace") as fh:
            fh.write("path\treason\n")
            for path, reason in skipped:
                fh.write(f"{path}\t{reason}\n")
        print(f"Skipped-file report: {args.report}")

    existing = 0
    if os.path.exists(args.archive):
        con = sqlite3.connect(f"file:{args.archive}?mode=ro", uri=True)
        try:
            existing = con.execute(
                f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
            already = sum(1 for (k,) in con.execute(
                f"SELECT entry FROM {TABLE}") if k in keys)
        except sqlite3.OperationalError:
            existing = already = 0
        con.close()
        print(f"Archive already has {existing} entries, "
              f"{already} of them match files found now.")
    else:
        already = 0
        print("Archive does not exist yet; it will be created.")

    if not args.write:
        print(f"DRY RUN: would add {len(keys) - already} entries. "
              f"Re-run with --write (gallery-dl stopped) to apply.")
        return 0

    if os.path.exists(args.archive):
        backup = f"{args.archive}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(args.archive, backup)
        print(f"Backup: {backup}")
    con = open_archive(args.archive)
    try:
        con.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        sys.exit("archive is locked: is gallery-dl still running? Stop it first.")
    before = con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    con.executemany(f"INSERT OR IGNORE INTO {TABLE} (entry) VALUES (?)",
                    ((k,) for k in sorted(keys)))
    con.execute("COMMIT")
    after = con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    con.close()
    print(f"Written: {after - before} new entries ({after} total).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
