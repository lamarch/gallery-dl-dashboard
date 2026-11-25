"""Build a fake, realistic environment (log + albums.txt + downloads) for demos.

Usage: python3 tests/make_demo.py OUTDIR
Everything is invented. Timestamps end "now" so the header looks live.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakelog import FakeLog  # noqa: E402

def jpeg(seed):
    """A small real JPEG with a distinct blocky pattern per seed."""
    import io
    import random
    from PIL import Image
    rnd = random.Random(seed)
    im = Image.new("RGB", (320, 240))
    for x in range(0, 320, 40):
        for y in range(0, 240, 40):
            im.paste(tuple(rnd.randrange(256) for _ in range(3)), (x, y, x + 40, y + 40))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


ARCHIVE = []

ALBUMS = [  # set_id, account, title, photo count
    ("a.100", "Camille Dupont", "Vacances Bretagne 2019", 40),
    ("a.200", "Camille Dupont", "Été 🌞 Mariage de Léa", 60),
    ("a.300", "Алексей Иванов", "Новый год 2020", 80),
    ("a.400", "Алексей Иванов", "Дача", 30),
    ("a.500", "محمد", "ألبوم العائلة", 25),
    ("a.600", "Camille Dupont", "Profile pictures", 12),
]


def write_photo(root, account, title, sid, pid, date="20250506_082802",
                num=1, next_id="", first_id="", data=None):
    d = os.path.join(root, "facebook", account, f"{title} ({sid})")
    os.makedirs(d, exist_ok=True)
    name = f"{date}_{pid}.jpg"
    with open(os.path.join(d, name), "wb") as fh:
        fh.write(data if data is not None else jpeg(int(pid)))
    ARCHIVE.append((sid, pid))
    meta = {"id": pid, "set_id": sid, "username": account, "user_id": "1", "user_pfbid": "",
            "caption": "", "date": "2025-05-06 08:28:02", "url": "https://x.fbcdn.net/REDACTED",
            "next_photo_id": next_id, "filename": "x", "extension": "jpg", "followups_ids": [],
            "num": num, "title": title, "first_photo_id": first_id, "category": "facebook",
            "subcategory": "set"}
    with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)


def main(out):
    os.makedirs(os.path.join(out, "logs"), exist_ok=True)
    os.makedirs(os.path.join(out, "input"), exist_ok=True)
    pid = lambda sid, i: str(int(sid[2:]) * 10**8 + i)  # noqa: E731

    # durations are computed so that the log ends ~10 s before now
    now = datetime.now(timezone.utc).replace(microsecond=0)
    f = FakeLog((now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"))
    for _ in range(8):
        f.raw("[%(asctime)s][%(levelname)s] %(name)s: %(message)s")
    f.session_start()
    sid, acc, title, n = ALBUMS[0]
    f.job_start(sid)
    for i in range(1, n + 1):
        if i == 17:
            f.page(pid(sid, i)).no_url_retry(pid(sid, i)).page(pid(sid, i)).no_url_skip(pid(sid, i))
            continue
        f.photo(pid(sid, i))
        write_photo(out, acc, title, sid, pid(sid, i), num=i, next_id=pid(sid, i + 1) if i < n else '', first_id=pid(sid, 1))
    f.end_chain()
    sid, acc, title, n = ALBUMS[1]
    f.job_start(sid)
    for i in range(1, 21):
        f.photo(pid(sid, i))
        write_photo(out, acc, title, sid, pid(sid, i), num=i, next_id=pid(sid, i + 1) if i < n else '', first_id=pid(sid, 1))
    f.at(3 * 3600)  # power cut
    f.session_start()
    f.job_start(sid, archive=True)
    for i in range(1, 21):
        f.skipped(pid(sid, i))
    for i in range(21, n + 1):
        if i == 33:
            f.download_retry(1)
        f.photo(pid(sid, i))
        write_photo(out, acc, title, sid, pid(sid, i), num=i, next_id=pid(sid, i + 1) if i < n else '', first_id=pid(sid, 1))
    f.jump(pid(sid, n), "99999999999")
    sid, acc, title, n = ALBUMS[2]
    # current album: start so that we end near "now"
    f.job_start(sid, archive=True)
    k = n - 30
    for i in range(1, k + 1):
        f.photo(pid(sid, i))
        write_photo(out, acc, title, sid, pid(sid, i), num=i, next_id=pid(sid, i + 1) if i < n else '', first_id=pid(sid, 1))
    f.log("facebook", "debug", "Sleeping 14.20 seconds (request)", 1)
    shift = now.replace(tzinfo=None) - timedelta(seconds=6) - f.t
    lines = []
    for line in f.lines:   # shift every timestamp so the last line is 6 s ago
        if line.startswith("[20"):
            ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S") + shift
            line = f"[{ts:%Y-%m-%d %H:%M:%S}]" + line[21:]
        lines.append(line)
    with open(os.path.join(out, "logs", "fb-dl.log"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    # a re-uploaded duplicate: same image as a.100 #5, saved in a.200 under another ID
    write_photo(out, ALBUMS[1][1], ALBUMS[1][2], "a.200", pid("a.200", 61), num=61,
                first_id=pid("a.200", 1), data=jpeg(int(pid("a.100", 5))))
    import sqlite3
    con = sqlite3.connect(os.path.join(out, "logs", "fb-archive.sqlite3"))
    con.execute("CREATE TABLE IF NOT EXISTS archive (entry TEXT PRIMARY KEY) WITHOUT ROWID")
    con.executemany("INSERT OR IGNORE INTO archive VALUES (?)",
                    [(f"facebook{s_}_{p_}",) for s_, p_ in ARCHIVE])
    con.commit()
    con.close()
    with open(os.path.join(out, "input", "albums.txt"), "w", encoding="utf-8") as fh:
        fh.write("# account 1\n")
        for i, (sid, *_r) in enumerate(ALBUMS):
            url = f"https://www.facebook.com/media/set/?set={sid}&type=3"
            fh.write(("# " if i < 2 else "") + url + "\n")
    print(out)


if __name__ == "__main__":
    main(sys.argv[1])
