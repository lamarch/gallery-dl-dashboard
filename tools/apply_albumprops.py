#!/usr/bin/env python3
"""Write immich-folder-album-creator `.albumprops` files from album-names.tsv.

Gives EVERY album folder a name, WITHOUT renaming any folder (an Immich
external asset is identified by its path: renaming a folder creates new
assets and loses faces, albums and favorites). The name is, in order:
  1. the custom name chosen in the dashboard (Names tab),
  2. else the Facebook title (from an imported Facebook page),
  3. else the folder name without " (<set_id>)".
When several folders end up with the same name (the album creator would merge
them into one album), the account folder name is added: "Name (Account)", or
the album ID when they are in the same account.

Safety:
  * DRY RUN by default: prints the plan, writes nothing. --write to apply.
  * Only ever creates/replaces/removes files named `.albumprops`; never
    touches photos, JSON sidecars or folders.
  * A `.albumprops` not written by this tool (no marker line) is never
    modified.
  * Writes are atomic (temporary file + rename in the same folder).
  * Albums are found by the "(set_id)" at the end of the folder name.

Immich side: set READ_ALBUM_PROPERTIES=1 on immich-folder-album-creator.
Albums already created under the old name are not renamed by that tool (it
matches albums by name and would create a new one): rename the existing album
in Immich to the new name first, and the tool reuses it.

Standard library only.
"""
import argparse
import json
import os
import re
import sys

MARKER = "# managed by gallery-dl-dashboard"
SID_IN_DIR = re.compile(r"\(([^()]+)\)\s*$")
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


def clean(v):
    v = "".join(ch for ch in (v or "") if ch >= " " or ch == "\t")
    return " ".join(v.split())[:200]


def read_names(path):
    """{set_id: (custom_name, facebook_title)}"""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    header = lines[0].split("\t") if lines else []
    if "set_id" not in header or "custom_name" not in header:
        sys.exit(f"{path}: missing header (set_id ... custom_name)")
    idx = {c: header.index(c) for c in COLUMNS if c in header}
    names = {}
    for line in lines[1:]:
        if not line.strip() or line.startswith("#"):
            continue
        p = line.split("\t")
        get = lambda c: clean(unesc(p[idx[c]])) if c in idx and idx[c] < len(p) else ""  # noqa: E731
        if get("set_id"):
            names[get("set_id")] = (get("custom_name"), get("facebook_title"))
    return names


def folder_title(folder):
    return clean(SID_IN_DIR.sub("", os.path.basename(folder)))


def choose_names(folders, names):
    """{folder path: (name, source)} for every album folder; source is
    custom | facebook | folder, plus "+account" / "+id" when a duplicate
    name had to be told apart."""
    out = {}
    for sid, paths in folders.items():
        custom, fb = names.get(sid, ("", ""))
        for f in paths:
            if custom:
                out[f] = (custom, "custom", sid)
            elif fb:
                out[f] = (fb, "facebook", sid)
            else:
                out[f] = (folder_title(f) or sid, "folder", sid)
    # names the album creator would merge into one album
    for _ in range(2):
        by = {}
        for f, (n, src, sid) in out.items():
            by.setdefault(n.casefold(), []).append(f)
        for fs in by.values():
            if len(set(out[f][2] for f in fs)) < 2:
                continue                         # one album (possibly in two folders): fine
            accounts = {os.path.basename(os.path.dirname(f)) for f in fs}
            for f in fs:
                n, src, sid = out[f]
                if len(accounts) == len(fs) and "+account" not in src:
                    out[f] = (f"{n} ({os.path.basename(os.path.dirname(f))})", src + "+account", sid)
                elif "+id" not in src:
                    out[f] = (f"{n} ({sid})", src + "+id", sid)
    return {f: (n, src) for f, (n, src, _) in out.items()}


def content(name):
    return (f"{MARKER} (tools/apply_albumprops.py): album name for immich-folder-album-creator\n"
            f"override_name: {json.dumps(name, ensure_ascii=False)}\n")


def album_folders(root):
    out = {}
    for acc in sorted(os.listdir(root)):
        ap = os.path.join(root, acc)
        if not os.path.isdir(ap):
            continue
        for alb in sorted(os.listdir(ap)):
            p = os.path.join(ap, alb)
            m = SID_IN_DIR.search(alb)
            if m and os.path.isdir(p):
                out.setdefault(m.group(1), []).append(p)
    return out


def plan(root, names):
    actions = []      # (action, path, name, source)
    folders = album_folders(root)
    chosen = choose_names(folders, names)
    for folder in sorted(chosen):
        want, src = chosen[folder]
        p = os.path.join(folder, ".albumprops")
        exists = os.path.exists(p)
        cur = open(p, encoding="utf-8").read() if exists else None
        if exists and not cur.startswith(MARKER):
            actions.append(("skip", p, "a .albumprops not written by this tool exists: left untouched", src))
        elif cur == content(want):
            actions.append(("ok", p, want, src))
        else:
            actions.append(("write", p, want, src))
    for sid in sorted(set(s for s, (c, _) in names.items() if c) - set(folders)):
        actions.append(("missing", sid, "custom name for an album with no folder on disk", ""))
    return actions


def write_atomic(path, text):
    tmp = os.path.join(os.path.dirname(path), ".albumprops.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write .albumprops (override_name) from album-names.tsv. "
                                             "Dry run unless --write.")
    ap.add_argument("--root", required=True, help="e.g. /downloads/facebook")
    ap.add_argument("--names", required=True, help="e.g. /data/album-names.tsv")
    ap.add_argument("--write", action="store_true", help="apply the plan")
    args = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    actions = plan(args.root, read_names(args.names))
    counts, sources = {}, {}
    for act, path, detail, src in actions:
        counts[act] = counts.get(act, 0) + 1
        if src:
            sources[src] = sources.get(src, 0) + 1
        if act != "ok":
            print(f"{act:8} {path}  {detail}" + (f"  [{src}]" if src else ""))
    print("summary:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing to do")
    print(f"{sum(sources.values())} album folder(s), name taken from:",
          ", ".join(f"{k} {v}" for k, v in sorted(sources.items())) or "-")
    print("  custom = Names tab, facebook = imported Facebook title, folder = folder name; "
          "+account / +id = told apart from another album with the same name")
    if not args.write:
        print("DRY RUN: nothing written. Re-run with --write to apply.")
        return 0
    done = 0
    for act, path, detail, _ in actions:
        if act == "write":
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    if not fh.read().startswith(MARKER):   # re-check right before replacing
                        continue
            write_atomic(path, content(detail))
            done += 1
    print(f"applied: {done} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
