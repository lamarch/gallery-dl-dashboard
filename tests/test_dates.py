"""Dates & order (fix_order.py) detection. Files are written the way fix_order
writes them (EXIF DateTimeOriginal + OffsetTimeOriginal, mtime), with Pillow."""
import io
import json
import os
import struct
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from PIL import Image  # noqa: E402

from app import integrity as ig  # noqa: E402
from app.dates import build_dates, embedded_instant  # noqa: E402

ZONE = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


def jpeg(seed, when=None, tz=ZONE):
    im = Image.new("RGB", (32, 24), ((seed * 37) % 256, (seed * 91) % 256, (seed * 53) % 256))
    exif = Image.Exif()
    if when is not None:
        local = when.astimezone(tz)
        off = local.strftime("%z")
        ifd = exif.get_ifd(0x8769)
        ifd[0x9003] = local.strftime("%Y:%m:%d %H:%M:%S")
        ifd[0x9011] = f"{off[:3]}:{off[3:]}"
        if local.microsecond:
            ifd[0x9291] = f"{local.microsecond // 1000:03d}"    # SubSecTimeOriginal
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def mp4(created_utc=None):
    secs = 0 if created_utc is None else int((created_utc - datetime(1904, 1, 1, tzinfo=UTC)).total_seconds())
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom\0\0\2\0"
    mvhd_body = b"\0\0\0\0" + struct.pack(">II", secs, secs) + b"\0" * 88
    mvhd = struct.pack(">I4s", 8 + len(mvhd_body), b"mvhd") + mvhd_body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    mdat = struct.pack(">I4s", 24, b"mdat") + b"\1" * 16
    return ftyp + moov + mdat


def T(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


class Lib:
    def __init__(self, root):
        self.root = root

    def album(self, sid, photos):
        """photos: [(id, json_date, fixed_target_or_None, ext)] in album order."""
        d = os.path.join(self.root, "Acc", f"Album {sid} ({sid})")
        os.makedirs(d, exist_ok=True)
        ids = [p[0] for p in photos]
        for i, (pid, date, fixed, ext) in enumerate(photos):
            name = f"20240501_100000_{pid}{ext}"
            path = os.path.join(d, name)
            data = mp4(fixed) if ext == ".mp4" else jpeg(int(pid), fixed)
            open(path, "wb").write(data)
            json.dump({"id": pid, "set_id": sid, "num": i + 1, "date": date,
                       "next_photo_id": ids[i + 1] if i + 1 < len(ids) else "",
                       "first_photo_id": ids[0], "category": "facebook"},
                      open(path + ".json", "w"))
            t = fixed.timestamp() if fixed else 1700000000   # gallery-dl: HTTP Last-Modified
            os.utime(path, (t, t))
        return d


def run(root, tz=ZONE, tz_name="Europe/Berlin", step=1.0):
    s = ig.IntegrityScanner(root, None, None, max_mbps=1000)
    s.scan_once()
    return build_dates(s._last_lists[0], tz, tz_name, step, s.report.albums, None), s.report


B = "2024-05-01 10:00:00"


def test_never_run_then_applied(tmp_path):
    L = Lib(str(tmp_path))
    L.album("a.1", [("101", B, None, ".jpg"), ("102", B, None, ".jpg"), ("103", "2024-05-02 08:00:00", None, ".jpg")])
    dr, rep = run(L.root)
    a = dr.albums["a.1"]
    assert a.state == "not run" and a.danger == 0
    assert [f.target for f in a.files] == [T(B), T(B) + timedelta(seconds=1), T("2024-05-02 08:00:00")]
    assert a.ready
    # now as fix_order would have written them (batch of 2 spread by 1 s)
    L2 = Lib(str(tmp_path / "applied"))
    L2.album("a.1", [("101", B, T(B), ".jpg"), ("102", B, T(B) + timedelta(seconds=1), ".jpg"),
                     ("103", "2024-05-02 08:00:00", T("2024-05-02 08:00:00"), ".jpg")])
    dr, _ = run(L2.root)
    a = dr.albums["a.1"]
    assert a.state == "applied" and a.danger == 0
    assert dr.offsets == {"+02:00": 3} and dr.steps_seen == {1.0: 1}
    # same files, but checked as if run with another --tz: every EXIF would be rewritten
    dr, _ = run(L2.root, UTC, "UTC")
    a = dr.albums["a.1"]
    assert a.state == "changed" and a.danger == 3 and a.danger_content == 3 and dr.tz_mismatch == 3


def test_album_changed_after_correction(tmp_path):
    """A photo appears inside a same-second batch after fix_order ran: the
    following photos shift by one rank -> already-corrected files would change."""
    L = Lib(str(tmp_path))
    L.album("a.2", [("201", B, T(B), ".jpg"), ("205", B, None, ".jpg"),          # 205: recovered later
                    ("202", B, T(B) + timedelta(seconds=1), ".jpg")])
    dr, _ = run(L.root)
    a = dr.albums["a.2"]
    states = {f.entry.photo_id: f.state for f in a.files}
    assert states == {"201": "ok", "205": "never", "202": "changed"}
    assert a.state == "changed" and a.danger == 1 and a.danger_content == 1


def test_new_photo_only_is_partial(tmp_path):
    L = Lib(str(tmp_path))
    L.album("a.3", [("301", B, T(B), ".jpg"), ("302", "2024-06-01 09:00:00", None, ".jpg")])
    a = run(L.root)[0].albums["a.3"]
    assert a.state == "partial" and a.danger == 0


def test_overlaps(tmp_path):
    L = Lib(str(tmp_path))
    # batch of 3 at :00 spreads to :02, past the next batch at :01 -> overflow + exact collision
    L.album("a.4", [("401", B, None, ".jpg"), ("402", B, None, ".jpg"), ("403", B, None, ".jpg"),
                    ("404", "2024-05-01 10:00:01", None, ".jpg")])
    L.album("a.5", [("501", B, None, ".jpg")])     # same second as a.4's first photo
    dr, _ = run(L.root)
    a4 = dr.albums["a.4"]
    assert len(a4.overflows) == 1 and a4.inner_collisions == 1
    assert a4.cross_collisions == {"a.5": 1} and dr.albums["a.5"].cross_collisions == {"a.4": 1}


def test_video_dates(tmp_path):
    L = Lib(str(tmp_path))
    # one video corrected (QuickTime date = target, mtime = target),
    # one with an encoder date unrelated to the target (not "corrected")
    d = L.album("a.6", [("601", B, T(B), ".mp4"), ("602", "2024-05-03 10:00:00", None, ".mp4")])
    p = [os.path.join(d, n) for n in os.listdir(d) if n.endswith("602.mp4")][0]
    open(p, "wb").write(mp4(T("2019-01-01 00:00:00")))
    os.utime(p, (1700000000, 1700000000))
    a = run(L.root)[0].albums["a.6"]
    st = {f.entry.photo_id: (f.state, f.corrected) for f in a.files}
    assert st == {"601": ("ok", True), "602": ("never", False)}
    assert a.danger == 0


def test_embedded_instant_formats():
    class R:
        def __init__(self, kind, value, offset=None, subsec=None):
            self.dt_kind, self.dt_value, self.dt_offset, self.dt_subsec = kind, value, offset, subsec
    assert embedded_instant(R("exif", "2024:05:01 12:00:00", "+02:00")) == T(B)
    assert embedded_instant(R("exif", "2024:05:01 12:00:00", "+02:00", "500")) == T(B) + timedelta(milliseconds=500)
    assert embedded_instant(R("exif", "2024:05:01 12:00:00")) is None          # no zone: not comparable
    assert embedded_instant(R("qt", "2024:05:01 10:00:00")) == T(B)
    assert embedded_instant(R("xmp", "2024-05-01T12:00:00+02:00")) == T(B)
    assert embedded_instant(R("xmp", "2024:05:01 12:00:00+02:00")) == T(B)


def test_never_writes(tmp_path):
    L = Lib(str(tmp_path))
    L.album("a.7", [("701", B, None, ".jpg"), ("702", B, T(B), ".jpg")])
    snap = lambda: {os.path.join(dp, n): (os.stat(os.path.join(dp, n)).st_mtime_ns,  # noqa: E731
                                          open(os.path.join(dp, n), "rb").read())
                    for dp, _, ns in os.walk(L.root) for n in ns}
    before = snap()
    run(L.root)
    assert snap() == before


def test_ready_only_when_album_is_proven_complete(tmp_path):
    from app.status import log_snapshot
    sys.path.insert(0, HERE)
    from fakelog import FakeLog
    from app.logparse import LogState
    f = FakeLog((datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")).session_start()
    f.job_start("a.8", sleep_extractor=0)
    f.photo("801").photo("802").end_chain()
    f.job_start("a.9", sleep_extractor=0)
    f.photo("901").jump("901", "999999")
    st = LogState()
    for line in f.text().splitlines():
        st.feed_line(line)
    L = Lib(str(tmp_path))
    L.album("a.8", [("801", B, None, ".jpg"), ("802", B, None, ".jpg")])
    L.album("a.9", [("901", B, None, ".jpg")])
    L.album("a.10", [("1001", B, None, ".jpg")])                       # not in the log
    L.album("a.11", [("1101", B, None, ".jpg"), ("1103", B, None, ".jpg")])
    os.remove(os.path.join(tmp_path, "Acc", "Album a.11 (a.11)", "20240501_100000_1101.jpg"))  # broken
    s = ig.IntegrityScanner(L.root, None, None, max_mbps=1000, log_snapshot=lambda: log_snapshot(st),
                            current_set=lambda: None)
    s.scan_once()
    dr = build_dates(s._last_lists[0], ZONE, "Europe/Berlin", 1.0, s.report.albums, "a.10x")
    ready = {sid: a.ready for sid, a in dr.albums.items()}
    assert ready == {"a.8": True, "a.9": False, "a.10": True, "a.11": False}
    assert "did not reach the end" in dr.albums["a.9"].ready_reason


def test_interleaving_in_the_current_timeline(tmp_path):
    L = Lib(str(tmp_path))
    day = lambda h, m=0: T(f"2024-05-01 {h:02d}:{m:02d}:00")  # noqa: E731
    # a.20 corrected: 10:00 .. 12:00 ; a.21 corrected: 11:00 (inside a.20's span) and 13:00
    L.album("a.20", [("2001", "2024-05-01 10:00:00", day(10), ".jpg"),
                     ("2002", "2024-05-01 12:00:00", day(12), ".jpg")])
    L.album("a.21", [("2101", "2024-05-01 11:00:00", day(11), ".jpg"),
                     ("2102", "2024-05-01 13:00:00", day(13), ".jpg")])
    # a.22 not corrected: Immich shows the file time (gallery-dl's Last-Modified)
    L.album("a.22", [("2201", "2024-05-01 18:00:00", None, ".jpg"),
                     ("2202", "2024-05-01 19:00:00", None, ".jpg")])
    dr, _ = run(L.root)
    pairs = {(i.a, i.b): (i.inside, i.a_fixed, i.b_fixed) for i in dr.interleaved}
    # 2101 (11:00) is inside a.20 (10-12), 2002 (12:00) is inside a.21 (11-13)
    assert pairs == {("a.20", "a.21"): (1, True, True), ("a.21", "a.20"): (1, True, True)}
    # the uncorrected album sits at its file time (2023-11-14), far away: no interleave
    assert dr.albums["a.22"].files[0].timeline.year == 2023
    assert [i.b for i in dr.albums["a.20"].interleaved_by] == ["a.21"]


def test_mtime_rounded_by_exiftool_is_harmless(tmp_path):
    """Seen live with --step 0.1: exiftool -P (run after fix_order set the
    mtime) restores mtimes with 1 s precision; only 1 photo in 10 keeps an
    exact mtime. EXIF is exact, Immich orders by it: not a 'changed' album."""
    import math
    L = Lib(str(tmp_path))
    n = 25
    photos = [(str(3000 + i), B, T(B) + timedelta(seconds=round(i * 0.1, 1)), ".jpg") for i in range(n)]
    d = L.album("a.30", photos)
    for i, (pid, _, fixed, _) in enumerate(photos):
        p = os.path.join(d, f"20240501_100000_{pid}.jpg")
        t = math.floor(fixed.timestamp())                     # what exiftool -P leaves
        os.utime(p, (t, t))
    dr, _ = run(L.root, step=0.1)
    a = dr.albums["a.30"]
    st = Counter(f.state for f in a.files)
    assert st == {"rounded": 22, "ok": 3}                   # ranks 0, 10, 20 stay exact
    assert a.state == "applied" and a.rounded == 22 and a.danger == 0
    assert sum(f.would_mtime for f in a.files) == 22         # fix_order would still touch them
