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
    assert {"bad_file", "id_dup", "json_only", "part"} <= kinds
    assert "exact_dup" not in kinds                      # removed on request
    bad = {os.path.basename(e.path): e.result.problem for e in a4.bad_files}
    assert "AVIF" in bad["20250101_000000_403.jpg"]
    assert [{e.name for e in g} for g in rep.id_dups] == [
        {"20250101_000000_404.jpg", "20250102_000000_404.jpg"}]


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


# ---------------------------------------------------------------------------
# log vs disk
# ---------------------------------------------------------------------------
def _log_state(build):
    sys.path.insert(0, HERE)
    from fakelog import FakeLog
    from app.logparse import LogState
    from datetime import datetime, timedelta, timezone
    f = FakeLog((datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"))
    f.session_start()
    build(f)
    st = LogState()
    for line in f.text().splitlines():
        st.feed_line(line)
    return st


def test_log_vs_disk(tmp_path):
    from app.status import log_snapshot

    def build(f):
        f.job_start("a.5", sleep_extractor=0)
        f.photo("501").photo("502").photo("503").skipped("504").photo("505").end_chain()
        f.job_start("a.6", sleep_extractor=0)
        f.photo("601").photo("602").photo("603").end_chain()
        f.job_start("a.7", sleep_extractor=0)
        f.photo("701").jump("701", "999999")
    st = _log_state(build)

    L = Lib(str(tmp_path / "fb"))
    # a.5: 504 (skipped as "already had") and 505 (the LAST photo) are gone.
    # The JSON chain sees 504 (503 -> 504), but only the log can see 505.
    L.chain("A", "Five", "a.5", ["501", "502", "503", "504", "505"], skip=("504", "505"))
    L.chain("A", "Six", "a.6", ["601", "602", "603"])
    L.photo("A", "Six", "a.6", "699", 4)                  # on disk, not in the last full pass
    L.chain("A", "Seven", "a.7", ["701"])
    L.chain("A", "Nine", "a.9", ["901", "902"])           # never in the log
    s = ig.IntegrityScanner(L.root, None, None, max_mbps=1000,
                            log_snapshot=lambda: log_snapshot(st))
    s.scan_once()
    r = s.report.albums

    a5 = r["a.5"]
    assert sorted(a5.log_not_on_disk) == [("504", "skipped"), ("505", "downloaded")]
    assert a5.missing_estimate == 2 and set(a5.missing_ids) == {"504", "505"}
    kinds = {i.kind for i in a5.issues}
    assert {"log_skipped_missing", "log_downloaded_missing", "missing"} <= kinds
    assert a5.verdict == "problems" and a5.log_expected == 5

    a6 = r["a.6"]
    assert a6.log_confirmed and a6.log_extra_on_disk == ["699"]
    assert a6.verdict == "verified"          # extra photos are only informative

    assert "log_jump" in {i.kind for i in r["a.7"].issues} and r["a.7"].verdict == "warnings"

    a9 = r["a.9"]
    assert a9.verdict == "verified" and not a9.log_confirmed and a9.log_passes == 0


def test_photo_seen_after_the_listing_is_ignored(tmp_path):
    from app.status import log_snapshot
    from datetime import datetime, timezone
    from app.logparse import LogState
    sys.path.insert(0, HERE)
    from fakelog import FakeLog

    # log lines written "now", i.e. after the scanner listed the files
    f = FakeLog(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    f.session_start().job_start("a.8", sleep_extractor=0)
    f.photo("801", ).photo("802")
    st = LogState()
    for line in f.text().splitlines():
        st.feed_line(line)
    L = Lib(str(tmp_path / "fb"))
    L.chain("A", "Eight", "a.8", ["801"])
    s = ig.IntegrityScanner(L.root, None, None, max_mbps=1000,
                            log_snapshot=lambda: log_snapshot(st))
    s.scan_once()
    assert s.report.albums["a.8"].log_not_on_disk == []


def test_rebuild_follows_the_log_without_rereading(tmp_path, monkeypatch):
    from app.status import log_snapshot
    L = Lib(str(tmp_path / "fb"))
    L.chain("A", "Six", "a.6", ["601", "602"])
    state = {"st": _log_state(lambda f: None)}
    s = ig.IntegrityScanner(L.root, None, None, max_mbps=1000,
                            log_snapshot=lambda: log_snapshot(state["st"]))
    s.scan_once()
    assert s.report.albums["a.6"].log_passes == 0
    state["st"] = _log_state(lambda f: f.job_start("a.6", sleep_extractor=0)
                             .photo("601").photo("602").photo("603").end_chain())
    monkeypatch.setattr(ig, "check_file", lambda *a: (_ for _ in ()).throw(AssertionError("re-read")))
    s.rebuild()
    a6 = s.report.albums["a.6"]
    assert a6.log_passes == 1 and [p for p, _ in a6.log_not_on_disk] == ["603"]


def test_archive_entry_newer_than_the_listing_is_not_missing(tmp_path):
    """Regression: between full passes the report is rebuilt with the current
    archive but the old file listing; photos downloaded meanwhile were flagged
    'in the archive but not on disk'."""
    L = Lib(str(tmp_path / "fb"))
    L.chain("A", "Current", "a.50", ["5001", "5002"])
    arch = str(tmp_path / "fb-archive.sqlite3")
    write_archive(arch, L.archive)
    s = ig.IntegrityScanner(L.root, None, arch, max_mbps=1000, current_set=lambda: "a.50")
    s.scan_once()
    assert s.report.albums["a.50"].archived_missing == []
    # gallery-dl downloads one more photo and archives it after the listing
    L.photo("A", "Current", "a.50", "5003", 3, "", "5001")
    write_archive_add(arch, [("a.50", "5003"), ("a.50", "5009")])   # 5009: really missing
    s.rebuild()
    assert s.report.albums["a.50"].archived_missing == ["5009"]


def write_archive_add(path, keys):
    con = sqlite3.connect(path)
    con.executemany("INSERT OR IGNORE INTO archive VALUES (?)", [(f"facebook{s}_{p}",) for s, p in keys])
    con.commit()
    con.close()


def test_rescan_one_album(tmp_path):
    L = Lib(str(tmp_path / "fb"))
    L.chain("A", "One", "a.60", ["6001", "6002"])
    L.chain("A", "Two", "a.61", ["6101"])
    arch = str(tmp_path / "fb-archive.sqlite3")
    write_archive(arch, L.archive)
    s = ig.IntegrityScanner(L.root, None, arch, max_mbps=1000)
    s.scan_once()
    listed = s.report.albums["a.60"].listed_at
    # a new photo arrives in a.60 (chain extended), a.61 untouched
    p = os.path.join(L.root, "A", "One (a.60)", "20250101_000000_6002.jpg.json")
    d = json.load(open(p))
    d["next_photo_id"] = "6003"
    json.dump(d, open(p, "w"))
    L.photo("A", "One", "a.60", "6003", 3, "", "6001")
    calls = []
    real = ig.check_file
    ig.check_file = lambda path, st: calls.append(os.path.basename(path)) or real(path, st)
    try:
        assert s.rescan_album("a.60")
    finally:
        ig.check_file = real
    a60 = s.report.albums["a.60"]
    assert a60.media == 3 and a60.chain == "ok" and a60.max_num == 3
    assert calls == ["20250101_000000_6003.jpg"]           # only the new file is read
    assert a60.listed_at > listed
    assert s.report.albums["a.61"].listed_at == listed     # other albums keep their listing
    assert s.report.albums["a.61"].media == 1


def test_missing_reasons_and_ignore(tmp_path):
    L = Lib(str(tmp_path / "fb"))
    # 7003 missing in the middle (gap at position 3); the last photo points to 7009 (end)
    L.chain("A", "Seven", "a.70", ["7001", "7002", "7003", "7004", "7009"], skip=("7003", "7009"))
    ignored = {}
    s = ig.IntegrityScanner(L.root, None, None, max_mbps=1000, ignored=lambda: ignored)
    s.scan_once()
    a = s.report.albums["a.70"]
    assert a.missing_in_order == ["7003", "7009"] and a.missing_pos == {"7003": 3, "7009": 5}
    assert "END of the album" in a.missing_why["7009"][0]
    assert "position 2" in a.missing_why["7003"][0]
    assert a.verdict == "problems"
    # the end one is Facebook wrapping to another album: ignore it
    ignored["a.70"] = {"7009": "belongs to another album"}
    s.rebuild()
    a = s.report.albums["a.70"]
    assert a.missing_ids == ["7003"] and a.missing_estimate == 1
    assert a.ignored_missing == {"7009": "belongs to another album"}
    # ignoring the middle one too clears the album (its gap is accounted for)
    ignored["a.70"]["7003"] = "deleted on Facebook"
    s.rebuild()
    a = s.report.albums["a.70"]
    assert a.missing_estimate == 0 and a.num_gaps == [] and a.verdict == "verified"
