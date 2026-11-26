import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

from app.names import NamesFile, albumprops_content, clean_name, read_albumprops  # noqa: E402
import apply_albumprops as tool  # noqa: E402

WEIRD = [
    "Été 🌞 Mariage de Léa",
    "Новый год 2020",
    "ألبوم العائلة‏",                  # right-to-left mark kept
    "Family 👨‍👩‍👧",            # emoji with zero-width joiners kept
    'Quotes "double" and \'single\' # not a comment: colon',
    "back\\slash",
]


def test_clean_name():
    for w in WEIRD:
        assert clean_name(w) == w.strip()
    assert clean_name("  a \t b\n c ") == "a b c"
    assert clean_name("x\x00y\x07") == "xy"
    with pytest.raises(ValueError):
        clean_name("x" * 201)


def test_file_roundtrip_and_hand_edits(tmp_path):
    p = str(tmp_path / "album-names.tsv")
    nf = NamesFile(p)
    nf.sync({"a.1": ("Vacances (a.1)", "Vacances"), "a.2": ("Tab\there (a.2)", "x")}, write=True)
    nf.set_custom("a.1", WEIRD[4])
    again = NamesFile(p)
    again.load()
    assert again.rows["a.1"].custom_name == WEIRD[4]
    assert again.rows["a.2"].folder == "Tab\there (a.2)"          # escaped tab survives
    assert again.rows["a.1"].folder_title == "Vacances"
    # someone edits the file by hand, then a name is saved in the UI: both survive
    txt = open(p, encoding="utf-8").read().replace("\tx\t\t\n", "\tx\t\tHand made\n")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(txt)
    os.utime(p, (1, 1))
    nf.set_custom("a.1", "From UI")
    final = NamesFile(p)
    final.load()
    assert final.rows["a.2"].custom_name == "Hand made" and final.rows["a.1"].custom_name == "From UI"
    assert os.path.exists(p + ".bak")
    # sync never touches custom names
    nf.sync({"a.1": ("Vacances renamed (a.1)", "Vacances")}, write=True)
    assert nf.rows["a.1"].custom_name == "From UI"


def test_duplicates(tmp_path):
    nf = NamesFile(str(tmp_path / "n.tsv"))
    nf.sync({"a.1": ("A (a.1)", ""), "a.2": ("B (a.2)", "")}, write=True)
    nf.set_custom("a.1", "Same")
    nf.set_custom("a.2", "same")
    assert nf.duplicates() == {"same": ["a.1", "a.2"]}


def test_albumprops_is_valid_yaml_for_the_album_creator():
    yaml = pytest.importorskip("yaml")
    for w in WEIRD:
        assert yaml.safe_load(albumprops_content(w)) == {"override_name": w}


def _lib(tmp_path):
    root = tmp_path / "facebook"
    for sid, title in (("a.1", "Vacances"), ("a.2", "Новый год"), ("a.3", "Foreign")):
        d = root / "Acc" / f"{title} ({sid})"
        d.mkdir(parents=True)
        (d / "20240101_000000_1.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (root / "Acc" / "Foreign (a.3)" / ".albumprops").write_text("override_name: \"theirs\"\n", encoding="utf-8")
    names = tmp_path / "album-names.tsv"
    nf = NamesFile(str(names))
    nf.sync({"a.1": ("Vacances (a.1)", ""), "a.2": ("Новый год (a.2)", ""),
             "a.3": ("Foreign (a.3)", ""), "a.9": ("Gone (a.9)", "")}, write=True)
    nf.set_custom("a.1", WEIRD[0])
    nf.set_custom("a.3", "Mine")
    nf.set_custom("a.9", "No folder")
    return root, names


def snapshot(root):
    return {os.path.join(dp, n): open(os.path.join(dp, n), "rb").read()
            for dp, _, ns in os.walk(root) for n in ns}


def test_tool_dry_run_writes_nothing(tmp_path, capsys):
    root, names = _lib(tmp_path)
    before = snapshot(root)
    assert tool.main(["--root", str(root), "--names", str(names)]) == 0
    assert snapshot(root) == before
    out = capsys.readouterr().out
    assert "write" in out and "skip" in out and "missing" in out and "DRY RUN" in out


def test_tool_write_only_albumprops(tmp_path):
    root, names = _lib(tmp_path)
    before = snapshot(root)
    tool.main(["--root", str(root), "--names", str(names), "--write"])
    after = snapshot(root)
    changed = {p for p in after if before.get(p) != after[p]}
    assert changed == {str(root / "Acc" / "Vacances (a.1)" / ".albumprops")}
    assert read_albumprops(str(root / "Acc" / "Vacances (a.1)")) == ("ours", WEIRD[0])
    # a .albumprops not written by the tool is never touched
    assert read_albumprops(str(root / "Acc" / "Foreign (a.3)")) == ("foreign", "theirs")
    # idempotent
    tool.main(["--root", str(root), "--names", str(names), "--write"])
    assert snapshot(root) == after
    # name removed: our file is only removed with --remove-stale
    nf = NamesFile(str(names))
    nf.load()
    nf.set_custom("a.1", "")
    tool.main(["--root", str(root), "--names", str(names), "--write"])
    assert os.path.exists(root / "Acc" / "Vacances (a.1)" / ".albumprops")
    tool.main(["--root", str(root), "--names", str(names), "--write", "--remove-stale"])
    assert not os.path.exists(root / "Acc" / "Vacances (a.1)" / ".albumprops")
    assert os.path.exists(root / "Acc" / "Foreign (a.3)" / ".albumprops")


def test_routes_refuse_cross_site_posts(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    root, names = _lib(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOADS_ROOT", str(root))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "none.log"))
    monkeypatch.setenv("ALBUMS_FILE", str(tmp_path / "none.txt"))
    monkeypatch.setenv("INTEGRITY", "0")
    import importlib
    import app.main as m
    m = importlib.reload(m)
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        assert c.get("/names").status_code == 200
        r = c.post("/names/a.2", data={"custom_name": "Evil"},
                   headers={"sec-fetch-site": "cross-site"}, follow_redirects=False)
        assert r.status_code == 403
        r = c.post("/names/a.2", data={"custom_name": "Réveillon"},
                   headers={"sec-fetch-site": "same-origin"}, follow_redirects=False)
        assert r.status_code == 303
        assert "Réveillon" in c.get("/names").text
        assert c.post("/integrity/rescan", headers={"origin": "https://evil.example"}).status_code == 403
