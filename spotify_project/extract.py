"""
Spotify Data Extractor
Extract track details from Spotify artist, album, track, or playlist URLs.
Saves results to CSV format.
"""

import os
import re
import json
import csv
import time
import webbrowser
import requests
from urllib.parse import urlencode, urlparse, parse_qs
from dotenv import load_dotenv
import spotipy

load_dotenv()

REDIRECT_URI = "http://127.0.0.1:8000/callback"


# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

def setup_credentials():
    """Check for credentials in .env file. If missing, prompt user to enter them."""
    env_file = os.path.join(os.path.dirname(__file__), ".env")

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    if client_id and client_secret:
        return client_id, client_secret

    # Credentials not found — ask user
    print("\n⚙️  FIRST-TIME SETUP")
    print("=" * 50)
    print("You need Spotify API credentials to use this tool.")
    print("\nHow to get them (free):")
    print("  1. Go to https://developer.spotify.com/dashboard")
    print("  2. Log in with your Spotify account")
    print("  3. Create an App (any name)")
    print("  4. In Settings → add Redirect URI: http://127.0.0.1:8000/callback")
    print("  5. Copy Client ID and Client Secret")
    print("=" * 50)

    client_id = input("\n🔑 Enter your Client ID: ").strip()
    client_secret = input("🔐 Enter your Client Secret: ").strip()

    if not client_id or not client_secret:
        raise ValueError("Client ID and Client Secret are required!")

    # Save to .env for future use
    with open(env_file, "w") as f:
        f.write(f"SPOTIFY_CLIENT_ID={client_id}\n")
        f.write(f"SPOTIFY_CLIENT_SECRET={client_secret}\n")

    print("\n✅ Credentials saved to .env file (won't ask again)")
    return client_id, client_secret


# ─────────────────────────────────────────────
# AUTHENTICATION
# ─────────────────────────────────────────────

def get_spotify_client():
    """Authenticate with Spotify and return (spotipy_client, access_token)."""
    client_id, client_secret = setup_credentials()

    # Try using saved token
    token_file = os.path.join(os.path.dirname(__file__), ".spotify_token")
    if os.path.exists(token_file):
        with open(token_file, "r") as f:
            token_data = json.load(f)
            sp = spotipy.Spotify(auth=token_data["access_token"], requests_timeout=10)
            try:
                sp.current_user()
                print("✅ Using saved token")
                return sp, token_data["access_token"]
            except:
                print("🔄 Token expired, re-authenticating...")

    # Build auth URL and open browser
    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "playlist-read-private playlist-read-collaborative",
    }
    auth_url = "https://accounts.spotify.com/authorize?" + urlencode(auth_params)

    print("\n🌐 Opening browser for Spotify login...")
    print(f"\nIf browser doesn't open, go to this URL manually:")
    print(f"{auth_url}\n")
    webbrowser.open(auth_url)

    print("After logging in, you'll be redirected (page may show error - that's OK!).")
    print("Copy the FULL URL from your browser address bar and paste it here:\n")
    redirect_response = input("📋 Paste the URL here: ").strip()

    # Extract authorization code from callback URL
    parsed = urlparse(redirect_response)
    code = parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        raise ValueError("Could not find authorization code in the URL you pasted.")

    # Exchange code for token
    response = requests.post("https://accounts.spotify.com/api/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    })

    if response.status_code != 200:
        raise ValueError(f"Token exchange failed: {response.json()}")

    token_info = response.json()

    # Save token for reuse
    with open(token_file, "w") as f:
        json.dump(token_info, f)

    sp = spotipy.Spotify(auth=token_info["access_token"], requests_timeout=10)
    print("✅ Authenticated successfully!")
    return sp, token_info["access_token"]


# ─────────────────────────────────────────────
# URL PARSING & HELPERS
# ─────────────────────────────────────────────

def parse_spotify_url(url):
    """Parse Spotify URL and return (type, id). Supports playlist, album, track, artist."""
    patterns = [
        r'open\.spotify\.com/(playlist|album|track|artist)/([a-zA-Z0-9]+)',
        r'spotify:(playlist|album|track|artist):([a-zA-Z0-9]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1), match.group(2)
    raise ValueError(f"Invalid Spotify URL: {url}")


def ms_to_min_sec(ms):
    """Convert milliseconds to 'min:sec' format."""
    minutes = ms // 60000
    seconds = (ms % 60000) // 1000
    return f"{minutes}:{seconds:02d}"


def save_to_csv(songs, filename):
    """Save list of song dicts to CSV file."""
    if not songs:
        print("⚠️  No songs to save.")
        return

    filepath = os.path.join(os.path.dirname(__file__), filename)
    fieldnames = ["song_title", "artists", "album", "track_number", "duration",
                  "explicit", "release_date", "track_id", "credits"]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(songs)

    print(f"💾 Saved {len(songs)} songs to: {filename}")


# ─────────────────────────────────────────────
# CREDITS FETCHING (requires sp_dc cookie)
# ─────────────────────────────────────────────

def get_web_player_token():
    """Get a web player access token using sp_dc cookie from .env.
    This token has permissions to access the track-credits-view endpoint.
    Falls back to authenticated embed token if direct endpoint is blocked.
    """
    sp_dc = os.getenv("SP_DC")

    if sp_dc:
        # Method 1: Direct get_access_token endpoint
        try:
            token_url = "https://open.spotify.com/get_access_token?reason=transport&productType=web_player"
            resp = requests.get(token_url, headers={
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

        # Method 2: Use session with sp_dc cookie to get authenticated embed token
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

    # Fallback: anonymous embed page token (very limited permissions)
    try:
        resp = requests.get(
            "https://open.spotify.com/embed/track/4iV5W9uYEdYUVa79Axb7Rh",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            match = re.search(r'"accessToken":"([^"]+)"', resp.text)
            if match:
                return match.group(1)
    except Exception:
        pass

    return None


def fetch_track_credits(track_id, web_token):
    """Fetch all credits info for a single track (performers, writers, producers, sources)."""
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

            # Check if response is empty (no track title = failed)
            if data.get('trackTitle', '') == '':
                return ""

            # Build a single credits string with all roles
            credit_parts = []

            for role_credit in data.get("roleCredits", []):
                role_title = role_credit.get("roleTitle", "")
                artists = [a.get("name", "") for a in role_credit.get("artists", []) if a.get("name")]
                if artists:
                    credit_parts.append(f"{role_title}: {', '.join(artists)}")

            # Add source names if available
            sources = data.get("sourceNames", [])
            if sources:
                credit_parts.append(f"Source: {', '.join(sources)}")

            return " | ".join(credit_parts)

        elif resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 2))
            time.sleep(min(retry_after, 5))
            return fetch_track_credits(track_id, web_token)

    except Exception:
        pass

    return ""


def enrich_songs_with_credits(songs):
    """Add credits info (performers, writers, producers, sources) to songs."""
    if not songs:
        return songs

    sp_dc = os.getenv("SP_DC")
    if not sp_dc:
        print(f"\n📝 Credits: SP_DC cookie not found in .env — skipping credits.")
        print("  💡 To enable credits, add SP_DC to your .env file:")
        print("     1. Open https://open.spotify.com in browser (logged in)")
        print("     2. Open DevTools (Cmd+Option+I) → Application → Cookies")
        print("     3. Find 'sp_dc' cookie and copy its value")
        print("     4. Add to .env: SP_DC=your_cookie_value_here")
        for song in songs:
            song["credits"] = ""
        return songs

    print(f"\n📝 Fetching credits for {len(songs)} tracks...")
    web_token = get_web_player_token()

    if not web_token:
        print("  ⚠️  Could not get web player token — skipping credits.")
        for song in songs:
            song["credits"] = ""
        return songs

    for i, song in enumerate(songs, 1):
        track_id = song.get("track_id")
        if not track_id:
            song["credits"] = ""
            continue

        song["credits"] = fetch_track_credits(track_id, web_token)

        if i % 10 == 0 or i == len(songs):
            print(f"  📝 Credits: {i}/{len(songs)} tracks processed...")

        # Small delay to avoid rate limiting
        if i < len(songs):
            time.sleep(1.5)

    credited_count = sum(1 for s in songs if s.get("credits"))
    print(f"  ✅ Credits found for {credited_count}/{len(songs)} tracks")

    return songs


# ─────────────────────────────────────────────
# MAIN EXTRACTION LOGIC
# ─────────────────────────────────────────────

def extract_from_url(url):
    """Main entry point: extract data from any Spotify URL and save to CSV."""
    link_type, link_id = parse_spotify_url(url)

    print(f"\n🔍 Detected: {link_type.upper()} (ID: {link_id})")
    print("🔑 Authenticating with Spotify...")

    sp, token = get_spotify_client()
    headers = {"Authorization": f"Bearer {token}"}
    api_base = "https://api.spotify.com/v1"

    print("📥 Fetching data...")

    try:
        if link_type == "artist":
            data = fetch_artist_songs(headers, api_base, link_id)
        elif link_type == "album":
            data = fetch_album_songs(headers, api_base, link_id)
        elif link_type == "playlist":
            data = fetch_playlist_songs(headers, api_base, link_id)
        elif link_type == "track":
            data = fetch_single_track(headers, api_base, link_id)

        # Enrich with credits (composers & lyricists)
        if data:
            data = enrich_songs_with_credits(data)
            # Re-save with credits included
            if link_type == "artist":
                resp = requests.get(f"{api_base}/artists/{link_id}", headers=headers)
                name = resp.json().get("name", "Unknown") if resp.status_code == 200 else "Unknown"
                safe_name = re.sub(r'[^a-zA-Z0-9]', '_', name)[:30]
                save_to_csv(data, f"artist_{safe_name}.csv")
            elif link_type == "album":
                safe_name = re.sub(r'[^a-zA-Z0-9]', '_', data[0].get("album", "Unknown"))[:30]
                save_to_csv(data, f"album_{safe_name}.csv")
            elif link_type == "playlist":
                resp = requests.get(f"{api_base}/playlists/{link_id}", headers=headers)
                name = resp.json().get("name", "Unknown") if resp.status_code == 200 else "Unknown"
                safe_name = re.sub(r'[^a-zA-Z0-9]', '_', name)[:30]
                save_to_csv(data, f"playlist_{safe_name}.csv")
            elif link_type == "track":
                safe_name = re.sub(r'[^a-zA-Z0-9]', '_', data[0].get("song_title", "Unknown"))[:30]
                save_to_csv(data, f"track_{safe_name}.csv")

        return data

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()


def fetch_artist_songs(headers, api_base, artist_id):
    """Fetch all songs from an artist's discography."""

    # Get artist name
    resp = requests.get(f"{api_base}/artists/{artist_id}", headers=headers)
    artist_info = resp.json() if resp.status_code == 200 else {}
    artist_name = artist_info.get("name", "Unknown")
    print(f"  🎤 Artist: {artist_name}")

    # Get all albums (paginate with limit=5)
    all_albums = []
    offset = 0
    while True:
        resp = requests.get(
            f"{api_base}/artists/{artist_id}/albums",
            headers=headers,
            params={"include_groups": "album,single,compilation", "limit": 5, "offset": offset}
        )

        # Handle rate limiting (429)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5))
            if retry_after > 60:
                print(f"  ❌ Rate limited for {retry_after} seconds ({retry_after//3600}h {(retry_after%3600)//60}m).")
                print(f"  💡 Try again later or create a new app in Spotify Dashboard.")
                break
            print(f"  ⏳ Rate limited! Waiting {retry_after} seconds...")
            import time
            time.sleep(retry_after)
            continue

        if resp.status_code != 200:
            print(f"  ⚠️  Albums error at offset {offset}: {resp.status_code}")
            break

        data = resp.json()
        items = data.get("items", [])
        if not items:
            break

        all_albums.extend(items)
        total = data.get("total", "?")
        print(f"  📀 Albums: {len(all_albums)}/{total}")

        if not data.get("next"):
            break
        offset += 5

    print(f"  ✅ Total albums/singles: {len(all_albums)}")

    # Get tracks from each album
    all_songs = []
    seen = set()

    for i, album in enumerate(all_albums, 1):
        resp = requests.get(f"{api_base}/albums/{album['id']}", headers=headers)

        # Handle rate limiting
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5))
            print(f"  ⏳ Rate limited! Waiting {retry_after} seconds...")
            import time
            time.sleep(retry_after)
            resp = requests.get(f"{api_base}/albums/{album['id']}", headers=headers)

        if resp.status_code != 200:
            print(f"  ⚠️  Skipping album {album.get('name', '')}: {resp.status_code}")
            continue

        album_data = resp.json()
        album_name = album_data.get("name", "Unknown")
        release_date = album_data.get("release_date", "N/A")
        tracks = album_data.get("tracks", {}).get("items", [])

        for track in tracks:
            # Skip duplicates
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
                "duration_ms": track["duration_ms"],
                "explicit": track["explicit"],
                "release_date": release_date,
                "track_id": track["id"],
            })

        print(f"  🎵 [{i}/{len(all_albums)}] {album_name} — {len(tracks)} tracks")

    # Sort by release date (newest first)
    all_songs.sort(key=lambda x: x.get("release_date", ""), reverse=True)

    # Print summary
    print(f"\n{'='*60}")
    print(f"🎤 {artist_name} — Complete Discography")
    print(f"{'='*60}")
    print(f"📀 Albums/Singles: {len(all_albums)}")
    print(f"🎶 Total Songs   : {len(all_songs)}")
    print(f"{'='*60}")

    # Print song list
    print(f"\n{'#':<5} {'Song Title':<45} {'Album':<25} {'Released':<12} {'Duration':<8}")
    print("-" * 95)
    for i, song in enumerate(all_songs, 1):
        print(f"{i:<5} {song['song_title'][:43]:<45} {song['album'][:23]:<25} {song['release_date']:<12} {song['duration']:<8}")

    # Save to CSV
    safe_name = re.sub(r'[^a-zA-Z0-9]', '_', artist_name)[:30]
    save_to_csv(all_songs, f"artist_{safe_name}.csv")

    return all_songs


def fetch_album_songs(headers, api_base, album_id):
    """Fetch all songs from an album."""
    resp = requests.get(f"{api_base}/albums/{album_id}", headers=headers)
    if resp.status_code != 200:
        print(f"❌ Error fetching album: {resp.status_code}")
        return []

    album_data = resp.json()
    album_name = album_data.get("name", "Unknown")
    release_date = album_data.get("release_date", "N/A")
    artist_name = ", ".join(a["name"] for a in album_data.get("artists", []))

    print(f"  💿 Album: {album_name} by {artist_name}")

    songs = []
    for track in album_data.get("tracks", {}).get("items", []):
        songs.append({
            "song_title": track["name"],
            "artists": ", ".join(a["name"] for a in track["artists"]),
            "album": album_name,
            "track_number": track["track_number"],
            "duration": ms_to_min_sec(track["duration_ms"]),
            "duration_ms": track["duration_ms"],
            "explicit": track["explicit"],
            "release_date": release_date,
            "track_id": track["id"],
        })

    # Print
    print(f"\n{'='*60}")
    print(f"💿 {album_name} — {artist_name}")
    print(f"📅 Released: {release_date} | 🎶 Tracks: {len(songs)}")
    print(f"{'='*60}")
    print(f"\n{'#':<5} {'Song Title':<50} {'Duration':<10}")
    print("-" * 65)
    for song in songs:
        print(f"{song['track_number']:<5} {song['song_title'][:48]:<50} {song['duration']:<10}")

    # Save to CSV
    safe_name = re.sub(r'[^a-zA-Z0-9]', '_', album_name)[:30]
    save_to_csv(songs, f"album_{safe_name}.csv")

    return songs


def fetch_playlist_songs(headers, api_base, playlist_id):
    """Fetch all songs from a playlist."""
    resp = requests.get(f"{api_base}/playlists/{playlist_id}", headers=headers)
    if resp.status_code != 200:
        print(f"❌ Error fetching playlist: {resp.status_code} - {resp.text[:100]}")
        return []

    playlist_data = resp.json()
    playlist_name = playlist_data.get("name", "Unknown")
    owner = playlist_data.get("owner", {}).get("display_name", "Unknown")
    total_tracks = playlist_data.get("tracks", {}).get("total", 0)

    print(f"  🎵 Playlist: {playlist_name} by {owner} ({total_tracks} tracks)")

    # Get all tracks with pagination
    songs = []
    tracks_data = playlist_data.get("tracks", {})
    page = 1

    while True:
        print(f"  📄 Page {page}...")
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
                "duration_ms": track["duration_ms"],
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
        page += 1

    # Print
    print(f"\n{'='*60}")
    print(f"🎵 {playlist_name} — by {owner}")
    print(f"🎶 Total Tracks: {len(songs)}")
    print(f"{'='*60}")
    print(f"\n{'#':<5} {'Song Title':<40} {'Artist':<25} {'Duration':<10}")
    print("-" * 80)
    for i, song in enumerate(songs, 1):
        print(f"{i:<5} {song['song_title'][:38]:<40} {song['artists'][:23]:<25} {song['duration']:<10}")

    # Save to CSV
    safe_name = re.sub(r'[^a-zA-Z0-9]', '_', playlist_name)[:30]
    save_to_csv(songs, f"playlist_{safe_name}.csv")

    return songs


def fetch_single_track(headers, api_base, track_id):
    """Fetch details for a single track."""
    resp = requests.get(f"{api_base}/tracks/{track_id}", headers=headers)
    if resp.status_code != 200:
        print(f"❌ Error fetching track: {resp.status_code}")
        return []

    track = resp.json()
    song = {
        "song_title": track["name"],
        "artists": ", ".join(a["name"] for a in track["artists"]),
        "album": track["album"]["name"],
        "track_number": track["track_number"],
        "duration": ms_to_min_sec(track["duration_ms"]),
        "duration_ms": track["duration_ms"],
        "explicit": track["explicit"],
        "release_date": track["album"].get("release_date", "N/A"),
        "track_id": track["id"],
    }

    # Print
    print(f"\n{'='*60}")
    print(f"🎵 {song['song_title']}")
    print(f"{'='*60}")
    print(f"  🎤 Artists  : {song['artists']}")
    print(f"  💿 Album    : {song['album']}")
    print(f"  📅 Released : {song['release_date']}")
    print(f"  ⏱️  Duration : {song['duration']}")
    print(f"  🔞 Explicit : {'Yes' if song['explicit'] else 'No'}")
    print(f"{'='*60}")

    # Save to CSV
    safe_name = re.sub(r'[^a-zA-Z0-9]', '_', song['song_title'])[:30]
    save_to_csv([song], f"track_{safe_name}.csv")

    return [song]


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  🎧 SPOTIFY DATA EXTRACTOR")
    print("=" * 60)
    print("\nSupported: artist, album, track, playlist URLs")
    print("Example: https://open.spotify.com/artist/7bXgB6jMjp9ATFy66eO08Z\n")

    url = input("🔗 Enter Spotify URL: ").strip()
    if url:
        extract_from_url(url)
    else:
        print("❌ No URL provided.")
