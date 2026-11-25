"""Generate fake gallery-dl logs with the exact message formats of 1.32.21.

All IDs, names and hosts are invented.
"""
from datetime import datetime, timedelta


class FakeLog:
    def __init__(self, start="2026-11-22 18:40:12"):
        self.t = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        self.lines = []

    def at(self, seconds=0):
        self.t += timedelta(seconds=seconds)
        return self

    def raw(self, line):
        self.lines.append(line)
        return self

    def log(self, name, level, msg, dt=0):
        self.t += timedelta(seconds=dt)
        self.lines.append(f"[{self.t:%Y-%m-%d %H:%M:%S}] [{name}][{level}] {msg}")
        return self

    # ---- building blocks ----------------------------------------------------
    def session_start(self):
        self.log("gallery-dl", "debug", "Version 1.32.21")
        self.log("gallery-dl", "debug", "Python 3.14.10 - Linux-6.12-x86_64-with-musl1")
        self.log("gallery-dl", "debug", "requests 2.34.2 - urllib3 2.8.0 - certifi 2026.10.23")
        self.log("gallery-dl", "debug", "Configuration Files ['/config/gallery-dl.conf']")
        return self

    def job_start(self, set_id, sleep_extractor=3600, archive=None):
        url = f"https://www.facebook.com/media/set/?set={set_id}&type=3"
        self.log("gallery-dl", "debug", f"Starting DownloadJob for '{url}'")
        self.log("facebook", "debug", f"Using FacebookSetExtractor for '{url}'")
        self.log("facebook", "debug", "cookies: Loading cookies from '/config/fb-cookies.txt'")
        if sleep_extractor:
            self.log("facebook", "debug", f"Sleeping {sleep_extractor:.2f} seconds (extractor)")
            self.at(sleep_extractor)
        self.log("urllib3.connectionpool", "debug", "Starting new HTTPS connection (1): www.facebook.com:443")
        self.log("urllib3.connectionpool", "debug",
                 f'https://www.facebook.com:443 "GET /media/set/?set={set_id} HTTP/1.1" 200 None', 1)
        self._set = set_id
        self._first = True
        self._archive = archive
        return self

    def page(self, pid, status=200, sleep=12.5):
        self.log("facebook", "debug", f"Sleeping {sleep:.2f} seconds (request)", 1)
        self.at(sleep)
        self.log("urllib3.connectionpool", "debug",
                 f'https://www.facebook.com:443 "GET /photo/?fbid={pid}&set={self._set} HTTP/1.1" {status} None')
        if self._first:
            self._first = False
            if self._archive is True:
                self.log("facebook", "debug", "Using download archive '/downloads/logs/fb-archive.sqlite3'")
            elif self._archive is False:
                self.log("facebook", "warning", "Failed to open download archive at "
                         "'/downloads/logs/fb-archive.sqlite3' (OperationalError: unable to open database file)")
            self.log("facebook", "debug", "Active postprocessor modules: [MetadataPP]", 1)
        return self

    def download(self, pid, size=812345, avif=True, date="20250506_082802"):
        self.log("facebook", "debug", "Sleeping 7.52 seconds (download)")
        self.at(8)
        self.log("urllib3.connectionpool", "debug",
                 f'https://scontent-xxx1-1.xx.fbcdn.net:443 "GET /v/t39.30808-6/{pid}_n.jpg?oh=SECRET&oe=SECRET HTTP/1.1" 200 {size}')
        if avif:
            self.log("downloader.http", "debug",
                     f"Adjusting filename extension of '{date}_{pid}.avif' to 'jpg'")
        return self

    def photo(self, pid, **kw):
        return self.page(pid).download(pid, **kw)

    def skipped(self, pid):
        return self.page(pid)

    def no_url_retry(self, pid, seconds=900):
        url = f"https://www.facebook.com/photo/?fbid={pid}&set={self._set}"
        self.log("facebook", "warning",
                 f"Failed to find photo download URL for {url}. Retrying in {seconds:.1f} seconds.")
        mins = int(seconds / 60)
        until = self.t + timedelta(seconds=seconds + 1)
        self.log("facebook", "info", f"Waiting for {mins} minutes until {until:%H:%M:%S} (429 Too Many Requests)")
        self.at(seconds + 1)
        return self

    def no_url_skip(self, pid):
        url = f"https://www.facebook.com/photo/?fbid={pid}&set={self._set}"
        self.log("facebook", "error", f"Failed to find photo download URL for {url}. Skipping.")
        return self

    def end_chain(self):
        self.log("facebook", "debug", "Can't find next image in the set. Extraction is over.")
        return self

    def jump(self, a, b):
        self.log("facebook", "info", f"Detected jump to the beginning of the set. ({a} -> {b})")
        return self

    def blocked(self, pid):
        url = f"https://www.facebook.com/photo/?fbid={pid}&set={self._set}"
        self.log("facebook", "debug", "")
        self.raw("Traceback (most recent call last):")
        self.raw('  File "/usr/local/lib/python3.14/site-packages/gallery_dl/job.py", line 162, in run')
        self.raw("gallery_dl.exception.AbortExtraction: You've been temporarily blocked")
        self.log("facebook", "error",
                 "You've been temporarily blocked from viewing images.")
        self.raw("Please try using a different account, using a VPN or waiting before you retry.")
        self.raw("You can use this URL to continue from where you left off (added \"&setextract\"): ")
        self.raw(url + "&setextract")
        return self

    def download_retry(self, n=1):
        self.log("downloader.http", "warning",
                 "('Connection aborted.', RemoteDisconnected('Remote end closed connection "
                 f"without response')) ({n}/5)")
        return self

    def wrapper_exit(self, code):
        self.log("wrapper", "info", f"gallery-dl exited with code {code}")
        return self

    def text(self):
        return "\n".join(self.lines) + "\n"
