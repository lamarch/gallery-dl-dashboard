#!/usr/bin/env python3
"""Rename existing Immich albums to the names chosen in album-names.tsv.

immich-folder-album-creator finds albums BY NAME: once .albumprops gives an
album a new name (override_name), the tool would create a second album
instead of reusing the old one. Renaming the old album first keeps it, with
everything attached to it (description, sharing, cover, comments); the photos
themselves are never touched either way.

Safety:
  * DRY RUN by default: prints the plan. --write to rename.
  * Only renames (PATCH /api/albums/<id> {"albumName": ...}); never deletes,
    never adds or removes photos.
  * An album is matched only when its name is the folder name (or the folder
    name without " (a.<id>)") AND its cover photo lives in that folder.
  * Skips when several albums match, when the cover cannot be checked, or
    when an album with the new name already exists (shown as a conflict).

The API key is read from the IMMICH_API_KEY environment variable (never on
the command line, so it does not end up in your shell history). It needs the
album.read, album.update and asset.read permissions.

Usage:
  IMMICH_API_KEY=... python3 rename_immich_albums.py --url https://immich.example \\
      --names dashboard-data/album-names.tsv [--write]
Standard library only.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

COLUMNS = ("set_id", "folder", "gallerydl_title", "facebook_title", "custom_name")


def unesc(v):
    out, i = [], 0
    while i < len(v):
        if v[i] == "\\" and i + 1 < len(v):
            out.append({"t": "\t", "n": "\n", "\\": "\\"}.get(v[i + 1], "\\" + v[i + 1]))
            i += 2
        else:
            out.append(v[i])
            i += 1
    return "".join(out)


def read_names(path):
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    header = lines[0].split("\t")
    idx = {c: header.index(c) for c in COLUMNS if c in header}
    if "set_id" not in idx or "custom_name" not in idx or "folder" not in idx:
        sys.exit(f"{path}: missing columns (set_id, folder, custom_name)")
    rows = []
    for line in lines[1:]:
        if not line.strip() or line.startswith("#"):
            continue
        p = line.split("\t")
        get = lambda c: unesc(p[idx[c]]).strip() if c in idx and idx[c] < len(p) else ""  # noqa: E731
        if get("set_id") and get("custom_name"):
            rows.append({"set_id": get("set_id"), "folder": get("folder"), "new": get("custom_name")})
    return rows


class Immich:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/api"
        self.key = key

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"x-api-key": self.key, "Accept": "application/json",
                                              **({"Content-Type": "application/json"} if data else {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None


def plan(api, rows):
    albums = api.call("GET", "/albums")
    by_name = {}
    for a in albums:
        by_name.setdefault(a.get("albumName"), []).append(a)
    actions = []   # (action, set_id, detail, album_id, new_name)
    cover_cache = {}
    for r in rows:
        folder = r["folder"]
        bare = re.sub(r"\s*\([^()]+\)\s*$", "", folder)
        if r["new"] in (folder, bare):
            continue
        cands = [a for n in dict.fromkeys((folder, bare)) for a in by_name.get(n, [])]
        if not cands:
            if by_name.get(r["new"]):
                actions.append(("ok", r["set_id"], f"already named “{r['new']}”", None, None))
            else:
                actions.append(("none", r["set_id"], f"no album named “{folder}” or “{bare}”", None, None))
            continue
        if len(cands) > 1:
            actions.append(("skip", r["set_id"], f"{len(cands)} albums are named “{cands[0]['albumName']}”", None, None))
            continue
        a = cands[0]
        thumb = a.get("albumThumbnailAssetId")
        path = None
        if thumb:
            if thumb not in cover_cache:
                try:
                    cover_cache[thumb] = (api.call("GET", f"/assets/{thumb}") or {}).get("originalPath")
                except urllib.error.HTTPError:
                    cover_cache[thumb] = None
            path = cover_cache[thumb]
        if not path or f"({r['set_id']})" not in path.replace("\\", "/"):
            actions.append(("skip", r["set_id"], f"cannot confirm that album “{a['albumName']}” is this folder "
                                                  f"(its cover is {'elsewhere' if path else 'unknown'})", None, None))
            continue
        if by_name.get(r["new"]):
            actions.append(("conflict", r["set_id"], f"an album named “{r['new']}” already exists "
                                                     f"(created by the album creator?): rename skipped", None, None))
            continue
        actions.append(("rename", r["set_id"], f"“{a['albumName']}” → “{r['new']}”", a["id"], r["new"]))
        by_name.setdefault(r["new"], []).append(a)          # two folders can't take the same new name
    return actions


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rename Immich albums to the names in album-names.tsv "
                                             "(dry run unless --write).")
    ap.add_argument("--url", default=os.environ.get("IMMICH_URL"), help="Immich URL (or IMMICH_URL)")
    ap.add_argument("--names", required=True, help="album-names.tsv from the dashboard data folder")
    ap.add_argument("--write", action="store_true", help="really rename")
    args = ap.parse_args(argv)
    key = os.environ.get("IMMICH_API_KEY")
    if not args.url or not key:
        sys.exit("set --url (or IMMICH_URL) and the IMMICH_API_KEY environment variable")
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    api = Immich(args.url, key)
    try:
        actions = plan(api, read_names(args.names))
    except urllib.error.HTTPError as e:
        sys.exit(f"Immich answered {e.code}: check the URL and the API key permissions")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach Immich: {e.reason}")
    counts = {}
    for act, sid, detail, _, _ in actions:
        counts[act] = counts.get(act, 0) + 1
        print(f"{act:9} {sid:24} {detail}")
    print("summary:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing to do")
    if not args.write:
        print("DRY RUN: nothing renamed. Re-run with --write to apply.")
        return 0
    done = 0
    for act, sid, detail, album_id, new in actions:
        if act == "rename":
            api.call("PATCH", f"/albums/{album_id}", {"albumName": new})
            done += 1
    print(f"renamed: {done} album(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
