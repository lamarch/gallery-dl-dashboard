"""rename_immich_albums.py against a fake Immich API (same endpoints as
Immich's album.controller: GET /api/albums, PATCH /api/albums/:id,
GET /api/assets/:id)."""
import http.server
import json
import os
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import rename_immich_albums as tool  # noqa: E402

KEY = "test-key"


class FakeImmich(http.server.BaseHTTPRequestHandler):
    albums, assets, patches = [], {}, []

    def log_message(self, *a):
        pass

    def _send(self, code, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        if self.headers.get("x-api-key") != KEY:
            self._send(401, {"message": "Invalid API key"})
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
        if self.path == "/api/albums":
            return self._send(200, self.albums)
        if self.path.startswith("/api/assets/"):
            a = self.assets.get(self.path.rsplit("/", 1)[1])
            return self._send(200, a) if a else self._send(404, {})
        self._send(404, {})

    def do_PATCH(self):
        if not self._auth():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        aid = self.path.rsplit("/", 1)[1]
        type(self).patches.append((aid, body))
        for a in self.albums:
            if a["id"] == aid:
                a.update(body)
                return self._send(200, a)
        self._send(404, {})


@pytest.fixture
def immich(tmp_path):
    root = "/ext/facebook/Acc"
    FakeImmich.albums = [
        {"id": "u1", "albumName": "Vacances (a.1)", "albumThumbnailAssetId": "t1"},
        {"id": "u2", "albumName": "Ete", "albumThumbnailAssetId": "t2"},          # name without "(a.2)"
        {"id": "u3", "albumName": "Autre (a.3)", "albumThumbnailAssetId": "t3"},   # cover in another folder
        {"id": "u4", "albumName": "Double (a.4)", "albumThumbnailAssetId": "t4"},
        {"id": "u4b", "albumName": "Double (a.4)", "albumThumbnailAssetId": "t4"},
        {"id": "u5", "albumName": "Old (a.5)", "albumThumbnailAssetId": "t5"},
        {"id": "u5n", "albumName": "New name 5", "albumThumbnailAssetId": "t5"},   # already created
    ]
    FakeImmich.assets = {
        "t1": {"originalPath": f"{root}/Vacances (a.1)/x.jpg"},
        "t2": {"originalPath": f"{root}/Ete (a.2)/x.jpg"},
        "t3": {"originalPath": f"{root}/Something else (a.99)/x.jpg"},
        "t4": {"originalPath": f"{root}/Double (a.4)/x.jpg"},
        "t5": {"originalPath": f"{root}/Old (a.5)/x.jpg"},
    }
    FakeImmich.patches = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeImmich)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    names = tmp_path / "album-names.tsv"
    names.write_text("set_id\tfolder\tgallerydl_title\tfacebook_title\tcustom_name\n"
                     "a.1\tVacances (a.1)\t\t\tVacances 2019 🌞\n"
                     "a.2\tEte (a.2)\t\t\tÉté\n"
                     "a.3\tAutre (a.3)\t\t\tAutre nom\n"
                     "a.4\tDouble (a.4)\t\t\tDouble nom\n"
                     "a.5\tOld (a.5)\t\t\tNew name 5\n"
                     "a.6\tNone (a.6)\t\t\tSix\n"
                     "a.7\tKeep (a.7)\t\t\t\n", encoding="utf-8")
    yield f"http://127.0.0.1:{srv.server_address[1]}", str(names)
    srv.shutdown()


def run(url, names, *extra, key=KEY, monkeypatch=None):
    monkeypatch.setenv("IMMICH_API_KEY", key)
    return tool.main(["--url", url, "--names", names, *extra])


def test_plan_and_dry_run(immich, capsys, monkeypatch):
    url, names = immich
    assert run(url, names, monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert FakeImmich.patches == []                             # dry run
    lines = {l.split()[1]: l.split()[0] for l in out.splitlines() if l[:1].isalpha() and " a." in l}
    assert lines == {"a.1": "rename", "a.2": "rename", "a.3": "skip", "a.4": "skip",
                     "a.5": "conflict", "a.6": "none"}
    assert "DRY RUN" in out


def test_write_renames_only_safe_matches(immich, monkeypatch):
    url, names = immich
    run(url, names, "--write", monkeypatch=monkeypatch)
    assert sorted(FakeImmich.patches) == [("u1", {"albumName": "Vacances 2019 🌞"}),
                                          ("u2", {"albumName": "Été"})]
    # second run: nothing left to rename
    FakeImmich.patches = []
    run(url, names, "--write", monkeypatch=monkeypatch)
    assert FakeImmich.patches == []


def test_bad_key_is_reported(immich, monkeypatch):
    url, names = immich
    with pytest.raises(SystemExit) as e:
        run(url, names, key="wrong", monkeypatch=monkeypatch)
    assert "401" in str(e.value)
