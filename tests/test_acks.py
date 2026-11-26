import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from app.logparse import LogState  # noqa: E402
from fakelog import FakeLog  # noqa: E402


def log_text():
    f = FakeLog().session_start()
    f.job_start("a.1", sleep_extractor=0)
    f.photo("11").page("12").no_url_retry("12").page("12").no_url_skip("12")
    f.download_retry(1).download_retry(1)          # identical messages, same second
    f.end_chain()
    f.job_start("a.2", sleep_extractor=0)
    f.photo("21").page("22").blocked("22")
    return f.text()


def parse(text):
    st = LogState()
    for line in text.splitlines():
        st.feed_line(line)
    return st


def test_event_keys_are_stable_and_unique():
    a, b = parse(log_text()), parse(log_text())
    keys = [e.key for e in a.events]
    assert keys == [e.key for e in b.events]            # same log -> same keys (restart)
    assert len(set(keys)) == len(keys) and all(keys)   # identical events still distinct


def test_ack_flow(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    logf = tmp_path / "fb-dl.log"
    logf.write_text(log_text())
    for k, v in {"DATA_DIR": str(tmp_path), "LOG_FILE": str(logf), "INTEGRITY": "0",
                 "ALBUMS_FILE": str(tmp_path / "none.txt"), "DOWNLOADS_ROOT": str(tmp_path / "dl")}.items():
        monkeypatch.setenv(k, v)
    import importlib
    import app.main as m
    from fastapi.testclient import TestClient
    h = {"sec-fetch-site": "same-origin"}

    def badge(c):
        mm = re.search(r'Events <span class="pill bad">(\d+)</span>', c.get("/events").text)
        return int(mm.group(1)) if mm else 0

    m = importlib.reload(m)
    with TestClient(m.app) as c:
        bad_before = badge(c)
        assert bad_before >= 2                           # photo_missing + blocked
        missing = next(e for e in m.MON.tail.state.events if e.kind == "photo_missing")
        assert c.post("/events/ack", data={"key": missing.key},
                      headers={"sec-fetch-site": "cross-site"}).status_code == 403
        c.post("/events/ack", data={"key": missing.key}, headers=h)
        assert badge(c) == bad_before - 1
        assert missing.key not in c.get("/events").text            # hidden from "new"
        assert missing.key in c.get("/events?show=acked").text
        c.post("/events/ack", data={"key": missing.key, "undo": "1"}, headers=h)
        assert badge(c) == bad_before
        c.post("/events/ack", data={"scope": "filtered"}, headers=h)   # acknowledge all
        assert badge(c) == 0
        assert "no new serious problems" in c.get("/").text
    # restart: the log is re-read, keys are the same, acknowledgements still apply
    m = importlib.reload(m)
    with TestClient(m.app) as c:
        assert badge(c) == 0
        # a NEW problem appears in the log: it shows up
        with open(logf, "a") as fh:
            fh.write(FakeLog("2026-11-23 10:00:00").job_start("a.3", sleep_extractor=0)
                     .page("31").no_url_skip("31").text())
        m.MON.refresh()
        assert badge(c) == 1
