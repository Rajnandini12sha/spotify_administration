"""
🎼 ISRC Finder — batch lookup (separate from the Spotify extractor app)

Goal: take a MANUAL upload/entry of songs (CSV or TXT with title + artist),
find the ISRC for each, add an "isrc" column, and export a PDF (and CSV).

Why not isrcfinder.com?
    isrcfinder.com only accepts a Spotify *track link* and is protected by a
    Cloudflare Turnstile CAPTCHA, so it cannot be automated. But that site is
    just a front-end for Spotify's data — the ISRC lives on the Spotify track
    object as `external_ids.isrc`. We read it from that same source directly,
    which is reliable, bulk-capable, needs no login and no CAPTCHA.

Usage:
    python isrc.py test_isrc_input.csv
    python isrc.py songs.csv -o my_isrcs.pdf
    python isrc.py songs.txt --csv-out my_isrcs.csv

Input formats:
    • CSV with headers — title/song/track column + artist/artists column
      (column names are auto-detected; a header row is recommended).
    • CSV without recognizable headers — first column = title, second = artist.
    • TXT — one song per line, free text like:
          Musalini's Stroll The Musalini, 9th Wonder
      (the whole line is used as the search query).

Credentials are reused from extract_cli.get_spotify_client() (which reads
.streamlit/secrets.toml or env vars).
"""

import os
import re
import csv
import sys
import json
import time
import argparse

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# Reuse the extractor's authenticated client AND its shared rolling-window rate
# limiter so ISRC lookups can NEVER saturate Spotify's ~30s window and trigger a
# multi-hour cooldown (the same protection the credits extractor uses).
from extract_cli import get_spotify_client, _RATE

try:
    from spotipy.exceptions import SpotifyException
except Exception:  # pragma: no cover
    class SpotifyException(Exception):
        http_status = None


# ─────────────────────────────────────────────
# ISRC CACHE (so re-runs make ZERO Spotify calls)
# ─────────────────────────────────────────────

ISRC_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".isrc_cache.json"
)


def load_isrc_cache():
    try:
        with open(ISRC_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def save_isrc_cache(cache):
    try:
        with open(ISRC_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError:
        pass


def _cache_key(title, artist, query):
    return f"{(title or '').lower().strip()}|{(artist or '').lower().strip()}|{(query or '').lower().strip()}"


# ─────────────────────────────────────────────
# INPUT PARSING
# ─────────────────────────────────────────────

TITLE_KEYS = ("title", "song", "song_title", "track", "track_title", "name")
ARTIST_KEYS = ("artist", "artists", "performer", "performers", "by")


def _pick_column(fieldnames, candidates):
    """Return the first fieldname whose lowercased name matches a candidate."""
    if not fieldnames:
        return None
    lowered = {f.lower().strip(): f for f in fieldnames}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    return None


def read_input(path):
    """Read the input file → list of rows: {"title", "artist", "query"}.

    Each row always carries a `query` (used for the Spotify search) plus the
    best-effort `title` / `artist` we could identify (for display in the PDF).
    """
    ext = os.path.splitext(path)[1].lower()

    # ---- PDF: extract the table (e.g. the artist/album export from the app) ----
    if ext == ".pdf":
        return _read_pdf(path)

    # ---- TXT: one free-text song per line ----
    if ext in (".txt", ""):
        rows = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append({"title": line, "artist": "", "query": line})
        return rows

    # ---- CSV ----
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        has_header = csv.Sniffer().has_header(sample) if sample.strip() else False

        if has_header:
            reader = csv.DictReader(f)
            title_col = _pick_column(reader.fieldnames, TITLE_KEYS)
            artist_col = _pick_column(reader.fieldnames, ARTIST_KEYS)
            rows = []
            for r in reader:
                # Fall back to positional if headers weren't recognizable.
                values = list(r.values())
                title = (r.get(title_col) if title_col else (values[0] if values else "")) or ""
                artist = (r.get(artist_col) if artist_col else (values[1] if len(values) > 1 else "")) or ""
                title, artist = title.strip(), artist.strip()
                if not title and not artist:
                    continue
                rows.append({"title": title, "artist": artist,
                             "query": _build_query(title, artist)})
            return rows

        # No header → assume col0=title, col1=artist
        reader = csv.reader(f)
        rows = []
        for cols in reader:
            if not cols or not any(c.strip() for c in cols):
                continue
            title = cols[0].strip() if len(cols) > 0 else ""
            artist = cols[1].strip() if len(cols) > 1 else ""
            rows.append({"title": title, "artist": artist,
                         "query": _build_query(title, artist)})
        return rows


def _read_pdf(path):
    """Extract title + artist rows from a PDF table (e.g. our app's export).

    Uses pdfplumber to read the table on every page. Recognises the columns by
    header name ("Song Title"/"Title" and "Artists"/"Artist"); falls back to the
    first two columns if headers aren't found.
    """
    try:
        import pdfplumber
    except ImportError:
        print("❌ Reading PDF input needs 'pdfplumber'. Install it:  pip install pdfplumber")
        sys.exit(1)

    rows = []
    header_map = None
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            start = 0
            # Detect a header row (contains something like "title"/"artist").
            first = [(c or "").lower().strip() for c in table[0]]
            if any("title" in c or "song" in c for c in first) and \
               any("artist" in c for c in first):
                ti = next((i for i, c in enumerate(first) if "title" in c or "song" in c), 0)
                ai = next((i for i, c in enumerate(first) if "artist" in c), 1)
                header_map = (ti, ai)
                start = 1
            ti, ai = header_map if header_map else (0, 1)
            for r in table[start:]:
                if not r:
                    continue
                title = (r[ti] if ti < len(r) else "") or ""
                artist = (r[ai] if ai < len(r) else "") or ""
                title = title.replace("\n", " ").strip()
                artist = artist.replace("\n", " ").strip()
                if not title and not artist:
                    continue
                # Skip a repeated header row if it appears on later pages.
                if title.lower() in ("song title", "title") and artist.lower() in ("artists", "artist"):
                    continue
                rows.append({"title": title, "artist": artist,
                             "query": _build_query(title, artist)})
    return rows


def _first_artist(artist):
    """Use the primary artist only (before a comma/&/feat) for a tighter match."""
    if not artist:
        return ""
    return re.split(r",|&|feat\.?|ft\.?|/", artist, flags=re.IGNORECASE)[0].strip()


def _build_query(title, artist):
    if title and artist:
        return f'{title} {artist}'
    return title or artist


# ─────────────────────────────────────────────
# ISRC LOOKUP (Spotify — same source isrcfinder.com uses)
# ─────────────────────────────────────────────

def find_isrc(sp, title, artist, query):
    """Return (isrc, matched_name, matched_artists) or ("", "", "").

    Tries a strict field-scoped search first, then falls back to looser
    queries so slightly-off titles still resolve.
    """
    attempts = []
    primary = _first_artist(artist)
    if title and primary:
        attempts.append(f'track:"{title}" artist:"{primary}"')
    if title and artist:
        attempts.append(f'{title} {artist}')
    if query:
        attempts.append(query)
    if title:
        attempts.append(title)

    seen = set()
    for q in attempts:
        if not q or q in seen:
            continue
        seen.add(q)
        # Pace via the shared rolling-window limiter so we never saturate
        # Spotify's ~30s window (this is what prevents a cooldown).
        _RATE.acquire()
        try:
            res = sp.search(q=q, type="track", limit=5)
        except SpotifyException as exc:
            status = getattr(exc, "http_status", None)
            if status == 429:
                retry = 5
                try:
                    retry = int(exc.headers.get("Retry-After", "5")) + 1
                except Exception:
                    pass
                if retry > 30:
                    # Long server-side cooldown — abort rather than hang/hammer.
                    raise RuntimeError(
                        f"Spotify rate-limit cooldown (~{retry}s). Stopping; "
                        "cached progress is saved. Try again later.")
                print(f"   ⏳ Rate limited (429). Waiting {retry}s...")
                time.sleep(retry)
                continue
            print(f"   ⚠️  search error for {q!r}: {exc}")
            time.sleep(1.0)
            continue
        except Exception as exc:
            print(f"   ⚠️  search error for {q!r}: {exc}")
            time.sleep(1.0)
            continue
        items = res.get("tracks", {}).get("items", [])
        if items:
            t = items[0]
            isrc = (t.get("external_ids") or {}).get("isrc", "")
            if isrc:
                return (isrc, t.get("name", ""),
                        ", ".join(a["name"] for a in t.get("artists", [])))
    return "", "", ""


def process(rows, sp):
    """Look up ISRC for every row; return list of result dicts.

    Rate-safe by design:
      • an on-disk ISRC cache means re-runs cost ZERO Spotify calls;
      • within-run de-duplication (same title|artist) avoids repeat lookups
        (artist catalogs repeat tracks across compilations);
      • the shared rolling-window limiter paces every actual search.
    """
    cache = load_isrc_cache()
    results = []
    total = len(rows)
    fetched_since_save = 0
    aborted = False
    for i, row in enumerate(rows, 1):
        title, artist, query = row["title"], row["artist"], row["query"]
        key = _cache_key(title, artist, query)

        if key in cache:
            entry = cache[key]
            isrc = entry.get("isrc", "")
            m_name = entry.get("matched_track", "")
            m_artists = entry.get("matched_artists", "")
            print(f"   ♻️  {i}/{total}  {title or query} → {isrc or 'NOT FOUND'} (cached)")
        elif aborted:
            isrc = m_name = m_artists = ""
        else:
            try:
                isrc, m_name, m_artists = find_isrc(sp, title, artist, query)
            except RuntimeError as exc:
                print(f"\n🛑 {exc}")
                save_isrc_cache(cache)
                aborted = True
                isrc = m_name = m_artists = ""
            else:
                status = "✅" if isrc else "❌"
                print(f"   {status} {i}/{total}  {title or query} → {isrc or 'NOT FOUND'}")
                # Cache the result (even NOT FOUND is cached to avoid re-querying).
                cache[key] = {"isrc": isrc, "matched_track": m_name,
                              "matched_artists": m_artists}
                fetched_since_save += 1
                if fetched_since_save >= 25:
                    save_isrc_cache(cache)
                    fetched_since_save = 0

        results.append({
            "title": title or m_name,
            "artist": artist or m_artists,
            "matched_track": m_name,
            "matched_artists": m_artists,
            "isrc": isrc or "NOT FOUND",
        })

    save_isrc_cache(cache)
    return results


# ─────────────────────────────────────────────
# OUTPUT (PDF + CSV) — dedicated to ISRC results
# ─────────────────────────────────────────────

def save_isrc_pdf(results, output_path, heading="ISRC Results"):
    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=8, leading=10)
    head = ParagraphStyle("head", parent=styles["BodyText"], fontSize=8.5,
                           leading=10, textColor=colors.white, fontName="Helvetica-Bold")

    doc = SimpleDocTemplate(
        output_path, pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
    )

    columns = ["title", "artist", "matched_track", "matched_artists", "isrc"]
    display = ["Title", "Artist", "Matched Track", "Matched Artists", "ISRC"]
    table_data = [[Paragraph(h, head) for h in display]]
    for r in results:
        table_data.append([Paragraph(str(r.get(c, "")), cell) for c in columns])

    page_width = landscape(A4)[0] - 24 * mm
    weights = {"title": 2.2, "artist": 2.2, "matched_track": 2.2,
               "matched_artists": 2.2, "isrc": 1.6}
    total = sum(weights[c] for c in columns)
    col_widths = [page_width * weights[c] / total for c in columns]

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1DB954")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    found = sum(1 for r in results if r.get("isrc") and r["isrc"] != "NOT FOUND")
    elements = [
        Paragraph(f"<b>{heading}</b>", styles["Title"]),
        Paragraph(f"{found}/{len(results)} ISRCs found", styles["Normal"]),
        Spacer(1, 6 * mm),
        table,
    ]
    doc.build(elements)
    return output_path


def save_isrc_csv(results, output_path):
    columns = ["title", "artist", "matched_track", "matched_artists", "isrc"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for r in results:
            writer.writerow({c: r.get(c, "") for c in columns})
    return output_path


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run(input_path, pdf_out=None, csv_out=None):
    if not os.path.exists(input_path):
        print(f"❌ Input file not found: {input_path}")
        sys.exit(1)

    rows = read_input(input_path)
    if not rows:
        print("❌ No rows found in the input file.")
        sys.exit(1)
    print(f"📥 Read {len(rows)} song(s) from {input_path}")

    sp, _ = get_spotify_client()
    print("🔎 Looking up ISRCs via Spotify...")
    results = process(rows, sp)

    base = os.path.splitext(os.path.basename(input_path))[0]
    pdf_out = pdf_out or f"{base}_isrc.pdf"
    save_isrc_pdf(results, pdf_out)
    print(f"📄 Saved PDF → {os.path.abspath(pdf_out)}")

    if csv_out:
        save_isrc_csv(results, csv_out)
        print(f"🧾 Saved CSV → {os.path.abspath(csv_out)}")

    found = sum(1 for r in results if r["isrc"] != "NOT FOUND")
    print(f"✅ Done — {found}/{len(results)} ISRCs found.")
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Batch ISRC finder (CSV/TXT of title+artist → ISRC column → PDF).")
    parser.add_argument("input", help="Input CSV or TXT file (title + artist).")
    parser.add_argument("-o", "--output", help="Output PDF path.")
    parser.add_argument("--csv-out", help="Also write results to this CSV path.")
    args = parser.parse_args()
    run(args.input, pdf_out=args.output, csv_out=args.csv_out)


if __name__ == "__main__":
    main()

