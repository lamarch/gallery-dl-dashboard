"""Tests for tools/seed_archive.py, checked against gallery-dl's own archive code.

Run:  GALLERY_DL_SRC=/path/to/gallery-dl python3 -m pytest tools/tests/
"""
import json
import os
import struct
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import seed_archive as sa  # noqa: E402

GDL = os.environ.get("GALLERY_DL_SRC")
if GDL:
    sys.path.insert(0, GDL)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9"
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 30 +
       b"\x00\x00\x00\x00IEND\xaeB`\x82")


def mp4(truncate=0):
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom\x00\x00\x02\x00"
    mdat = struct.pack(">I4s", 108, b"mdat") + b"\x00" * 100
    data = ftyp + mdat
    return data[:len(data) - truncate] if truncate else data


def webp(extra=0, cut=0):
    payload = b"VP8 " + b"\x00" * 20
    data = b"RIFF" + struct.pack("<I", len(payload) + 4) + b"WEBP" + payload
    data += b"\x00" * extra
    return data[:len(data) - cut] if cut else data


def put(album, fname, content, meta=None, sid=None, pid=None):
    os.makedirs(album, exist_ok=True)
    path = os.path.join(album, fname)
    with open(path, "wb") as fh:
        fh.write(content)
    if meta is not False:
        data = {"id": pid, "set_id": sid, "category": "facebook",
                "subcategory": "set", "title": "x", "num": 1}
        if isinstance(meta, dict):
            data.update(meta)
        with open(path + ".json", "w", encoding="utf-8") as fh:
            if meta == "broken":
                fh.write("{not json")
            else:
                json.dump(data, fh, ensure_ascii=False)
    return path


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "facebook"
    good = []
    # Non-latin account and album names, emoji, RTL
    a1 = str(root / "Алексей Иванов" / "Été 🌞 2024 (a.111)")
    a2 = str(root / "محمد" / "ألبوم العائلة (a.222)")
    a3 = str(root / "Someone" / " (a.333)")              # empty title
    good.append(("a.111", "1001", put(a1, "20240101_000000_1001.jpg", JPEG, sid="a.111", pid="1001")))
    good.append(("a.111", "1002", put(a1, "20240101_000001_1002.png", PNG, sid="a.111", pid="1002")))
    good.append(("a.222", "2001", put(a2, "20240101_000000_2001.mp4", mp4(), sid="a.222", pid="2001")))
    good.append(("a.222", "2002", put(a2, "20240101_000000_2002.webp", webp(), sid="a.222", pid="2002")))
    good.append(("a.333", "3001", put(a3, "20240101_000000_3001.jpg", JPEG + b"\x00\x00", sid="a.333", pid="3001")))
    bad = {
        "trunc_jpg": put(a1, "20240101_000002_1003.jpg", JPEG[:-30], sid="a.111", pid="1003"),
        "empty": put(a1, "20240101_000003_1004.jpg", b"", sid="a.111", pid="1004"),
        "no_json": put(a1, "20240101_000004_1005.jpg", JPEG, meta=False),
        "broken_json": put(a1, "20240101_000005_1006.jpg", JPEG, meta="broken", sid="a.111", pid="1006"),
        "wrong_set": put(a1, "20240101_000006_1007.jpg", JPEG, sid="a.999", pid="1007"),
        "id_not_in_name": put(a1, "20240101_000007_1008.jpg", JPEG, sid="a.111", pid="9999"),
        "trunc_mp4": put(a2, "20240101_000008_2003.mp4", mp4(truncate=20), sid="a.222", pid="2003"),
        "trunc_webp": put(a2, "20240101_000009_2004.webp", webp(cut=5), sid="a.222", pid="2004"),
        "fake_jpg": put(a2, "20240101_000010_2005.jpg", PNG, sid="a.222", pid="2005"),
        "part": put(a2, "20240101_000011_2006.jpg.part", JPEG[:50], meta=False),
        "other_cat": put(a2, "20240101_000012_2007.jpg", JPEG, meta={"category": "instagram"}, sid="a.222", pid="2007"),
    }
    # an undecodable (non UTF-8) file name must not crash the scan
    weird = os.path.join(os.fsencode(a1), b"bad\xffname.jpg")
    with open(weird, "wb") as fh:
        fh.write(JPEG)
    return str(root), good, bad


def test_scan_accepts_only_safe_files(tree):
    root, good, bad = tree
    results = list(sa.scan(root))
    ok = {p for s, _, p, _ in results if s == "ok"}
    assert ok == {p for _, _, p in good}
    for name, path in bad.items():
        assert path not in ok, name


def test_written_keys_are_what_gallery_dl_checks(tree, tmp_path):
    pytest.importorskip("gallery_dl")
    from gallery_dl import archive

    root, good, bad = tree
    arch = str(tmp_path / "logs" / "fb-archive.sqlite3")
    os.makedirs(os.path.dirname(arch))
    assert sa.main(["--root", root, "--archive", arch]) == 0
    assert not os.path.exists(arch), "dry run must not create the archive"
    assert sa.main(["--root", root, "--archive", arch, "--write"]) == 0

    # Same call gallery-dl makes (job.py) with
    #   archive-prefix default = category, archive-format = "{set_id}_{id}"
    db = archive.connect(arch, "facebook", "{set_id}_{id}")
    for sid, pid, _ in good:
        assert db.check({"set_id": sid, "id": pid, "extension": "avif"})
    for sid, pid in [("a.111", "1003"), ("a.111", "1004"),
                     ("a.222", "2003"), ("a.999", "1007")]:
        assert not db.check({"set_id": sid, "id": pid})
    # same photo in another album is NOT skipped (album-scoped key)
    assert not db.check({"set_id": "a.222", "id": "1001"})
    db.close()

    # re-running is idempotent and makes a backup
    assert sa.main(["--root", root, "--archive", arch, "--write"]) == 0
    assert any(f.startswith("fb-archive.sqlite3.bak-")
               for f in os.listdir(os.path.dirname(arch)))
