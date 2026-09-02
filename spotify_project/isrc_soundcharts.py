"""
🎼 ISRC Finder — via Soundcharts (no Spotify API calls from this machine)

A SEPARATE alternative to isrc.py. Instead of calling the Spotify Web API
directly, this scrapes Soundcharts' public ISRC Finder tool:

    https://soundcharts.com/en/isrc-finder

You submit a free-text "artist + title" query (or a Spotify track URL) and it
returns the official ISRC plus title/artist/release. Soundcharts resolves the
recording on its side (it uses the Spotify API with ITS OWN credentials), so
your machine never touches api.spotify.com — which is exactly the "bypass" you
asked for. No login and no CAPTCHA are required for the public tool.

Input/formatting is shared with isrc.py (CSV / TXT / PDF in, PDF + CSV out) —
we import those helpers so isrc.py stays untouched.

Usage:
    python isrc_soundcharts.py test_isrc_input.csv
    python isrc_soundcharts.py test_artist_muzalini.pdf -o out.pdf --csv-out out.csv
    python isrc_soundcharts.py songs.txt --min-interval 2.0

Be a good citizen: this hits a third-party site, so requests are paced
(min-interval + jitter) and every result is cached to disk so re-runs make
zero network calls.
"""

import os
import re
import sys
import json
import html
import time
import random
import argparse

import requests
import urllib3

# Reuse isrc.py's IO helpers so this file stays focused on Soundcharts and
# isrc.py remains completely untouched.
from isrc import read_input, save_isrc_pdf, save_isrc_csv


SOUNDCHARTS_URL = "https://soundcharts.com/en/isrc-finder"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".isrc_soundcharts_cache.json"
)


# ─────────────────────────────────────────────
# HEADER GUARD (isrc.py is left untouched, so we defend here)
# ─────────────────────────────────────────────

_HEADER_TITLES = {"title", "song", "song_title", "song title", "track", "track_title", "name"}
_HEADER_ARTISTS = {"artist", "artists", "performer", "performers", "by", ""}


def drop_header_like(rows):
    """Remove a leading row that is actually a CSV header misread as data.

    csv.Sniffer can fail to detect a header on small files, which makes
    read_input() return the header line ("title","artist") as if it were a
    song. We defensively drop that first row when it clearly looks like labels.
    """
    if rows:
        t = (rows[0].get("title") or "").strip().lower()
        a = (rows[0].get("artist") or "").strip().lower()
        if t in _HEADER_TITLES and a in _HEADER_ARTISTS:
            return rows[1:]
    return rows


def _clean(text):
    """Normalize a title/artist for a more forgiving second-attempt query.

    Soundcharts' search is stricter than Spotify's, so a "(feat. …)" suffix or
    symbols like $ / " / & can cause a miss. This strips those so we can retry.
    """
    if not text:
        return ""
    t = re.sub(r"\((?:feat|ft)\.?[^)]*\)", " ", text, flags=re.IGNORECASE)  # drop (feat. …)
    t = re.sub(r"\b(?:feat|ft)\.?\s.*$", " ", t, flags=re.IGNORECASE)        # drop trailing feat …
    t = t.replace("$", "s").replace('"', " ").replace("&", " and ")
    t = re.sub(r"[^\w\s]", " ", t)                                            # drop other symbols
    return re.sub(r"\s+", " ", t).strip()


def candidate_queries(title, artist, query):
    """Ordered, de-duplicated queries to try for one song (miss → retry cleaner)."""
    primary = re.split(r",|&|feat\.?|ft\.?|/", artist, flags=re.IGNORECASE)[0].strip() if artist else ""
    ct, ca = _clean(title), _clean(primary)
    cands = [
        query or f"{title} {artist}",
        f"{ct} {ca}".strip() if (ct or ca) else "",
        ct,
    ]
    out, seen = [], set()
    for c in cands:
        c = (c or "").strip()
        if c and len(c) >= 3 and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


# ─────────────────────────────────────────────
# CACHE (re-runs cost zero network calls)
# ─────────────────────────────────────────────

def load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def save_cache(cache):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError:
        pass


# ─────────────────────────────────────────────
# SOUNDCHARTS CLIENT
# ─────────────────────────────────────────────

class SoundchartsCooldown(Exception):
    """Raised when Soundcharts blocks/rate-limits us (429/403) — stop the run."""


class SoundchartsClient:
    """Minimal client for the public Soundcharts ISRC Finder.

    • Fetches the CSRF token once (refreshes it automatically if it expires).
    • Paces requests (fixed interval + jitter) to stay polite to a 3rd party.
    • Parses the result card: title / artist / release_date / isrc.
    """

    def __init__(self, min_interval=1.5, jitter=0.6):
        self.min_interval = min_interval
        self.jitter = jitter
        self._last = 0.0
        self._token = None
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        # Corporate TLS-inspection proxies present a self-signed chain that makes
        # verification fail for this domain. Try verified first; fall back to
        # unverified (with warnings silenced) so the tool still works.
        self._verify = True

    # ---- pacing -------------------------------------------------------------
    def _wait(self):
        now = time.monotonic()
        gap = self.min_interval + random.uniform(0, self.jitter)
        elapsed = now - self._last
        if self._last and elapsed < gap:
            time.sleep(gap - elapsed)
        self._last = time.monotonic()

    # ---- low-level GET/POST with SSL fallback -------------------------------
    def _request(self, method, **kwargs):
        kwargs.setdefault("timeout", 30)
        try:
            return self.session.request(method, SOUNDCHARTS_URL, verify=self._verify, **kwargs)
        except requests.exceptions.SSLError:
            if self._verify:
                # Retry unverified (TLS-inspection proxy) and remember the choice.
                urllib3.disable_warnings()
                self._verify = False
                print("   ⚠️  TLS verification failed (proxy); continuing without verification.")
                return self.session.request(method, SOUNDCHARTS_URL, verify=False, **kwargs)
            raise

    # ---- token --------------------------------------------------------------
    def _fetch_token(self):
        resp = self._request("GET")
        if resp.status_code in (403, 429):
            raise SoundchartsCooldown(f"Soundcharts returned {resp.status_code} on load.")
        m = re.search(r'name="isrc_finder\[_token]"[^>]*value="([0-9a-f]+)"', resp.text)
        self._token = m.group(1) if m else None
        return self._token

    def _ensure_token(self):
        if not self._token:
            self._fetch_token()
        return self._token

    # ---- search -------------------------------------------------------------
    def search(self, query):
        """Return dict(isrc, title, artist, release_date) or None if not found."""
        self._ensure_token()
        for attempt in range(2):  # one retry to refresh an expired CSRF token
            self._wait()
            resp = self._request(
                "POST",
                data={"isrc_finder[query]": query, "isrc_finder[_token]": self._token},
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": SOUNDCHARTS_URL},
            )
            if resp.status_code in (403, 429):
                raise SoundchartsCooldown(
                    f"Soundcharts rate-limited/blocked us (HTTP {resp.status_code}).")
            if resp.status_code == 419 or (resp.status_code >= 400 and attempt == 0):
                # Likely an expired CSRF token — refresh once and retry.
                self._fetch_token()
                continue
            return self._parse(resp.text)
        return None

    @staticmethod
    def _parse(text):
        art = re.search(r'<article class="isrc__result">([\s\S]*?)</article>', text)
        if not art:
            return None
        a = art.group(1)

        def grab(pat):
            m = re.search(pat, a)
            return html.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""

        isrc = grab(r'data-clipboard-text-value="([A-Z0-9]{12})"') or \
            grab(r'isrc__isrc-value">([^<]+)</code>')
        if not isrc:
            return None
        return {
            "isrc": isrc,
            "title": grab(r'isrc__song-title">([\s\S]*?)</h2>'),
            "artist": re.sub(r"^by\s+", "", grab(r'isrc__song-credit">([\s\S]*?)</p>')),
            "release_date": re.sub(r"^Released\s+", "", grab(r'isrc__meta">([\s\S]*?)</p>')),
        }


# ─────────────────────────────────────────────
# PROCESS
# ─────────────────────────────────────────────

def process(rows, client, progress_cb=None):
    cache = load_cache()
    results = []
    total = len(rows)
    fetched_since_save = 0
    aborted = False

    for i, row in enumerate(rows, 1):
        title, artist, query = row["title"], row["artist"], row["query"]
        key = (query or f"{title} {artist}").lower().strip()

        if key in cache:
            hit = cache[key]
            print(f"   ♻️  {i}/{total}  {title or query} → {hit.get('isrc') or 'NOT FOUND'} (cached)")
        elif aborted:
            hit = {"isrc": "", "title": "", "artist": "", "release_date": ""}
        else:
            try:
                found = None
                for cand in candidate_queries(title, artist, query):
                    found = client.search(cand)
                    if found and found.get("isrc"):
                        break  # got it — don't spend extra requests
            except SoundchartsCooldown as exc:
                print(f"\n🛑 {exc}\n   Stopping to avoid a block; cached progress is saved. "
                      "Try again later.")
                save_cache(cache)
                aborted = True
                found = None
                hit = {"isrc": "", "title": "", "artist": "", "release_date": ""}
            else:
                hit = found or {"isrc": "", "title": "", "artist": "", "release_date": ""}
                status = "✅" if hit.get("isrc") else "❌"
                print(f"   {status} {i}/{total}  {title or query} → {hit.get('isrc') or 'NOT FOUND'}")
                # Only cache successful lookups so genuine misses are retried on
                # a later run (Soundcharts search can be stricter on odd titles).
                if hit.get("isrc"):
                    cache[key] = hit
                    fetched_since_save += 1
                    if fetched_since_save >= 25:
                        save_cache(cache)
                        fetched_since_save = 0

        results.append({
            "title": title or hit.get("title", ""),
            "artist": artist or hit.get("artist", ""),
            "matched_track": hit.get("title", ""),
            "matched_artists": hit.get("artist", ""),
            "isrc": hit.get("isrc") or "NOT FOUND",
        })

        # Optional progress callback (used by the Streamlit UI).
        if progress_cb is not None:
            try:
                progress_cb(i, total, f"{i}/{total} · {title or query}")
            except Exception:
                pass

    save_cache(cache)
    return results


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run(input_path, pdf_out=None, csv_out=None, min_interval=1.5):
    if not os.path.exists(input_path):
        print(f"❌ Input file not found: {input_path}")
        sys.exit(1)

    rows = read_input(input_path)
    rows = drop_header_like(rows)
    if not rows:
        print("❌ No rows found in the input file.")
        sys.exit(1)
    print(f"📥 Read {len(rows)} song(s) from {input_path}")

    print("🔎 Looking up ISRCs via Soundcharts (no Spotify API calls)...")
    client = SoundchartsClient(min_interval=min_interval)
    results = process(rows, client)

    base = os.path.splitext(os.path.basename(input_path))[0]
    pdf_out = pdf_out or f"{base}_isrc_soundcharts.pdf"
    save_isrc_pdf(results, pdf_out, heading="ISRC Results (via Soundcharts)")
    print(f"📄 Saved PDF → {os.path.abspath(pdf_out)}")

    if csv_out:
        save_isrc_csv(results, csv_out)
        print(f"🧾 Saved CSV → {os.path.abspath(csv_out)}")

    found = sum(1 for r in results if r["isrc"] != "NOT FOUND")
    print(f"✅ Done — {found}/{len(results)} ISRCs found.")
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Batch ISRC finder via Soundcharts (CSV/TXT/PDF → ISRC column → PDF).")
    parser.add_argument("input", help="Input CSV, TXT, or PDF file (title + artist).")
    parser.add_argument("-o", "--output", help="Output PDF path.")
    parser.add_argument("--csv-out", help="Also write results to this CSV path.")
    parser.add_argument("--min-interval", type=float, default=1.5,
                        help="Minimum seconds between Soundcharts requests (default 1.5).")
    args = parser.parse_args()
    run(args.input, pdf_out=args.output, csv_out=args.csv_out, min_interval=args.min_interval)


if __name__ == "__main__":
    main()
