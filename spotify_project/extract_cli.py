"""
🎧 Spotify Data Extractor — Terminal / CLI version
Run from the command line, no Streamlit UI needed:

    python extract_cli.py "https://open.spotify.com/artist/XXXX"
    python extract_cli.py "https://open.spotify.com/album/XXXX" --no-credits
    python extract_cli.py "https://open.spotify.com/track/XXXX" -o my_output.pdf

Credentials are read from .streamlit/secrets.toml (same file the Streamlit app
uses) or from environment variables SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET / SP_DC.

The PDF helper (`save_pdf`) is importable, so the Streamlit app can reuse it.
"""

import os
import re
import sys
import json
import time
import random
import argparse
from collections import deque

import requests
from spotipy.oauth2 import SpotifyClientCredentials
import spotipy

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

def get_config():
    """Read credentials from .streamlit/secrets.toml or environment variables."""
    secrets_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml"
    )
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
    sp_dc = os.getenv("SP_DC", "")

    if os.path.exists(secrets_path):
        try:
            with open(secrets_path, "rb") as f:
                data = tomllib.load(f)
            client_id = data.get("SPOTIFY_CLIENT_ID", client_id)
            client_secret = data.get("SPOTIFY_CLIENT_SECRET", client_secret)
            sp_dc = data.get("SP_DC", sp_dc)
        except Exception as e:
            print(f"⚠️  Could not read secrets.toml: {e}")

    return client_id, client_secret, sp_dc


# ─────────────────────────────────────────────
# RATE-LIMIT-AWARE HTTP (handles the ~30s rolling window / HTTP 429)
# ─────────────────────────────────────────────

class _CreditsRateLimiter:
    """Serial pacing for the credits endpoint to avoid tripping the rolling
    ~30s window (which leads to 403 / multi-hour cooldowns).

    Guarantees a minimum gap between every call and adds a little random
    jitter so requests look steady rather than bursty. Keep it single-threaded
    (one request at a time) — do NOT call this from threads/asyncio.
    """

    def __init__(self, min_interval=1.5, jitter=0.4):
        self.min_interval = min_interval
        self.jitter = jitter
        self._last = 0.0

    def wait(self):
        now = time.monotonic()
        gap = self.min_interval + random.uniform(0, self.jitter)
        elapsed = now - self._last
        if self._last and elapsed < gap:
            time.sleep(gap - elapsed)
        self._last = time.monotonic()


class _RollingWindowLimiter:
    """True token-bucket over a real rolling time window.

    Guarantees no more than `max_calls` requests in ANY `window` seconds,
    shared across the WHOLE run (metadata + credits). This is what a bare
    min-interval limiter cannot do: it enforces a hard per-window budget, so
    the metadata phase can't silently exhaust the window before credits start.

    Single-threaded / serial by design — one request at a time.
    """

    def __init__(self, max_calls=15, window=30.0, jitter=0.3):
        self.max_calls = max_calls
        self.window = window
        self.jitter = jitter
        self._times = deque()

    def acquire(self):
        while True:
            now = time.monotonic()
            # Drop timestamps that have aged out of the rolling window.
            while self._times and now - self._times[0] >= self.window:
                self._times.popleft()
            if len(self._times) < self.max_calls:
                self._times.append(now)
                if self.jitter:
                    time.sleep(random.uniform(0, self.jitter))  # de-align bursts
                return
            # Budget full → wait until the oldest call exits the window.
            sleep_for = self.window - (now - self._times[0]) + 0.05
            time.sleep(max(sleep_for, 0.0))


# One shared budget for the entire run. Every request goes through safe_get,
# so this single limiter coordinates metadata AND credits against one window.
_RATE = _RollingWindowLimiter(max_calls=15, window=30.0, jitter=0.3)


def safe_get(url, headers, params=None, max_retries=5, throttle=0.2, max_wait=30):
    """GET that honors Spotify's Retry-After header and proactively throttles.

    Spotify enforces a rolling ~30-second rate-limit window. On 429 we wait the
    Retry-After seconds and retry (looped, with backoff) instead of giving up.

    If the API returns a very large Retry-After (a long cooldown/penalty, e.g.
    several hours), we do NOT block for that long — we abort with a clear
    message so the process doesn't hang. In that case, wait it out and retry
    later; the app has been temporarily rate-limited server-side.

    On HTTP 403 (the credits endpoint flagging the cookie), we stop retrying
    immediately and return the response so the caller can halt gracefully —
    continuing would only deepen the server-side penalty.
    """
    resp = None
    for attempt in range(max_retries):
        _RATE.acquire()  # shared rolling-window budget across the whole run
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
        except requests.exceptions.RequestException as exc:
            # Transient network error (timeout / connection reset). Do NOT crash
            # the whole run — back off briefly and retry.
            wait = min(2 ** attempt, max_wait)
            print(f"   🌐 Network error ({type(exc).__name__}); retrying in {wait}s "
                  f"(attempt {attempt + 1}/{max_retries})...")
            time.sleep(wait)
            continue
        if resp.status_code == 403:
            # Cookie/endpoint flagged — do not keep hammering; let caller stop.
            return resp
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None:
                wait = int(retry_after) + 1  # honor Retry-After (+1s safety margin)
            else:
                wait = min(2 ** attempt, max_wait)  # exponential backoff fallback

            if wait > max_wait:
                mins = wait // 60
                print(f"\n🛑 Spotify applied a long cooldown (Retry-After = {wait}s ≈ {mins} min).")
                print("   This is a server-side penalty from too many requests. "
                      "Not waiting that long — please try again later.")
                return resp  # abort; caller handles empty result gracefully

            print(f"   ⏳ Rate limited (429). Waiting {wait}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(wait)
            continue
        time.sleep(throttle)  # proactive spacing so we never saturate the window
        return resp
    return resp


# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────

def get_spotify_client():
    client_id, client_secret, _ = get_config()
    if not client_id or not client_secret:
        print("❌ Missing Spotify credentials. Set them in .streamlit/secrets.toml or env vars.")
        sys.exit(1)
    auth_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
    sp = spotipy.Spotify(auth_manager=auth_manager, requests_timeout=10)
    token = auth_manager.get_access_token(as_dict=False)
    return sp, token


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def parse_spotify_url(url):
    patterns = [
        r'open\.spotify\.com/(playlist|album|track|artist)/([a-zA-Z0-9]+)',
        r'spotify:(playlist|album|track|artist):([a-zA-Z0-9]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1), match.group(2)
    return None, None


def ms_to_min_sec(ms):
    minutes = ms // 60000
    seconds = (ms % 60000) // 1000
    return f"{minutes}:{seconds:02d}"


# ─────────────────────────────────────────────
# CREDITS (web-player token via sp_dc cookie)
# ─────────────────────────────────────────────

# Persistent on-disk cache of credits keyed by track_id. Credits never change,
# so once fetched we never hit the (rate-limited) credits endpoint for that
# track again — this is the single biggest protection against re-triggering a
# cooldown on repeated runs / overlapping catalogs.
CREDITS_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".credits_cache.json"
)


def load_credits_cache():
    try:
        with open(CREDITS_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def save_credits_cache(cache):
    try:
        with open(CREDITS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError:
        pass


# Persistent cache of the extracted TRACK LIST (metadata) keyed by
# "<type>:<id>" (e.g. "artist:53Gkg..."). Collecting a large artist's catalog
# costs ~150 main-API calls; caching it means we do that ONCE and every later
# run (e.g. to finish credits) reads track_ids from disk and makes ZERO
# main-API calls. This is the reliable way to never re-trigger the
# api.spotify.com "too many requests" cooldown from repeated scans.
METADATA_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".metadata_cache.json"
)


def load_metadata_cache():
    try:
        with open(METADATA_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def save_metadata_cache(cache):
    try:
        with open(METADATA_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError:
        pass


def fetch_tracklist(link_type, link_id, headers, api_base, use_cache=True):
    """Return (songs, name) for any link type, using a persistent metadata
    cache so the expensive main-API scan runs only ONCE per artist/album/etc.

    Pass use_cache=False (CLI --refresh) to force a fresh scan. Only a
    successful, non-empty scan is cached — a cooldown-empty result is never
    persisted, so it will be retried next time.
    """
    key = f"{link_type}:{link_id}"
    cache = load_metadata_cache()
    if use_cache and key in cache and cache[key].get("songs"):
        entry = cache[key]
        print(f"   ♻️  Loaded {len(entry['songs'])} tracks from metadata cache "
              "(no main-API calls).")
        return entry["songs"], entry.get("name", "Unknown")

    if link_type == "artist":
        songs, name = fetch_artist_songs(headers, api_base, link_id)
    elif link_type == "album":
        songs, name = fetch_album_songs(headers, api_base, link_id)
    elif link_type == "playlist":
        songs, name = fetch_playlist_songs(headers, api_base, link_id)
    else:  # track
        songs, name = fetch_single_track(headers, api_base, link_id)

    if songs:  # never cache an empty/cooldown result
        cache[key] = {"name": name, "songs": songs}
        save_metadata_cache(cache)
    return songs, name


def get_web_player_token(retries=4):
    _, _, sp_dc = get_config()
    if not sp_dc:
        return None
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    # Retry a few times so a single transient blip (e.g. a brief anti-bot
    # challenge right after a burst of requests) doesn't skip ALL credits.
    for attempt in range(retries):
        # Method 1: Direct endpoint (legacy — often blocked/deprecated by Spotify)
        try:
            resp = requests.get(
                "https://open.spotify.com/get_access_token?reason=transport&productType=web_player",
                headers={"User-Agent": ua, "Cookie": f"sp_dc={sp_dc}"}, timeout=10)
            if resp.status_code == 200:
                try:
                    token = resp.json().get("accessToken")
                    if token:
                        return token
                except (json.JSONDecodeError, ValueError):
                    pass
        except Exception:
            pass

        # Method 2: Authenticated embed page (current working fallback)
        try:
            session = requests.Session()
            session.headers.update({"User-Agent": ua})
            session.cookies.set("sp_dc", sp_dc, domain="open.spotify.com", path="/")
            resp = session.get(
                "https://open.spotify.com/embed/track/4iV5W9uYEdYUVa79Axb7Rh", timeout=10)
            if resp.status_code == 200:
                match = re.search(r'"accessToken":"([^"]+)"', resp.text)
                if match:
                    return match.group(1)
        except Exception:
            pass

        # Backoff before the next attempt (transient throttle usually clears fast)
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))

    return None


def fetch_track_credits(track_id, web_token, limiter=None):
    """Fetch all credits for a track.

    Returns (credits_string, status) where status is one of:
      * "ok"        — a real 200 response (credits_string may be "" if the
                       track genuinely has no listed credits). SAFE TO CACHE.
      * "throttled" — request failed (429 exhausted / network). DO NOT CACHE;
                       leave blank so a later run retries it.
      * "cooldown"  — HTTP 403: the endpoint flagged us. Caller should STOP
                       making further credits calls to avoid a long penalty.
    """
    if not web_token:
        return "", "throttled"
    headers = {
        "Authorization": f"Bearer {web_token}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Content-type": "application/json",
        "app-platform": "WebPlayer",
    }
    url = f"https://spclient.wg.spotify.com/track-credits-view/v0/experimental/{track_id}/credits"

    # Pace serially (fixed interval + jitter) before hitting the endpoint.
    if limiter is not None:
        limiter.wait()

    resp = safe_get(url, headers)
    if resp is None:
        return "", "throttled"  # network failed after retries
    if resp.status_code == 403:
        return "", "cooldown"  # flagged — signal caller to stop
    if resp.status_code == 200:
        data = resp.json()
        if data.get('trackTitle', '') == '':
            return "", "ok"  # genuine: track has no listed credits
        parts = []
        for rc in data.get("roleCredits", []):
            artists = [a.get("name", "") for a in rc.get("artists", []) if a.get("name")]
            if artists:
                parts.append(f"{rc.get('roleTitle', '')}: {', '.join(artists)}")
        sources = data.get("sourceNames", [])
        if sources:
            parts.append(f"Source: {', '.join(sources)}")
        return " | ".join(parts), "ok"
    # 429 exhausted or any other non-200 → transient; retry on a later run.
    return "", "throttled"


# ─────────────────────────────────────────────
# EXTRACTION (batched to minimize request count)
# ─────────────────────────────────────────────

def fetch_artist_songs(headers, api_base, artist_id):
    resp = safe_get(f"{api_base}/artists/{artist_id}", headers)
    artist_name = resp.json().get("name", "Unknown") if resp and resp.status_code == 200 else "Unknown"

    # Collect albums (paginated). NOTE: Spotify reduced the max page size for
    # this endpoint to 10 (limit >= 11 now returns HTTP 400 "Invalid limit"),
    # so we page at 10. The shared rolling-window limiter in safe_get keeps the
    # extra pagination calls from tripping any rate-limit cooldown.
    all_albums, offset = [], 0
    while True:
        resp = safe_get(
            f"{api_base}/artists/{artist_id}/albums", headers,
            params={"include_groups": "album,single,compilation", "limit": 10, "offset": offset},
        )
        if resp is None or resp.status_code != 200:
            break
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        all_albums.extend(items)
        if not data.get("next"):
            break
        offset += 10

    # De-dupe album ids, then fetch each album individually via /albums/{id}.
    # The batch endpoint (/albums?ids=...) now returns HTTP 403 Forbidden for
    # client-credentials tokens, so we fall back to one request per album. The
    # rolling-window limiter paces these safely.
    album_ids, seen_albums = [], set()
    for alb in all_albums:
        aid = alb.get("id")
        if aid and aid not in seen_albums:
            seen_albums.add(aid)
            album_ids.append(aid)

    all_songs, seen = [], set()
    total = len(album_ids)
    for idx, aid in enumerate(album_ids):
        print(f"   📀 Scanning album {idx + 1}/{total}", end="\r")
        resp = safe_get(f"{api_base}/albums/{aid}", headers)
        if resp is None or resp.status_code != 200:
            continue
        album_data = resp.json()
        album_name = album_data.get("name", "Unknown")
        release_date = album_data.get("release_date", "N/A")
        tracks_obj = album_data.get("tracks", {})
        track_items = list(tracks_obj.get("items", []))
        # Follow track pagination for albums with >50 tracks (rare).
        next_url = tracks_obj.get("next")
        while next_url:
            r2 = safe_get(next_url, headers)
            if r2 is None or r2.status_code != 200:
                break
            td = r2.json()
            track_items.extend(td.get("items", []))
            next_url = td.get("next")
        for track in track_items:
            key = f"{track['name'].lower()}_{track['duration_ms']}"
            if key in seen:
                continue
            seen.add(key)
            all_songs.append({
                "song_title": track["name"],
                "artists": ", ".join(a["name"] for a in track["artists"]),
                "album": album_name,
                "track_number": track.get("track_number", ""),
                "duration": ms_to_min_sec(track["duration_ms"]),
                "explicit": track.get("explicit", False),
                "release_date": release_date,
                "track_id": track["id"],
            })
    print()
    all_songs.sort(key=lambda x: x.get("release_date", ""), reverse=True)
    return all_songs, artist_name


def fetch_album_songs(headers, api_base, album_id):
    resp = safe_get(f"{api_base}/albums/{album_id}", headers)
    if resp is None or resp.status_code != 200:
        return [], "Unknown"
    album_data = resp.json()
    album_name = album_data.get("name", "Unknown")
    release_date = album_data.get("release_date", "N/A")
    artist_name = ", ".join(a["name"] for a in album_data.get("artists", []))
    songs = []
    for track in album_data.get("tracks", {}).get("items", []):
        songs.append({
            "song_title": track["name"],
            "artists": ", ".join(a["name"] for a in track["artists"]),
            "album": album_name,
            "track_number": track.get("track_number", ""),
            "duration": ms_to_min_sec(track["duration_ms"]),
            "explicit": track.get("explicit", False),
            "release_date": release_date,
            "track_id": track["id"],
        })
    return songs, f"{album_name} — {artist_name}"


def fetch_playlist_songs(headers, api_base, playlist_id):
    resp = safe_get(f"{api_base}/playlists/{playlist_id}", headers)
    if resp is None or resp.status_code != 200:
        return [], "Unknown"
    playlist_data = resp.json()
    playlist_name = playlist_data.get("name", "Unknown")
    songs = []
    tracks_data = playlist_data.get("tracks", {})
    while True:
        for item in tracks_data.get("items", []):
            track = item.get("track")
            if not track or not track.get("id"):
                continue
            album = track.get("album", {})
            songs.append({
                "song_title": track.get("name", ""),
                "artists": ", ".join(a["name"] for a in track.get("artists", [])),
                "album": album.get("name", ""),
                "track_number": track.get("track_number", ""),
                "duration": ms_to_min_sec(track.get("duration_ms", 0)),
                "explicit": track.get("explicit", False),
                "release_date": album.get("release_date", "N/A"),
                "track_id": track["id"],
            })
        next_url = tracks_data.get("next")
        if not next_url:
            break
        resp = safe_get(next_url, headers)
        if resp is None or resp.status_code != 200:
            break
        tracks_data = resp.json()
    return songs, playlist_name


def fetch_single_track(headers, api_base, track_id):
    resp = safe_get(f"{api_base}/tracks/{track_id}", headers)
    if resp is None or resp.status_code != 200:
        return [], "Unknown"
    track = resp.json()
    album = track.get("album", {})
    song = {
        "song_title": track.get("name", ""),
        "artists": ", ".join(a["name"] for a in track.get("artists", [])),
        "album": album.get("name", ""),
        "track_number": track.get("track_number", ""),
        "duration": ms_to_min_sec(track.get("duration_ms", 0)),
        "explicit": track.get("explicit", False),
        "release_date": album.get("release_date", "N/A"),
        "track_id": track["id"],
    }
    return [song], track.get("name", "track")


# ─────────────────────────────────────────────
# PDF EXPORT (importable — reusable in Streamlit)
# ─────────────────────────────────────────────

def save_pdf(rows, title, output_path):
    """Render the extracted rows into a nicely formatted PDF table."""
    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=7, leading=9)
    head = ParagraphStyle("head", parent=styles["BodyText"], fontSize=7.5,
                           leading=9, textColor=colors.white, fontName="Helvetica-Bold")

    doc = SimpleDocTemplate(
        output_path, pagesize=landscape(A4),
        leftMargin=10 * mm, rightMargin=10 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
    )

    columns = ["song_title", "artists", "album", "duration", "release_date", "credits"]
    columns = [c for c in columns if any(c in r for r in rows)]
    headers_display = [c.replace("_", " ").title() for c in columns]

    table_data = [[Paragraph(h, head) for h in headers_display]]
    for r in rows:
        table_data.append([Paragraph(str(r.get(c, "")), cell) for c in columns])

    page_width = landscape(A4)[0] - 20 * mm
    weights = {"song_title": 2.2, "artists": 2.2, "album": 2.2,
               "duration": 0.8, "release_date": 1.2, "credits": 3.4}
    total = sum(weights.get(c, 1) for c in columns)
    col_widths = [page_width * weights.get(c, 1) / total for c in columns]

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1DB954")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    elements = [
        Paragraph(f"<b>{title}</b>", styles["Title"]),
        Paragraph(f"{len(rows)} tracks", styles["Normal"]),
        Spacer(1, 6 * mm),
        table,
    ]
    doc.build(elements)
    return output_path


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run(url, include_credits=True, output=None, refresh=False):
    link_type, link_id = parse_spotify_url(url)
    if not link_type:
        print("❌ Invalid Spotify URL. Provide an artist, album, track, or playlist link.")
        sys.exit(1)

    print(f"🔍 Detected: {link_type.upper()}")
    _, token = get_spotify_client()
    headers = {"Authorization": f"Bearer {token}"}
    api_base = "https://api.spotify.com/v1"

    print("📥 Fetching data from Spotify...")
    # Uses the on-disk metadata cache: the expensive catalog scan happens once,
    # then later runs read track_ids from disk (zero main-API calls).
    data, name = fetch_tracklist(link_type, link_id, headers, api_base,
                                 use_cache=not refresh)

    if not data:
        print("❌ No tracks found. The URL may be invalid or inaccessible.")
        sys.exit(1)

    print(f"✅ Found {len(data)} tracks from '{name}'")

    if include_credits:
        web_token = get_web_player_token()
        if web_token:
            print("📝 Fetching credits...")
            limiter = _CreditsRateLimiter(min_interval=1.5, jitter=0.4)
            cache = load_credits_cache()
            hit_cooldown = False
            fetched_since_save = 0
            consecutive_throttled = 0
            for i, song in enumerate(data):
                tid = song["track_id"]
                # Cache hit → no network call at all (credits never change).
                if tid in cache:
                    song["credits"] = cache[tid]
                    print(f"   Credits {i + 1}/{len(data)} (cached)", end="\r")
                    continue
                if hit_cooldown:
                    song["credits"] = ""
                    continue
                song["credits"], status = fetch_track_credits(
                    tid, web_token, limiter
                )
                print(f"   Credits {i + 1}/{len(data)}", end="\r")
                if status == "cooldown":
                    hit_cooldown = True
                    print("\n⏸ Spotify flagged the credits endpoint (403). Stopped early "
                          "to avoid a multi-hour cooldown. Remaining tracks have no "
                          "credits — try again later (cached progress is saved).")
                    continue
                if status == "throttled":
                    # Couldn't fetch right now (429/network). Do NOT cache — leave
                    # blank so a later run retries it. If we're being throttled
                    # repeatedly, pause longer to let the rolling window drain.
                    song["credits"] = ""
                    consecutive_throttled += 1
                    if consecutive_throttled >= 5:
                        print("\n⏳ Repeated throttling — pausing 60s to let the rate "
                              "limit reset (cached progress is saved)...")
                        save_credits_cache(cache)
                        time.sleep(60)
                        consecutive_throttled = 0
                    continue
                # status == "ok" → real response, safe to cache (even if empty).
                consecutive_throttled = 0
                cache[tid] = song["credits"]
                fetched_since_save += 1
                if fetched_since_save >= 20:
                    save_credits_cache(cache)
                    fetched_since_save = 0
                # Longer breather every 25 tracks to let the rolling window drain.
                if i < len(data) - 1 and (i + 1) % 25 == 0:
                    time.sleep(random.uniform(15, 20))
            save_credits_cache(cache)
            print()
        else:
            print("⚠️  No SP_DC token — skipping credits.")
            for song in data:
                song["credits"] = ""
    else:
        for song in data:
            song["credits"] = ""

    if not output:
        safe_name = re.sub(r'[^a-zA-Z0-9]', '_', name)[:30]
        output = f"{link_type}_{safe_name}.pdf"

    save_pdf(data, name, output)
    print(f"📄 Saved PDF → {os.path.abspath(output)}")
    return output


def main():
    parser = argparse.ArgumentParser(description="Spotify Data Extractor (terminal → PDF)")
    parser.add_argument("url", help="Spotify URL (artist / album / track / playlist)")
    parser.add_argument("-o", "--output", help="Output PDF path")
    parser.add_argument("--no-credits", action="store_true", help="Skip fetching credits (faster)")
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore the cached track list and re-scan metadata from the API")
    args = parser.parse_args()
    run(args.url, include_credits=not args.no_credits, output=args.output, refresh=args.refresh)


if __name__ == "__main__":
    main()

