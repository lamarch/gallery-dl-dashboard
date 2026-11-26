"""fix_order.py with real exiftool (skipped when exiftool is missing).

Regressions fixed:
  * exiftool -P (EXIF write) restored the mtime with 1 s precision after the
    script had set it: with --step < 1 only 1 photo in 10 kept an exact mtime;
  * SubSecTimeOriginal left by an earlier run was never cleared when a target
    moved to a whole second: verification failed and the file was rewritten at
    every run.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "fix_order.py")
pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


def album(root, sid, n, only=None, skip=()):
    from PIL import Image
    d = os.path.join(root, "Acc", f"Album ({sid})")
    os.makedirs(d, exist_ok=True)
    ids = [f"{sid[2:]}{i:03d}" for i in range(n)]
    for i, pid in enumerate(ids):
        if pid in skip or (only is not None and pid not in only):
            continue
        b = io.BytesIO()
        Image.new("RGB", (16, 16), (i * 7 % 255, 40, 80)).save(b, "JPEG")
        p = os.path.join(d, f"20241208_195904_{pid}.jpg")
        open(p, "wb").write(b.getvalue())
        json.dump({"id": pid, "set_id": sid, "num": i + 1,
                   "next_photo_id": ids[i + 1] if i + 1 < n else "", "first_photo_id": ids[0],
                   "date": "2024-12-08 19:59:04", "category": "facebook"}, open(p + ".json", "w"))
        os.utime(p, (1700000000, 1700000000))
    return d, ids


def fix(root, *extra):
    r = subprocess.run([sys.executable, SCRIPT, "--tz", "Europe/Berlin", "--step", "0.1", *extra, root],
                       capture_output=True, text=True)
    line = next(l for l in r.stdout.splitlines() if "photos," in l)
    m = re.search(r"(\d+) mtime mis à jour.*EXIF (?:écrit sur|à écrire sur) (\d+)", line)
    return int(m.group(1)), int(m.group(2)), r.returncode, line


def test_one_run_gives_exact_mtimes(tmp_path):
    root = str(tmp_path / "lib")
    album(root, "a.61", 25)
    assert fix(root)[:3] == (25, 25, 0)
    assert fix(root, "--dry-run")[:2] == (0, 0)          # nothing left to do


def test_stale_subseconds_are_cleared(tmp_path):
    root = str(tmp_path / "lib")
    d, ids = album(root, "a.63", 12, skip={"63001"})       # one photo missing at first
    fix(root)
    album(root, "a.63", 12, only={"63001"})                # recovered later: ranks shift
    mtime, exif, code, line = fix(root)
    assert code == 0 and "vérification OK" in line, line
    assert fix(root, "--dry-run")[:2] == (0, 0)
    out = subprocess.run(["exiftool", "-s3", "-f", "-SubSecTimeOriginal",
                          os.path.join(d, f"20241208_195904_{ids[10]}.jpg")],
                         capture_output=True, text=True).stdout.strip()
    assert out == "-"                                      # rank 10 = whole second


def test_dashboard_agrees(tmp_path):
    sys.path.insert(0, os.path.join(HERE, "..", ".."))
    from zoneinfo import ZoneInfo
    from app.dates import build_dates
    from app.integrity import IntegrityScanner
    root = str(tmp_path / "lib")
    album(root, "a.64", 25)
    fix(root)
    s = IntegrityScanner(root, None, None, max_mbps=1000)
    s.scan_once()
    a = build_dates(s._last_lists[0], ZoneInfo("Europe/Berlin"), "Europe/Berlin", 0.1,
                    s.report.albums, None).albums["a.64"]
    assert a.state == "applied" and a.rounded == 0 and a.danger == 0
