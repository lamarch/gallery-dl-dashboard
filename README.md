# gallery-dl dashboard

A self-hosted, read-only web dashboard to supervise a long
[gallery-dl](https://github.com/mikf/gallery-dl) download of Facebook albums:
what it is doing right now, which albums are done, and above all **what may be
missing**.

![Dashboard on demo data](docs/screenshot.png)
*(demo data: every name and ID is invented)*

## What it shows

- **Live header**: downloading / pause before the next album (countdown) /
  rate-limited until hh:mm / blocked / login required / **stalled** (log quiet
  longer than any scheduled pause) / finished with exit code. Current account
  and album, album n/N, new / already-had / missing photos, ETA, downloaded
  size, disk usage and free space, whether the download archive is active.
- **Albums** in `albums.txt` order, each checked for:
  - photos gallery-dl skipped with *"Failed to find photo download URL …
    Skipping."* (the album is still marked done: the most likely silent gap),
  - albums stopped by gallery-dl's *"jump to the beginning"* heuristic,
  - albums marked done whose last pass did not reach the end of the album,
  - photos downloaded per the log but missing on disk, `.part` leftovers,
    media without JSON metadata.
- **Sessions** (one per gallery-dl start): result / exit code, interrupted
  runs (crash, power cut), time spent in each kind of sleep and 429 wait.
- **Events**: every warning and error, with tracebacks; **Diagnostics**: log
  lines the parser does not know yet, as privacy-safe templates.

## Privacy

- Read-only: the downloads are mounted `:ro`; nothing is sent anywhere, no
  external CDN, no telemetry.
- Signed image-server URLs (`*.fbcdn.net`, which carry tokens) are never
  stored; facebook.com links are reduced to photo and album IDs.
- Album names and photos are private: put a login in front (Caddy example
  below). The container publishes no port.

## Layout

```
web-dashboard/        the dashboard (FastAPI + Jinja, runs as an unprivileged uid)
  app/logparse.py     log parser: sessions -> albums -> photos, sleeps, 429s, errors
  app/status.py       live header (state, ETA, progress) + per-album checks
  app/disk.py         light read-only disk scan (names, counts, sizes, free space)
  app/albums.py       albums.txt reader (gallery-dl -I comments finished albums out)
  tests/              pytest suite, fake-log generator, demo builder
tools/seed_archive.py fill a gallery-dl download archive from files already on disk
deploy/               example compose.yml and gallery-dl.conf
```

Built against gallery-dl **1.32.21** with
`"output": {"logfile": {"level": "debug", "format": "[{asctime}] [{name}][{levelname}] {message}"}}`.

## gallery-dl setup notes

See `deploy/`. Three things matter:

1. **Mount the folder holding `albums.txt`, not the file.** `-I` rewrites the
   file (write + rename); a single-file bind mount keeps showing the old one.
2. **Use a download archive keyed on album + photo ID**:
   `"archive": "...sqlite3", "archive-format": "{set_id}_{id}"`.
   Facebook serves many JPEGs from `.avif` URLs; gallery-dl's "file exists"
   check uses the URL extension, misses the `.jpg` on disk and downloads the
   photo again after every interruption. `tools/seed_archive.py` fills the
   archive from existing files (dry run by default; only complete files with
   valid JSON are added).
3. **Run gallery-dl once per container start** (wrapper in `deploy/compose.yml`):
   it still restarts after a power cut, logs its exit code for the dashboard,
   and does not loop over the list forever.

## Deploy

```bash
docker compose up -d --build webui
```

Caddy:

```
fbdl.example.com {
    basic_auth {
        someuser <hash from: caddy hash-password>
    }
    reverse_proxy fb-dl-dashboard:8000
}
```

| Variable | Default | Meaning |
|---|---|---|
| `LOG_FILE` | `/downloads/logs/fb-dl.log` | gallery-dl `output.logfile.path` |
| `ALBUMS_FILE` | `/input/albums.txt` | the `-I` file (mount its folder) |
| `DOWNLOADS_ROOT` | `/downloads/facebook` | folder holding the account folders |
| `DISPLAY_TZ` | `TZ`, then UTC | timezone used to display times |
| `LOG_TZ` | `UTC` | timezone of the log timestamps (gallery-dl container time) |
| `BASE_PATH` | empty | e.g. `/fbdl` when served under a sub-path |
| `POLL_SECONDS` / `DISK_SCAN_SECONDS` | 2 / 60 | log polling / disk scan intervals |

## Development

```bash
pip install pytest fastapi uvicorn jinja2
python3 -m pytest -q web-dashboard/tests tools/tests
# archive tests against gallery-dl's own code:
GALLERY_DL_SRC=/path/to/gallery-dl-checkout python3 -m pytest -q tools/tests
# demo environment + local server:
python3 web-dashboard/tests/make_demo.py /tmp/demo
cd web-dashboard && LOG_FILE=/tmp/demo/logs/fb-dl.log ALBUMS_FILE=/tmp/demo/input/albums.txt \
  DOWNLOADS_ROOT=/tmp/demo/facebook DISK_PATH=/tmp/demo uvicorn app.main:app --port 8000
```
