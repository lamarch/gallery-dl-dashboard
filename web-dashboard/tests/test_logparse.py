import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from app.logparse import LogState, LogTail, sanitize_url, set_id_from_url  # noqa: E402
from fakelog import FakeLog  # noqa: E402


def parse(text):
    st = LogState()
    for line in text.splitlines(True):
        st.feed_line(line)
    return st


def scenario():
    f = FakeLog()
    for _ in range(8):
        f.raw("[%(asctime)s][%(levelname)s] %(name)s: %(message)s")
    f.session_start()
    # Album A: downloaded, skipped, soft-blocked twice then missing, downloaded, end
    f.job_start("a.100")
    f.photo("1001").skipped("1002")
    f.page("1003").no_url_retry("1003").page("1003").no_url_retry("1003").page("1003").no_url_skip("1003")
    f.page("1004").download_retry(1).download("1004", size=1000, avif=False)
    f.end_chain()
    # Album B: power cut in the middle
    f.job_start("a.200")
    f.photo("2001").photo("2002")
    f.at(7200)
    # restart after the power cut, now with the archive
    f.session_start()
    f.job_start("a.200", archive=True)
    f.skipped("2001").skipped("2002").photo("2003").jump("2003", "9999999")
    # Album C: blocked
    f.job_start("a.300", archive=True)
    f.photo("3001").page("3002").blocked("3002")
    f.wrapper_exit(4)
    return f


def test_sessions_and_jobs():
    st = parse(scenario().text())
    assert st.bad_format_lines == 8
    assert len(st.sessions) == 2
    s1, s2 = st.sessions
    assert s1.end_reason == "interrupted" and s1.exit_code is None
    assert s2.end_reason == "exited" and s2.exit_code == 4
    assert s2.archive is True and s1.archive is None
    assert [j.set_id for j in st.jobs] == ["a.100", "a.200", "a.200", "a.300"]
    a, b1, b2, c = st.jobs
    assert a.end_reason == "complete"
    assert b1.end_reason == "interrupted"
    assert b2.end_reason == "jump"
    assert c.end_reason == "blocked"
    assert s1.sleep_seconds["extractor"] == 7200
    assert s1.wait_count == 2 and s1.wait_seconds == 1800


def test_photos():
    st = parse(scenario().text())
    a, b1, b2, c = st.jobs
    assert {k: p.status for k, p in a.photos.items()} == {
        "1001": "downloaded", "1002": "skipped", "1003": "missing", "1004": "downloaded"}
    assert a.photos["1003"].url_retries == 2 and a.photos["1003"].page_requests == 3
    assert a.photos["1004"].download_retries == 1
    assert a.photos["1001"].filename == "20250506_082802_1001.jpg"
    assert a.bytes == 812345 + 1000
    assert {k: p.status for k, p in b2.photos.items()} == {
        "2001": "skipped", "2002": "skipped", "2003": "downloaded"}
    assert c.photos["3002"].status == "skipped"   # page loaded, then blocked: no file


def test_events_and_privacy():
    text = scenario().text()
    st = parse(text)
    kinds = [e.kind for e in st.events]
    assert kinds.count("photo_missing") == 1
    assert kinds.count("photo_retry_url") == 2
    assert kinds.count("wait_429") == 2
    assert "session_interrupted" in kinds and "blocked" in kinds and "jump" in kinds
    assert "download_retry" in kinds and "wrapper_exit" in kinds
    blocked = next(e for e in st.events if e.kind == "blocked")
    assert any("Traceback" in d for d in blocked.details)        # pre-logged traceback
    assert any("setextract" in d for d in blocked.details)       # message continuation
    assert not st.unrecognized, st.unrecognized
    # no CDN token anywhere in what we keep
    dump = repr([(e.message, e.details) for e in st.events]) + repr([j.url for j in st.jobs])
    assert "SECRET" not in dump and "fbcdn" not in dump


def test_sanitize():
    assert sanitize_url("https://scontent.xx.fbcdn.net/v/x.jpg?oh=1&oe=2") == "<cdn>"
    assert sanitize_url("https://www.facebook.com/photo/?fbid=1&set=a.2&__tn__=X") == "/photo/?fbid=1&set=a.2"
    assert set_id_from_url("https://www.facebook.com/media/set/?set=a.123&type=3") == "a.123"
    assert set_id_from_url("https://www.facebook.com/photo/?fbid=1&set=a.123.") in ("a.123", "a.123.")


def test_tail_incremental_and_rotation(tmp_path):
    p = tmp_path / "fb-dl.log"
    text = scenario().text()
    half = len(text) // 2
    p.write_text(text[:half])
    t = LogTail(str(p))
    t.poll()
    with open(p, "a") as fh:
        fh.write(text[half:])
    t.poll()
    full = parse(text)
    assert len(t.state.events) == len(full.events)
    assert [j.end_reason for j in t.state.jobs] == [j.end_reason for j in full.jobs]
    # truncation -> full reparse
    p.write_text(FakeLog().session_start().text())
    t.poll()
    assert t.resets == 1 and len(t.state.sessions) == 1


def test_big_log_speed(tmp_path):
    import time
    f = FakeLog().session_start()
    for a in range(60):
        f.job_start(f"a.{a}")
        for i in range(500):
            f.photo(str(10**6 * (a + 1) + i))
        f.end_chain()
    text = f.text()
    t0 = time.perf_counter()
    st = parse(text)
    dt = time.perf_counter() - t0
    assert len(st.jobs) == 60 and sum(len(j.photos) for j in st.jobs) == 30000
    print(f"{len(text)/1e6:.1f} MB parsed in {dt:.2f}s")
    assert dt < 20
