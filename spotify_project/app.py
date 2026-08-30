"""
🎧 Spotify Data Extractor — Streamlit Web App
Paste any Spotify URL → get a CSV with full credits.
No login needed. Fully automated.
"""

import os
import re
import json
import time
import random
import requests
from collections import deque
import streamlit as st
import pandas as pd
from spotipy.oauth2 import SpotifyClientCredentials
import spotipy

# ISRC Finder (Soundcharts) — separate functionality reused in this app.
# Importing is side-effect free (no network calls at import time).
from isrc import read_input, save_isrc_pdf, save_isrc_csv
from isrc_soundcharts import SoundchartsClient, process as sc_process, drop_header_like

# ─────────────────────────────────────────────
# CONFIG (from environment / secrets)
# ─────────────────────────────────────────────

def get_config():
    """Get credentials from Streamlit secrets or environment."""
    # Streamlit Cloud uses st.secrets; local uses env vars
    try:
        client_id = st.secrets["SPOTIFY_CLIENT_ID"]
        client_secret = st.secrets["SPOTIFY_CLIENT_SECRET"]
        sp_dc = st.secrets.get("SP_DC", "")
    except Exception:
        client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
        sp_dc = os.getenv("SP_DC", "")
    return client_id, client_secret, sp_dc


# ─────────────────────────────────────────────
# SPOTIFY AUTH (fully automatic)
# ─────────────────────────────────────────────

@st.cache_resource(ttl=3500)
def get_spotify_client():
    """Auto-authenticate with Spotify (Client Credentials — no browser needed)."""
    client_id, client_secret, _ = get_config()
    if not client_id or not client_secret:
        st.error("❌ Missing Spotify credentials. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.")
        st.stop()

    auth_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
    sp = spotipy.Spotify(auth_manager=auth_manager, requests_timeout=10)
    token = auth_manager.get_access_token(as_dict=False)
    return sp, token


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def parse_spotify_url(url):
    """Parse Spotify URL and return (type, id)."""
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
# RATE-LIMIT BACKOFF (per Spotify docs: honor Retry-After on 429)
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


def safe_get(url, headers, params=None, max_retries=6, throttle=0.2, max_wait=30):
    """GET with a backoff-retry strategy for Spotify's rolling ~30s rate limit.

    On HTTP 429, Spotify returns a `Retry-After` header (seconds). We wait that
    long before retrying, as recommended in the Web API docs. If the header is
    missing we fall back to exponential backoff. A small proactive throttle
    between successful calls keeps us from saturating the window in the first
    place (important for large artists / playlists).

    If Spotify returns a very large Retry-After (a long server-side cooldown,
    e.g. many minutes/hours after excessive requests), we do NOT block for that
    long — we abort so the app stays responsive. Try again later once the
    penalty window clears.

    On HTTP 403 (the credits endpoint flagging the cookie), we stop retrying
    immediately and return the response so the caller can halt gracefully —
    continuing would only deepen the server-side penalty.
    """
    resp = None
    for attempt in range(max_retries):
        _RATE.acquire()  # shared rolling-window budget across the whole run
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
        except requests.exceptions.RequestException:
            # Transient network error (timeout / connection reset). Do NOT crash
            # the whole run — back off briefly and retry.
            time.sleep(min(2 ** attempt, max_wait))
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
                st.warning(
                    f"🛑 Spotify applied a long cooldown (Retry-After ≈ {wait // 60} min) "
                    "due to too many requests. Please wait and try again later."
                )
                return resp  # abort; caller handles empty result gracefully

            time.sleep(wait)
            continue
        time.sleep(throttle)  # proactive spacing between successful calls
        return resp
    return resp


# ─────────────────────────────────────────────
# CREDITS
# ─────────────────────────────────────────────

# Persistent on-disk cache of credits keyed by track_id. Credits never change,
# so once fetched we never hit the (rate-limited) credits endpoint for that
# track again — the biggest protection against re-triggering a cooldown on
# repeated runs / overlapping catalogs.
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
# "<type>:<id>". Collecting a large artist's catalog costs ~150 main-API
# calls; caching it means we do that ONCE and every later run reads track_ids
# from disk with ZERO main-API calls — the reliable way to never re-trigger
# the api.spotify.com "too many requests" cooldown from repeated scans.
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


def get_cached_tracklist(link_type, link_id):
    """Return (songs, name) from the metadata cache, or (None, None) on miss."""
    entry = load_metadata_cache().get(f"{link_type}:{link_id}")
    if entry and entry.get("songs"):
        return entry["songs"], entry.get("name", "Unknown")
    return None, None


def store_tracklist(link_type, link_id, name, songs):
    """Persist a successful, non-empty track list so later runs skip the scan."""
    if not songs:
        return  # never cache an empty/cooldown result
    cache = load_metadata_cache()
    cache[f"{link_type}:{link_id}"] = {"name": name, "songs": songs}
    save_metadata_cache(cache)


def get_web_player_token():
    """Get authenticated token for credits endpoint using sp_dc cookie."""
    _, _, sp_dc = get_config()
    if not sp_dc:
        return None

    # Method 1: Direct endpoint
    try:
        resp = requests.get(
            "https://open.spotify.com/get_access_token?reason=transport&productType=web_player",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Cookie": f"sp_dc={sp_dc}",
            }, timeout=10)
        if resp.status_code == 200:
            try:
                data = resp.json()
                token = data.get("accessToken")
                if token:
                    return token
            except (json.JSONDecodeError, ValueError):
                pass
    except Exception:
        pass

    # Method 2: Authenticated embed page
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })
        session.cookies.set("sp_dc", sp_dc, domain="open.spotify.com", path="/")
        resp = session.get("https://open.spotify.com/embed/track/4iV5W9uYEdYUVa79Axb7Rh", timeout=10)
        if resp.status_code == 200:
            match = re.search(r'"accessToken":"([^"]+)"', resp.text)
            if match:
                return match.group(1)
    except Exception:
        pass

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

        credit_parts = []
        for role_credit in data.get("roleCredits", []):
            role_title = role_credit.get("roleTitle", "")
            artists = [a.get("name", "") for a in role_credit.get("artists", []) if a.get("name")]
            if artists:
                credit_parts.append(f"{role_title}: {', '.join(artists)}")

        sources = data.get("sourceNames", [])
        if sources:
            credit_parts.append(f"Source: {', '.join(sources)}")

        return " | ".join(credit_parts), "ok"
    # 429 exhausted or any other non-200 → transient; retry on a later run.
    return "", "throttled"


# ─────────────────────────────────────────────
# EXTRACTION FUNCTIONS
# ─────────────────────────────────────────────

def fetch_artist_songs(headers, api_base, artist_id, progress_bar=None):
    """Fetch all songs from an artist."""
    resp = safe_get(f"{api_base}/artists/{artist_id}", headers)
    artist_info = resp.json() if resp is not None and resp.status_code == 200 else {}
    artist_name = artist_info.get("name", "Unknown")

    # Get all albums (paginated). NOTE: Spotify reduced the max page size for
    # this endpoint to 10 (limit >= 11 now returns HTTP 400 "Invalid limit"),
    # so we page at 10. The shared rolling-window limiter in safe_get keeps the
    # extra pagination calls from tripping any rate-limit cooldown.
    all_albums = []
    offset = 0
    while True:
        resp = safe_get(
            f"{api_base}/artists/{artist_id}/albums", headers,
            params={"include_groups": "album,single,compilation", "limit": 10, "offset": offset}
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

    all_songs = []
    seen = set()
    total = len(album_ids)
    for idx, aid in enumerate(album_ids):
        if progress_bar:
            progress_bar.progress((idx + 1) / max(total, 1), text=f"Scanning album {idx+1}/{total}")

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
                "track_number": track["track_number"],
                "duration": ms_to_min_sec(track["duration_ms"]),
                "explicit": track["explicit"],
                "release_date": release_date,
                "track_id": track["id"],
            })

    all_songs.sort(key=lambda x: x.get("release_date", ""), reverse=True)
    return all_songs, artist_name


def fetch_album_songs(headers, api_base, album_id):
    """Fetch all songs from an album."""
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
            "track_number": track["track_number"],
            "duration": ms_to_min_sec(track["duration_ms"]),
            "explicit": track["explicit"],
            "release_date": release_date,
            "track_id": track["id"],
        })
    return songs, f"{album_name} — {artist_name}"


def fetch_playlist_songs(headers, api_base, playlist_id):
    """Fetch all songs from a playlist."""
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
            if track is None:
                continue
            songs.append({
                "song_title": track["name"],
                "artists": ", ".join(a["name"] for a in track["artists"]),
                "album": track["album"]["name"],
                "track_number": track["track_number"],
                "duration": ms_to_min_sec(track["duration_ms"]),
                "explicit": track["explicit"],
                "release_date": track["album"].get("release_date", "N/A"),
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
    """Fetch details for a single track."""
    resp = safe_get(f"{api_base}/tracks/{track_id}", headers)
    if resp is None or resp.status_code != 200:
        return [], "Unknown"

    track = resp.json()
    song = {
        "song_title": track["name"],
        "artists": ", ".join(a["name"] for a in track["artists"]),
        "album": track["album"]["name"],
        "track_number": track["track_number"],
        "duration": ms_to_min_sec(track["duration_ms"]),
        "explicit": track["explicit"],
        "release_date": track["album"].get("release_date", "N/A"),
        "track_id": track["id"],
    }
    return [song], track["name"]


# ─────────────────────────────────────────────
# STREAMLIT UI
# ─────────────────────────────────────────────

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LOGO.png")

st.set_page_config(
    page_title="The Administration",
    page_icon=LOGO_PATH if os.path.exists(LOGO_PATH) else "🎵",
    layout="wide",
)


def render_extractor():
    """🎧 Spotify Data Extractor — paste a Spotify URL → songs + credits."""
    st.subheader("🎧 Spotify Data Extractor")
    st.markdown("Paste any Spotify URL → get song data + credits as CSV. **No login required.**")

    url = st.text_input("🔗 Spotify URL", placeholder="https://open.spotify.com/artist/7bXgB6jMjp9ATFy66eO08Z")

    col1, col2 = st.columns([1, 4])
    with col1:
        include_credits = st.checkbox("Include Credits", value=True, help="Fetches writers, producers, sources (slower)")
    with col2:
        force_refresh = st.checkbox(
            "Force re-scan", value=False,
            help="Ignore the cached track list and re-scan metadata from Spotify. "
                 "Leave OFF to reuse the cached track list (avoids extra API calls)."
        )

    if not (st.button("🚀 Extract", type="primary", use_container_width=True) and url):
        return

    link_type, link_id = parse_spotify_url(url)

    if not link_type:
        st.error("❌ Invalid Spotify URL. Paste a link to an artist, album, track, or playlist.")
        st.stop()

    st.info(f"🔍 Detected: **{link_type.upper()}**")

    # Step 1: Authenticate
    sp, token = get_spotify_client()
    headers = {"Authorization": f"Bearer {token}"}
    api_base = "https://api.spotify.com/v1"

    # Step 2: Fetch tracks (reuse cached track list when available)
    data, name = (None, None) if force_refresh else get_cached_tracklist(link_type, link_id)
    if data:
        st.info(f"♻️ Loaded {len(data)} tracks from cache — no main-API scan needed.")
    else:
        with st.spinner(f"📥 Fetching {link_type} data from Spotify..."):
            if link_type == "artist":
                progress = st.progress(0, text="Scanning albums...")
                data, name = fetch_artist_songs(headers, api_base, link_id, progress_bar=progress)
                progress.empty()
            elif link_type == "album":
                data, name = fetch_album_songs(headers, api_base, link_id)
            elif link_type == "playlist":
                data, name = fetch_playlist_songs(headers, api_base, link_id)
            elif link_type == "track":
                data, name = fetch_single_track(headers, api_base, link_id)
        # Persist a successful scan so future runs skip the main API entirely.
        store_tracklist(link_type, link_id, name, data)

    if not data:
        st.error("❌ No tracks found. The URL may be invalid or the content is not accessible.")
        st.stop()

    st.success(f"✅ Found **{len(data)} tracks** from *{name}*")

    # Step 3: Fetch credits
    if include_credits:
        web_token = get_web_player_token()
        if web_token:
            limiter = _CreditsRateLimiter(min_interval=1.5, jitter=0.4)
            cache = load_credits_cache()
            credits_progress = st.progress(0, text="Fetching credits...")
            hit_cooldown = False
            fetched_since_save = 0
            consecutive_throttled = 0
            for i, song in enumerate(data):
                tid = song["track_id"]
                # Cache hit → no network call at all (credits never change).
                if tid in cache:
                    song["credits"] = cache[tid]
                    credits_progress.progress((i + 1) / len(data), text=f"Credits: {i+1}/{len(data)} (cached)")
                    continue
                if hit_cooldown:
                    song["credits"] = ""
                    continue
                song["credits"], status = fetch_track_credits(tid, web_token, limiter)
                credits_progress.progress((i + 1) / len(data), text=f"Credits: {i+1}/{len(data)}")
                if status == "cooldown":
                    # Endpoint flagged us — stop hitting it to avoid a long ban.
                    hit_cooldown = True
                    st.warning(
                        "⏸ Spotify flagged the credits endpoint (403). Stopped early to "
                        "avoid a multi-hour cooldown. Remaining tracks have no credits — "
                        "try again later (cached progress is saved)."
                    )
                    continue
                if status == "throttled":
                    # Couldn't fetch now (429/network). Do NOT cache — leave blank
                    # so a later run retries it. Pause longer if it keeps happening.
                    song["credits"] = ""
                    consecutive_throttled += 1
                    if consecutive_throttled >= 5:
                        save_credits_cache(cache)
                        time.sleep(60)  # let the rolling window fully drain
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
            credits_progress.empty()
            credited = sum(1 for s in data if s.get("credits"))
            st.info(f"📝 Credits found for {credited}/{len(data)} tracks")
        else:
            st.warning("⚠️ Could not get credits token (SP_DC cookie missing or expired). Skipping credits.")
            for song in data:
                song["credits"] = ""
    else:
        for song in data:
            song["credits"] = ""

    # Step 4: Display results
    df = pd.DataFrame(data)
    display_cols = ["song_title", "artists", "album", "duration", "release_date", "credits"]
    display_cols = [c for c in display_cols if c in df.columns]
    st.dataframe(df[display_cols], use_container_width=True, height=400)

    # Step 5: Download CSV
    csv_data = df.to_csv(index=False)
    safe_name = re.sub(r'[^a-zA-Z0-9]', '_', name)[:30]
    st.download_button(
        "📥 Download CSV",
        csv_data,
        file_name=f"{link_type}_{safe_name}.csv",
        mime="text/csv",
        use_container_width=True
    )


def render_isrc_finder():
    """🎼 ISRC Finder — upload a CSV/TXT/PDF → get ISRCs via Soundcharts.

    This path does NOT use the Spotify Data Extractor and makes no Spotify API
    calls from this machine (Soundcharts resolves the ISRCs on its side).
    """
    st.subheader("🎼 ISRC Finder")
    st.markdown(
        "Upload a **CSV, TXT, or PDF** with song **title + artist** → get the **ISRC** for each, "
        "exported as **PDF** (and CSV). Powered by **Soundcharts** — no Spotify login or API calls."
    )

    up = st.file_uploader("📄 Upload CSV / TXT / PDF", type=["csv", "txt", "pdf"])
    c1, c2 = st.columns([1, 3])
    with c1:
        min_interval = st.slider(
            "Delay between lookups (s)", 1.0, 3.0, 1.5, 0.5,
            help="Higher = gentler on Soundcharts (avoids being rate-limited)."
        )

    if not st.button("🔎 Find ISRCs", type="primary", use_container_width=True):
        return
    if not up:
        st.warning("Please upload a CSV, TXT, or PDF file first.")
        return

    import tempfile
    suffix = os.path.splitext(up.name)[1].lower() or ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
        tf.write(up.getbuffer())
        tmp_path = tf.name
    try:
        rows = drop_header_like(read_input(tmp_path))
    except Exception as exc:
        st.error(f"❌ Could not read the file: {exc}")
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not rows:
        st.error("❌ No songs found in the file. Make sure it has a title (and ideally an artist).")
        return

    st.info(f"📥 Read **{len(rows)}** song(s). Looking up ISRCs via Soundcharts…")
    client = SoundchartsClient(min_interval=min_interval)
    bar = st.progress(0, text="Starting…")

    def _cb(i, total, label):
        bar.progress(i / total, text=f"🔎 {label}")

    try:
        results = sc_process(rows, client, progress_cb=_cb)
    except Exception as exc:
        st.error(f"❌ Lookup failed: {exc}")
        return
    bar.empty()

    df = pd.DataFrame(results)
    found = int((df["isrc"] != "NOT FOUND").sum())
    st.success(f"✅ Found **{found}/{len(results)}** ISRCs")
    st.caption(
        "Tip: check **Matched Track / Matched Artists** to verify — for unusual titles a "
        "loose match can occasionally pick a different recording."
    )
    st.dataframe(df, use_container_width=True, height=420)

    base = re.sub(r'[^a-zA-Z0-9]', '_', os.path.splitext(up.name)[0])[:40] or "isrc"
    pdf_tmp = os.path.join(tempfile.gettempdir(), f"{base}_isrc.pdf")
    save_isrc_pdf(results, pdf_tmp, heading="ISRC Results (via Soundcharts)")
    with open(pdf_tmp, "rb") as f:
        st.download_button(
            "📄 Download PDF", f.read(), file_name=f"{base}_isrc.pdf",
            mime="application/pdf", use_container_width=True,
        )
    st.download_button(
        "🧾 Download CSV", df.to_csv(index=False), file_name=f"{base}_isrc.csv",
        mime="text/csv", use_container_width=True,
    )


# ─────────────────────────────────────────────
# PAGE HEADER + MODE DISPATCH
# ─────────────────────────────────────────────

_hcol1, _hcol2 = st.columns([1, 8])
with _hcol1:
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, width=64)
with _hcol2:
    st.title("The Administration")

if os.path.exists(LOGO_PATH):
    st.sidebar.image(LOGO_PATH, width=140)
st.sidebar.markdown("### Choose a tool")
_mode = st.sidebar.radio(
    "Mode",
    ["🎧 Spotify Data Extractor", "🎼 ISRC Finder (upload)"],
    label_visibility="collapsed",
)
st.sidebar.caption(
    "**Extractor** pulls songs + credits from a Spotify link.\n\n"
    "**ISRC Finder** takes an uploaded CSV/TXT/PDF and returns ISRCs via "
    "Soundcharts (no Spotify API)."
)

if _mode.startswith("🎧"):
    render_extractor()
else:
    render_isrc_finder()

