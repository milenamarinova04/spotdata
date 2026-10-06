"""
Spotify metadata extractor (Client Credentials flow, plain `requests`).

CLI:
    export SPOTIFY_CLIENT_ID=...  SPOTIFY_CLIENT_SECRET=...
    python spotify_metadata.py <url-or-uri> [more ...] -o out.csv|out.xlsx [--writers]

Notes
- Songwriter/composer credits are NOT exposed by the Spotify Web API. With
  --writers they are looked up on MusicBrainz via the track's ISRC (free, no key,
  rate-limited to ~1 req/s, coverage is incomplete).
- Batch endpoints (GET /tracks, /albums) were removed for Development Mode apps
  in 2026, so everything is fetched one ID at a time and cached.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Iterator

import pandas as pd
import requests

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
MB_API = "https://musicbrainz.org/ws/2"
MB_UA = "SpotifyMetadataExtractor/1.0 (replace-with-your-email@example.com)"

COLUMNS = [
    "Primary Artist", "Featured Artists", "Track Title", "Album",
    "Release Date", "Release Date Precision", "Release Year",
    "Copyright (C)", "Phonographic Copyright (P)",
    "ISRC", "UPC", "Writers / Composers", "Explicit", "Spotify URI",
]

_URL_RE = re.compile(
    r"^(?:https?://)?(?:open|play|embed)\.spotify\.com/(?:embed/)?(?:intl-[a-z-]+/)?"
    r"(?P<type>album|track|playlist)/(?P<id>[A-Za-z0-9]{22})(?:[/?#].*)?$"
)
_URI_RE = re.compile(r"^spotify:(?P<type>album|track|playlist):(?P<id>[A-Za-z0-9]{22})$")


class SpotifyError(Exception):
    """Any user-facing failure (bad input, auth, HTTP error)."""


def parse_spotify_link(link: str) -> tuple[str, str]:
    """Return (type, id) from a Spotify URL or URI, or raise SpotifyError."""
    link = (link or "").strip()
    m = _URI_RE.match(link) or _URL_RE.match(link)
    if not m:
        raise SpotifyError(
            f"Not a valid Spotify album/track/playlist URL or URI: {link!r}"
        )
    return m.group("type"), m.group("id")


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str):
        if not client_id or not client_secret:
            raise SpotifyError("Client ID and Client Secret are required.")
        self.client_id, self.client_secret = client_id, client_secret
        self.s = requests.Session()
        self._token: str | None = None
        self._expires = 0.0
        self._albums: dict[str, dict] = {}

    # ---- auth / transport -------------------------------------------------
    def _auth(self) -> str:
        if self._token and time.time() < self._expires - 30:
            return self._token
        r = requests.post(
            TOKEN_URL, data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret), timeout=15,
        )
        if r.status_code in (400, 401):
            raise SpotifyError("Authentication failed: check Client ID / Client Secret.")
        r.raise_for_status()
        j = r.json()
        self._token, self._expires = j["access_token"], time.time() + j.get("expires_in", 3600)
        return self._token

    def _get(self, path: str, params: dict | None = None, _retries: int = 4) -> dict:
        url = path if path.startswith("http") else f"{API}{path}"
        for attempt in range(_retries):
            try:
                r = self.s.get(url, params=params, timeout=20,
                               headers={"Authorization": f"Bearer {self._auth()}"})
            except requests.RequestException as e:
                raise SpotifyError(f"Network error: {e}") from e
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 2)) + 1)
                continue
            if r.status_code == 401:
                self._token = None
                continue
            if r.status_code == 404:
                raise SpotifyError("Not found (wrong ID, or the playlist is private/unavailable).")
            if r.status_code == 403:
                raise SpotifyError(f"Forbidden (403) for {url}. Playlist may be restricted for "
                                   "Development Mode apps, or the app owner lacks Premium.")
            if not r.ok:
                raise SpotifyError(f"Spotify API error {r.status_code}: {r.text[:200]}")
            return r.json()
        raise SpotifyError("Rate limited or token refresh kept failing; try again later.")

    def _paginate(self, first: dict) -> Iterator[dict]:
        page = first
        while page:
            yield from page.get("items") or []
            page = self._get(page["next"]) if page.get("next") else None

    # ---- resources --------------------------------------------------------
    def album(self, album_id: str) -> dict:
        if album_id not in self._albums:
            self._albums[album_id] = self._get(f"/albums/{album_id}")
        return self._albums[album_id]

    def track(self, track_id: str) -> dict:
        return self._get(f"/tracks/{track_id}")

    def playlist_meta(self, pid: str) -> dict:
        return self._get(f"/playlists/{pid}", {"fields": "name"})

    def playlist_tracks(self, pid: str) -> Iterator[dict]:
        # Feb-2026 API renamed /tracks -> /items and item.track -> item.item.
        try:
            first = self._get(f"/playlists/{pid}/items", {"limit": 50})
        except SpotifyError as e:
            if "Not found" not in str(e):
                raise
            first = self._get(f"/playlists/{pid}/tracks", {"limit": 50})
        for it in self._paginate(first):
            yield it.get("item") or it.get("track") or {}


# ---- MusicBrainz writer lookup (optional) ---------------------------------
_mb_cache: dict[str, str] = {}
_mb_last = 0.0


def _mb_get(path: str, params: dict) -> dict | None:
    global _mb_last
    wait = 1.1 - (time.time() - _mb_last)
    if wait > 0:
        time.sleep(wait)
    _mb_last = time.time()
    try:
        r = requests.get(f"{MB_API}{path}", params={**params, "fmt": "json"},
                         headers={"User-Agent": MB_UA}, timeout=20)
        return r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return None


def lookup_writers(isrc: str | None) -> str:
    if not isrc:
        return ""
    if isrc in _mb_cache:
        return _mb_cache[isrc]
    names: list[str] = []
    hit = _mb_get(f"/isrc/{isrc}", {})
    for rec in (hit or {}).get("recordings", [])[:1]:
        full = _mb_get(f"/recording/{rec['id']}", {"inc": "work-rels+work-level-rels"})
        for rel in (full or {}).get("relations", []):
            for r2 in (rel.get("work") or {}).get("relations", []):
                if r2.get("type") in ("composer", "lyricist", "writer"):
                    n = (r2.get("artist") or {}).get("name")
                    if n and n not in names:
                        names.append(n)
    _mb_cache[isrc] = "; ".join(names)
    return _mb_cache[isrc]


# ---- row building ----------------------------------------------------------
def _release(album: dict) -> tuple[str, str, str]:
    raw = (album or {}).get("release_date") or ""
    prec = (album or {}).get("release_date_precision") or ("day" if len(raw) == 10 else "")
    full = raw
    if len(raw) == 4:
        full = f"{raw}-01-01"
    elif len(raw) == 7:
        full = f"{raw}-01"
    return full, prec, raw[:4]


def build_row(client: SpotifyClient, track: dict, writers: bool = False) -> dict | None:
    tid = track.get("id")
    if not tid or track.get("type", "track") != "track":
        return None  # local file, episode or removed item

    ext = (track.get("external_ids") or {})
    if "isrc" not in ext:  # simplified track objects (from /albums) lack external_ids
        ext = client.track(tid).get("external_ids") or {}
    isrc = ext.get("isrc")

    album_ref = track.get("album") or {}
    album = client.album(album_ref["id"]) if album_ref.get("id") else {}
    artists = [a.get("name", "") for a in track.get("artists") or []]
    rel, prec, year = _release(album)
    cps = album.get("copyrights") or []

    def join(kind: str) -> str:
        return " | ".join(c.get("text", "") for c in cps if c.get("type") == kind)

    return {
        "Primary Artist": artists[0] if artists else "",
        "Featured Artists": "; ".join(artists[1:]),
        "Track Title": track.get("name", ""),
        "Album": album.get("name", ""),
        "Release Date": rel,
        "Release Date Precision": prec,
        "Release Year": int(year) if year.isdigit() else None,
        "Copyright (C)": join("C"),
        "Phonographic Copyright (P)": join("P"),
        "ISRC": isrc,
        "UPC": (album.get("external_ids") or {}).get("upc") or (album.get("external_ids") or {}).get("ean"),
        "Writers / Composers": lookup_writers(isrc) if writers else "",
        "Explicit": bool(track.get("explicit", False)),
        "Spotify URI": track.get("uri", ""),
    }


def extract(client: SpotifyClient, link: str, writers: bool = False,
            progress=None) -> tuple[pd.DataFrame, list[str]]:
    """Return (DataFrame, warnings) for one URL/URI."""
    kind, sid = parse_spotify_link(link)
    warnings: list[str] = []

    if kind == "track":
        tracks = [client.track(sid)]
    elif kind == "album":
        alb = client.album(sid)
        tracks = [dict(t, album={"id": sid}) for t in client._paginate(alb["tracks"])]
    else:
        tracks = list(client.playlist_tracks(sid))

    rows = []
    for i, t in enumerate(tracks, 1):
        try:
            row = build_row(client, t, writers)
        except SpotifyError as e:
            warnings.append(f"Track {t.get('name') or t.get('id')}: {e}")
            continue
        if row is None:
            warnings.append(f"Skipped item {i} (local file, podcast episode or unavailable).")
        else:
            rows.append(row)
            if not row["ISRC"]:
                warnings.append(f"No ISRC returned for '{row['Track Title']}'.")
        if progress:
            progress(i / max(len(tracks), 1))
    return pd.DataFrame(rows, columns=COLUMNS), warnings


# ---- CLI ---------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("links", nargs="+")
    ap.add_argument("-o", "--out", default="spotify_metadata.csv", help=".csv or .xlsx")
    ap.add_argument("--writers", action="store_true", help="look up writers via MusicBrainz (slow)")
    a = ap.parse_args()
    try:
        client = SpotifyClient(os.getenv("SPOTIFY_CLIENT_ID", ""), os.getenv("SPOTIFY_CLIENT_SECRET", ""))
        frames = []
        for link in a.links:
            try:
                df, warns = extract(client, link, a.writers)
                frames.append(df)
                for w in warns:
                    print(f"warning: {w}", file=sys.stderr)
            except SpotifyError as e:
                print(f"error ({link}): {e}", file=sys.stderr)
        if not frames:
            return 1
        out = pd.concat(frames, ignore_index=True)
        out.to_excel(a.out, index=False) if a.out.endswith(".xlsx") else out.to_csv(a.out, index=False, encoding="utf-8-sig")
        print(f"Wrote {len(out)} rows to {a.out}")
        return 0
    except SpotifyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
