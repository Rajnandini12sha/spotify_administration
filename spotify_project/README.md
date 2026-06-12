# 🎧 Spotify Data Extractor

Extract track details from any Spotify URL (artist, album, track, playlist) and save to CSV.

---

## 📁 Project Structure

```
spotify_project/
├── .env                 # Spotify credentials (DO NOT share)
├── .gitignore           # Keeps credentials out of git
├── extract.py           # Main extractor script
├── main.py              # (Future) Analysis script
├── test_credits.py      # Test script for credits endpoint
├── requirements.txt     # Python dependencies
└── README.md            # This file
```

---

## 🚀 Setup (One-time)

### 1. Install Dependencies

```bash
pip3 install -r requirements.txt
```

### 2. Spotify Developer Account

1. Go to https://developer.spotify.com/dashboard
2. Log in with your Spotify account
3. Create an App → name it anything
4. Copy `Client ID` and `Client Secret`
5. In Settings → add Redirect URI: `http://127.0.0.1:8000/callback`

### 3. Configure `.env`

```env
SPOTIFY_CLIENT_ID=your_client_id_here
SPOTIFY_CLIENT_SECRET=your_client_secret_here
```

---

## ▶️ How to Run

```bash
python3 extract.py
```

### First Time:
1. Enter a Spotify URL when prompted
2. Browser opens → Log in to Spotify → Click **Agree**
3. Browser shows "can't be reached" — **that's OK!**
4. Copy the **full URL** from the browser address bar
5. Paste it into the terminal
6. ✅ Data extracted and saved to CSV!

### After First Time:
- Token is saved for ~1 hour
- Just enter a URL and it works instantly (no browser login needed)
- If token expires, it will ask you to log in again

---

## 🔗 Supported URLs

| Type | Example |
|------|---------|
| Artist | `https://open.spotify.com/artist/7bXgB6jMjp9ATFy66eO08Z` |
| Album | `https://open.spotify.com/album/7GToxH8az0ztPfYCeOAqij` |
| Track | `https://open.spotify.com/track/6XqByEYtFrbH2OPwQuGn8q` |
| Playlist | `https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M` |

> ⚠️ **Note:** Personalized playlists (Daily Mix, Discover Weekly etc.) with IDs starting with `37i9dQZF1E4...` are NOT accessible via API.

---

## 📊 Output

Results are saved as CSV files in the project folder:

| URL Type | Output File |
|----------|-------------|
| Artist | `artist_Chris_Brown.csv` |
| Album | `album_BROWN.csv` |
| Track | `track_Leave_Me_Alone.csv` |
| Playlist | `playlist_Today_s_Top_Hits.csv` |

### CSV Columns:
- `song_title` — Name of the track
- `artists` — All artists (comma-separated)
- `album` — Album name
- `track_number` — Position in album
- `duration` — Length (min:sec)
- `explicit` — True/False
- `release_date` — Release date (YYYY-MM-DD)
- `track_id` — Spotify track ID

---

## ⚠️ Known Limitations

1. **Token expires after ~1 hour** — re-run and authenticate again
2. **API rate limits** — for large artists (100+ albums), extraction takes 2-3 minutes
3. **Personalized playlists** — Daily Mixes, Discover Weekly etc. return 404
4. **Credits/Composers** — not available through standard API (requires browser cookie)
5. **Pagination limit** — API only allows `limit=5` per request for artist albums

---

## 🛠️ Troubleshooting

| Problem | Solution |
|---------|----------|
| `Token expired` | Run again, authenticate in browser |
| `redirect_uri: Not matching` | Make sure dashboard has exactly `http://127.0.0.1:8000/callback` |
| `404 Resource not found` | Playlist is personalized (use a public one) |
| `Could not find authorization code` | You pasted the wrong URL — paste the one from address bar AFTER login |
| `FileNotFoundError .spotify_token` | Token doesn't exist yet — run `extract.py` and authenticate first |

---

## 🔮 Future Enhancements

- [ ] Add song credits (Composers, Producers, Performers) via `sp_dc` cookie
- [ ] Data analysis (popularity trends, explicit %, genre breakdown)
- [ ] Search by artist/song name (without needing URL)
- [ ] Export to Excel with formatting
- [ ] Compare two artists side by side
- [ ] Circuit breaker pattern for large-scale extraction

---

## 📝 Quick Reference

```bash
# Run extractor
python3 extract.py

# If token expired, delete and re-authenticate
rm -f .spotify_token && python3 extract.py
```

