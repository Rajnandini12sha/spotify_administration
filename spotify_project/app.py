"""
🎧 Spotify Data Extractor — Streamlit Web App
Paste any Spotify URL → get a CSV with full credits.
No login needed. Fully automated.
"""

import os
import re
import json
import time
import requests
import streamlit as st
import pandas as pd
from spotipy.oauth2 import SpotifyClientCredentials
import spotipy

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
# CREDITS
# ─────────────────────────────────────────────

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


def fetch_track_credits(track_id, web_token):
    """Fetch all credits for a track."""
    if not web_token:
        return ""

    headers = {
        "Authorization": f"Bearer {web_token}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Content-type": "application/json",
        "app-platform": "WebPlayer",
    }

    url = f"https://spclient.wg.spotify.com/track-credits-view/v0/experimental/{track_id}/credits"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('trackTitle', '') == '':
                return ""

            credit_parts = []
            for role_credit in data.get("roleCredits", []):
                role_title = role_credit.get("roleTitle", "")
                artists = [a.get("name", "") for a in role_credit.get("artists", []) if a.get("name")]
                if artists:
                    credit_parts.append(f"{role_title}: {', '.join(artists)}")

            sources = data.get("sourceNames", [])
            if sources:
                credit_parts.append(f"Source: {', '.join(sources)}")

            return " | ".join(credit_parts)
        elif resp.status_code == 429:
            time.sleep(3)
            return fetch_track_credits(track_id, web_token)
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────
# EXTRACTION FUNCTIONS
# ─────────────────────────────────────────────

def fetch_artist_songs(headers, api_base, artist_id, progress_bar=None):
    """Fetch all songs from an artist."""
    resp = requests.get(f"{api_base}/artists/{artist_id}", headers=headers)
    artist_info = resp.json() if resp.status_code == 200 else {}
    artist_name = artist_info.get("name", "Unknown")

    # Get all albums
    all_albums = []
    offset = 0
    while True:
        resp = requests.get(
            f"{api_base}/artists/{artist_id}/albums", headers=headers,
            params={"include_groups": "album,single,compilation", "limit": 5, "offset": offset}
        )
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", 5)))
            continue
        if resp.status_code != 200:
            break
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        all_albums.extend(items)
        if not data.get("next"):
            break
        offset += 5

    # Get tracks from each album
    all_songs = []
    seen = set()
    for i, album in enumerate(all_albums):
        if progress_bar:
            progress_bar.progress((i + 1) / len(all_albums), text=f"Scanning album {i+1}/{len(all_albums)}")

        resp = requests.get(f"{api_base}/albums/{album['id']}", headers=headers)
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", 5)))
            resp = requests.get(f"{api_base}/albums/{album['id']}", headers=headers)
        if resp.status_code != 200:
            continue

        album_data = resp.json()
        album_name = album_data.get("name", "Unknown")
        release_date = album_data.get("release_date", "N/A")

        for track in album_data.get("tracks", {}).get("items", []):
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
    resp = requests.get(f"{api_base}/albums/{album_id}", headers=headers)
    if resp.status_code != 200:
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
    resp = requests.get(f"{api_base}/playlists/{playlist_id}", headers=headers)
    if resp.status_code != 200:
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
        resp = requests.get(next_url, headers=headers)
        if resp.status_code != 200:
            break
        tracks_data = resp.json()

    return songs, playlist_name


def fetch_single_track(headers, api_base, track_id):
    """Fetch details for a single track."""
    resp = requests.get(f"{api_base}/tracks/{track_id}", headers=headers)
    if resp.status_code != 200:
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

st.set_page_config(page_title="Spotify Data Extractor", page_icon="🎧", layout="wide")

st.title("🎧 Spotify Data Extractor")
st.markdown("Paste any Spotify URL → get song data + credits as CSV. **No login required.**")

url = st.text_input("🔗 Spotify URL", placeholder="https://open.spotify.com/artist/7bXgB6jMjp9ATFy66eO08Z")

col1, col2 = st.columns([1, 4])
with col1:
    include_credits = st.checkbox("Include Credits", value=True, help="Fetches writers, producers, sources (slower)")

if st.button("🚀 Extract", type="primary", use_container_width=True) and url:
    link_type, link_id = parse_spotify_url(url)

    if not link_type:
        st.error("❌ Invalid Spotify URL. Paste a link to an artist, album, track, or playlist.")
        st.stop()

    st.info(f"🔍 Detected: **{link_type.upper()}**")

    # Step 1: Authenticate
    sp, token = get_spotify_client()
    headers = {"Authorization": f"Bearer {token}"}
    api_base = "https://api.spotify.com/v1"

    # Step 2: Fetch tracks
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

    if not data:
        st.error("❌ No tracks found. The URL may be invalid or the content is not accessible.")
        st.stop()

    st.success(f"✅ Found **{len(data)} tracks** from *{name}*")

    # Step 3: Fetch credits
    if include_credits:
        web_token = get_web_player_token()
        if web_token:
            credits_progress = st.progress(0, text="Fetching credits...")
            for i, song in enumerate(data):
                song["credits"] = fetch_track_credits(song["track_id"], web_token)
                credits_progress.progress((i + 1) / len(data), text=f"Credits: {i+1}/{len(data)}")
                if i < len(data) - 1:
                    time.sleep(1.5)
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
