import io
import json
import os
import random
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from PIL import Image  # noqa: E402

from app import integrity as ig  # noqa: E402


def jpeg_bytes(seed, size=(96, 72), quality=85, brighten=0):
    rnd = random.Random(seed)
    im = Image.new("RGB", size)
    # blocky random pattern: distinct per seed, stable under small edits
    for x in range(0, size[0], 8):
        for y in range(0, size[1], 8):
            c = tuple(min(255, rnd.randrange(256) + brighten) for _ in range(3))
            im.paste(c, (x, y, x + 8, y + 8))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def with_app_segment(data, payload=b"rewritten by exiftool"):
    """Insert an APP1-like segment after SOI: same pixels, different file."""
    seg = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    return data[:2] + seg + data[2:]


class Lib:
    def __init__(self, root):
        self.root = root
        self.archive = []

    def photo(self, account, title, sid, pid, num, next_id="", first_id=None, data=None,
              seed=None, ext=".jpg", date="20250101_000000", json_ok=True):
        d = os.path.join(self.root, account, f"{title} ({sid})")
        os.makedirs(d, exist_ok=True)
        name = f"{date}_{pid}{ext}"
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(data if data is not None else jpeg_bytes(seed if seed is not None else int(pid)))
        if json_ok:
            meta = {"id": pid, "set_id": sid, "num": num, "next_photo_id": next_id,
                    "first_photo_id": first_id or "", "date": "2025-01-01 00:00:00",
                    "title": title, "username": account, "category": "facebook"}
            with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
        self.archive.append((sid, pid))
        return os.path.join(d, name)

    def chain(self, account, title, sid, ids, skip=(), **kw):
        for i, pid in enumerate(ids):
            if pid in skip:
                continue
            nxt = ids[i + 1] if i + 1 < len(ids) else ""
            self.photo(account, title, sid, pid, i + 1, nxt, ids[0], **kw)


def write_archive(path, keys):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE archive (entry TEXT PRIMARY KEY) WITHOUT ROWID")
    con.executemany("INSERT OR IGNORE INTO archive VALUES (?)", [(f"facebook{s}_{p}",) for s, p in keys])
    con.commit()
    con.close()


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "facebook"
    L = Lib(str(root))
    # a.1: complete and intact (non-latin names)
    L.chain("Алексей", "Новый год 🎄", "a.1", ["101", "102", "103", "104", "105"])
    # a.2: photo 203 missing in the middle, 206 truncated
    L.chain("Camille", "Été", "a.2", ["201", "202", "203", "204", "205"], skip=("203",))
    L.photo("Camille", "Été", "a.2", "206", 6, "", "201", data=jpeg_bytes(206)[:-400])
    # a.3: being downloaded right now; 303 not reached yet
    L.chain("Camille", "En cours", "a.3", ["301", "302"])
    p = os.path.join(str(root), "Camille", "En cours (a.3)", "20250101_000000_302.jpg.json")
    d = json.load(open(p))
    d["next_photo_id"] = "303"
    json.dump(d, open(p, "w"))
    # a.4: duplicates + format problems
    base = jpeg_bytes(101)
    L.photo("محمد", "ألبوم", "a.4", "401", 1, "402", "401", data=with_app_segment(base))   # = 101
    L.photo("محمد", "ألبوم", "a.4", "402", 2, "403", "401", data=jpeg_bytes(102, brighten=6))  # ~ 102
    L.photo("محمد", "ألبوم", "a.4", "403", 3, "404", "401",
            data=b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1" + b"\x00" * 200)  # AVIF as .jpg
    L.photo("محمد", "ألبوم", "a.4", "404", 4, "", "401", seed=404)
    L.photo("محمد", "ألبوم", "a.4", "404", 4, "", "401", seed=404, date="20250102_000000")  # same id twice
    adir = os.path.join(str(root), "محمد", "ألبوم (a.4)")
    open(os.path.join(adir, "20250101_000000_499.jpg.json"), "w").write("{}")      # JSON alone
    open(os.path.join(adir, "20250101_000000_498.jpg.part"), "wb").write(b"\xff\xd8")
    # archive knows a photo that is not on disk any more
    keys = list(L.archive) + [("a.1", "199")]
    arch = str(tmp_path / "fb-archive.sqlite3")
    write_archive(arch, keys)
    old = 10 ** 6   # make every file look older than the "transient" window
    for dp, _, names in os.walk(str(root)):
        for n in names:
            os.utime(os.path.join(dp, n), (old, old))
    return str(root), arch, str(tmp_path / "data" / "integrity.sqlite3")


def scan(lib, monkeypatch=None, now_offset=0):
    root, arch, cache = lib
    s = ig.IntegrityScanner(root, cache, arch, max_mbps=1000, current_set=lambda: "a.3",
                            clock=lambda: __import__("time").time() + 3600)
    s.scan_once()
    return s


def test_complete_album_is_verified(lib):
    rep = scan(lib).report
    a1 = rep.albums["a.1"]
    assert a1.media == 5 and a1.checked == 5 and a1.max_num == 5
    assert a1.chain == "ok" and not a1.missing_ids and not a1.bad_files
    # the archive lists a.1/199 which is not on disk
    assert a1.archived_missing == ["199"]
    assert a1.verdict == "problems"


def test_missing_and_damaged(lib):
    rep = scan(lib).report
    a2 = rep.albums["a.2"]
    assert a2.num_gaps == [(3, 3)]
    assert a2.missing_ids == ["203"] and a2.missing_estimate == 1
    assert a2.chain == "broken"
    assert [os.path.basename(e.path) for e in a2.bad_files] == ["20250101_000000_206.jpg"]
    assert "truncated" in a2.bad_files[0].result.problem or "decoded" in a2.bad_files[0].result.problem
    assert a2.verdict == "problems"


def test_album_in_progress_is_not_flagged(lib):
    a3 = scan(lib).report.albums["a.3"]
    assert a3.in_progress and not a3.missing_ids and a3.verdict == "in progress"


def test_duplicates_and_formats(lib):
    rep = scan(lib).report
    a4 = rep.albums["a.4"]
    kinds = {i.kind for i in a4.issues}
    assert {"bad_file", "id_dup", "json_only", "part", "exact_dup"} <= kinds
    bad = {os.path.basename(e.path): e.result.problem for e in a4.bad_files}
    assert "AVIF" in bad["20250101_000000_403.jpg"]
    # identical pixels despite a different header (EXIF rewrite) -> exact duplicate
    groups = [{e.photo_id for e in g} for g in rep.exact_dups]
    assert {"101", "401"} in groups
    # slightly brighter copy -> near-identical pair
    pairs = [{a.photo_id, b.photo_id} for _, a, b in rep.similar]
    assert {"102", "402"} in pairs
    assert not any({"101", "401"} == p for p in pairs)   # exact ones are not repeated


def test_cache_skips_unchanged_files(lib, monkeypatch):
    scan(lib)
    calls = []
    real = ig.check_file
    monkeypatch.setattr(ig, "check_file", lambda p, st: calls.append(p) or real(p, st))
    s = scan(lib)
    assert calls == []
    # touching a file (e.g. fix_order rewrote its EXIF) re-checks only that file
    path = s.report.by_key[next(iter(s.report.by_key))]
    os.utime(path, (2 * 10 ** 6, 2 * 10 ** 6))
    scan(lib)
    assert calls == [path]


def test_read_only(lib):
    """The scanner must never modify the library."""
    root = lib[0]
    before = {}
    for dp, _, names in os.walk(root):
        for n in names:
            p = os.path.join(dp, n)
            st = os.stat(p)
            before[p] = (st.st_size, st.st_mtime_ns)
    scan(lib)
    after = {}
    for dp, _, names in os.walk(root):
        for n in names:
            p = os.path.join(dp, n)
            st = os.stat(p)
            after[p] = (st.st_size, st.st_mtime_ns)
    assert before == after


def test_new_file_waiting_for_its_json_is_not_flagged(tmp_path):
    L = Lib(str(tmp_path / "fb"))
    L.chain("A", "B", "a.9", ["901", "902"])
    L.photo("A", "B", "a.9", "903", 3, json_ok=False)     # just downloaded, JSON not yet written
    s = ig.IntegrityScanner(L.root, None, None, max_mbps=1000)   # real clock
    s.scan_once()
    a = s.report.albums["a.9"]
    assert "no_json" not in {i.kind for i in a.issues}
