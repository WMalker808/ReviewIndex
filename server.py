"""TV Review Index - Flask backend.

Pulls Guardian TV reviews from the Content API (CAPI) into a local SQLite
index and serves the three endpoints the page calls:

    GET  /api/meta      archive count, date span, reviewer list
    GET  /api/reviews   search / filter / sort / paginate
    POST /api/ratings   save a reader's star rating

Run locally:
    export GUARDIAN_API_KEY=...        # your CAPI key
    python3 server.py                  # serve on http://127.0.0.1:5000 and sync in the background
    python3 server.py sync             # one-off incremental sync, no web server
    python3 server.py sync --full      # re-pull the whole archive

In production (Render and anywhere else) a WSGI server runs the app directly:
    gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 server:app
The background sync starts on import, so it runs under gunicorn too. Keep it to
one worker: a single process owns the SQLite file and the sync loop.
"""

from __future__ import annotations

import html
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent

# --- Configuration (all overridable by environment variable) ----------------
CAPI_KEY = os.environ.get("GUARDIAN_API_KEY", "").strip()
CAPI_BASE_URL = os.environ.get("CAPI_BASE_URL", "https://content.guardianapis.com").rstrip("/")
# Which content counts as a "TV review". Any CAPI tag expression works here,
# e.g. "tv-and-radio/tv-and-radio,tone/reviews" (comma = AND, pipe = OR).
CAPI_TAG = os.environ.get("CAPI_TAG", "tv-and-radio/series/tv-review")
CAPI_FIRST_YEAR = int(os.environ.get("CAPI_FIRST_YEAR", "1999"))
CAPI_PAGE_SIZE = 200  # CAPI maximum
CAPI_PAUSE_SECONDS = float(os.environ.get("CAPI_PAUSE_SECONDS", "0.25"))
SYNC_INTERVAL_MINUTES = float(os.environ.get("SYNC_INTERVAL_MINUTES", "30"))
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "reviews.db"))
PORT = int(os.environ.get("PORT", "5000"))
# Set SYNC_ON_START=0 to serve without syncing, e.g. when a scheduled job owns the sync.
SYNC_ON_START = os.environ.get("SYNC_ON_START", "1") != "0"

READER_COOKIE = "tvri_reader"
READER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

log = logging.getLogger("tv-review-index")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id            TEXT PRIMARY KEY,   -- CAPI content id (the URL path)
    show_title    TEXT NOT NULL,
    headline      TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    published_at  TEXT NOT NULL,      -- ISO 8601, UTC
    rating        INTEGER,            -- critic stars 0-5, NULL when the review has none
    author        TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL,
    thumbnail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS reviews_published ON reviews (published_at);
CREATE INDEX IF NOT EXISTS reviews_author ON reviews (author);

CREATE TABLE IF NOT EXISTS reader_ratings (
    review_id   TEXT NOT NULL REFERENCES reviews (id) ON DELETE CASCADE,
    reader_id   TEXT NOT NULL,
    rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (review_id, reader_id)
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Sorts and grouped subqueries stay in RAM rather than spilling to temp files
    # on whatever TMPDIR happens to be, and a reader waits rather than failing
    # while the sync thread holds a write.
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    return conn


# --- CAPI -> local record ----------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
# "Slow Horses season six review – it gets harder ..." -> "Slow Horses season six"
TITLE_RE = re.compile(r"^(?P<title>.+?)\s+review\s*(?:[–—:|]|\s-\s)\s*", re.IGNORECASE)
TITLE_ONLY_RE = re.compile(r"^(?P<title>.+?)\s+review$", re.IGNORECASE)
# Older house style: "TV review: Sherlock" / "Last night's TV: Sherlock"
PREFIX_RE = re.compile(r"^(?:tv review|last night[’']s tv|the weekend[’']s tv|tv)\s*[:|]\s*", re.IGNORECASE)


def plain_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub("", value or ""))).strip()


def show_title_from(headline: str) -> str:
    for pattern in (TITLE_RE, TITLE_ONLY_RE):
        match = pattern.match(headline)
        if match:
            return match.group("title").strip()
    stripped = PREFIX_RE.sub("", headline).strip()
    return stripped or headline


def parse_star_rating(value) -> int | None:
    try:
        rating = int(value)
    except (TypeError, ValueError):
        return None
    return rating if 0 <= rating <= 5 else None


def record_from_capi(item: dict) -> dict:
    fields = item.get("fields") or {}
    headline = plain_text(fields.get("headline") or item.get("webTitle"))
    author = plain_text(fields.get("byline"))
    if not author:
        author = ", ".join(
            tag.get("webTitle", "") for tag in item.get("tags") or [] if tag.get("type") == "contributor"
        )
    return {
        "id": item["id"],
        "show_title": show_title_from(headline),
        "headline": headline,
        "summary": plain_text(fields.get("trailText")),
        "published_at": item["webPublicationDate"],
        "rating": parse_star_rating(fields.get("starRating")),
        "author": author,
        "url": item["webUrl"],
        "thumbnail": fields.get("thumbnail") or "",
    }


class CapiError(RuntimeError):
    pass


def capi_search(session: requests.Session, **params) -> dict:
    """One call to CAPI /search, with retry on rate limiting and 5xx."""
    query = {
        "api-key": CAPI_KEY,
        "tag": CAPI_TAG,
        "show-fields": "headline,trailText,byline,starRating,thumbnail",
        "show-tags": "contributor",
        "page-size": CAPI_PAGE_SIZE,
        "order-by": "oldest",
        **params,
    }
    for attempt in range(5):
        response = session.get(f"{CAPI_BASE_URL}/search", params=query, timeout=30)
        if response.status_code == 429 or response.status_code >= 500:
            wait = 2 ** attempt
            log.warning("CAPI returned %s; retrying in %ss", response.status_code, wait)
            time.sleep(wait)
            continue
        if response.status_code in (401, 403):
            raise CapiError(f"CAPI rejected the API key (HTTP {response.status_code}).")
        if not response.ok:
            raise CapiError(f"CAPI error HTTP {response.status_code}: {response.text[:200]}")
        body = response.json().get("response") or {}
        if body.get("status") != "ok":
            raise CapiError(f"CAPI error: {body.get('message') or body}")
        return body
    raise CapiError("CAPI kept failing after 5 attempts.")


UPSERT = """
INSERT INTO reviews (id, show_title, headline, summary, published_at, rating, author, url, thumbnail)
VALUES (:id, :show_title, :headline, :summary, :published_at, :rating, :author, :url, :thumbnail)
ON CONFLICT (id) DO UPDATE SET
    show_title = excluded.show_title, headline = excluded.headline, summary = excluded.summary,
    published_at = excluded.published_at, rating = excluded.rating, author = excluded.author,
    url = excluded.url, thumbnail = excluded.thumbnail
"""

_sync_lock = threading.Lock()


def sync(full: bool = False) -> int:
    """Pull reviews from CAPI into SQLite. Returns the number of items written.

    Incremental by default: starts a few days before the newest review already
    stored, so late edits (a corrected star rating, say) are picked up. The
    archive is walked one calendar year at a time so no single query pages deep.
    """
    if not CAPI_KEY:
        raise CapiError("GUARDIAN_API_KEY is not set.")
    if not _sync_lock.acquire(blocking=False):
        log.info("Sync already running; skipping.")
        return 0
    conn = connect()
    try:
        latest = conn.execute("SELECT MAX(published_at) FROM reviews").fetchone()[0]
        if full or not latest:
            start = date(CAPI_FIRST_YEAR, 1, 1)
        else:
            start = datetime.fromisoformat(latest.replace("Z", "+00:00")).date() - timedelta(days=3)
        today = datetime.now(timezone.utc).date()
        written = 0
        session = requests.Session()
        for year in range(start.year, today.year + 1):
            window_from = max(start, date(year, 1, 1))
            window_to = min(today, date(year, 12, 31))
            page = 1
            while True:
                body = capi_search(
                    session,
                    **{"from-date": window_from.isoformat(), "to-date": window_to.isoformat(), "page": page},
                )
                results = body.get("results") or []
                with conn:
                    conn.executemany(UPSERT, [record_from_capi(item) for item in results])
                written += len(results)
                if page >= int(body.get("pages") or 0):
                    break
                page += 1
                time.sleep(CAPI_PAUSE_SECONDS)
            if results or page > 1:
                log.info("Synced %s: %s items so far", year, written)
            time.sleep(CAPI_PAUSE_SECONDS)
        total = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        log.info("Sync complete: %s written, %s reviews indexed.", written, total)
        return written
    finally:
        conn.close()
        _sync_lock.release()


_sync_thread: threading.Thread | None = None
_sync_thread_lock = threading.Lock()


def start_background_sync() -> None:
    """Start the periodic CAPI sync. Safe to call more than once per process."""
    global _sync_thread
    with _sync_thread_lock:
        if _sync_thread is not None and _sync_thread.is_alive():
            return
        if not CAPI_KEY:
            log.warning("GUARDIAN_API_KEY is not set: serving the existing index without syncing from CAPI.")
            return
        _sync_thread = threading.Thread(target=sync_forever, name="capi-sync", daemon=True)
        _sync_thread.start()


def sync_forever() -> None:
    while True:
        try:
            sync()
        except Exception:  # keep the web server up whatever CAPI does
            log.exception("Sync failed")
        time.sleep(SYNC_INTERVAL_MINUTES * 60)


# --- Web app -----------------------------------------------------------------

app = Flask(__name__, static_folder=None)
# Render, like any reverse proxy, terminates TLS and forwards the original
# scheme and client IP in X-Forwarded-* headers. Without this, request.is_secure
# is False behind the proxy and the reader cookie never gets its Secure flag.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def reader_id() -> str:
    """Anonymous per-browser id, used to hold one rating per reader per review."""
    if "reader_id" not in g:
        value = request.cookies.get(READER_COOKIE, "")
        try:
            g.reader_id = str(uuid.UUID(value))
        except ValueError:
            g.reader_id = str(uuid.uuid4())
            g.set_reader_cookie = True
    return g.reader_id


@app.after_request
def persist_reader_cookie(response):
    if g.get("set_reader_cookie"):
        response.set_cookie(
            READER_COOKIE, g.reader_id, max_age=READER_COOKIE_MAX_AGE,
            httponly=True, samesite="Lax", secure=request.is_secure,
        )
    return response


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.errorhandler(sqlite3.Error)
def database_error(error: sqlite3.Error):
    """Return the database's own message instead of a bare 500, and log the traceback."""
    log.exception("Database error serving %s", request.path)
    return jsonify(error="Database error.", detail=f"{type(error).__name__}: {error}"), 500


def disk_free_mb() -> float | None:
    try:
        usage = shutil.disk_usage(DB_PATH.parent)
        return round(usage.free / 1_000_000, 1)
    except OSError:
        return None


@app.get("/healthz")
def healthz():
    """Readiness probe. Reports the index size but stays healthy while it is still filling."""
    try:
        count = db().execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        ratings = db().execute("SELECT COUNT(*) FROM reader_ratings").fetchone()[0]
    except sqlite3.Error as error:
        return jsonify(status="error", error=f"{type(error).__name__}: {error}"), 503
    syncing = _sync_thread is not None and _sync_thread.is_alive()
    return jsonify(status="ok", reviews=count, ratings=ratings, syncing=syncing,
                   diskFreeMb=disk_free_mb())


@app.get("/healthz/integrity")
def integrity():
    """On-demand PRAGMA integrity_check. Slow on a large index; not part of the health check."""
    rows = [row[0] for row in db().execute("PRAGMA integrity_check")]
    return jsonify(ok=rows == ["ok"], result=rows[:20], diskFreeMb=disk_free_mb())


@app.get("/api/meta")
def meta():
    conn = db()
    count, earliest, latest = conn.execute(
        "SELECT COUNT(*), MIN(published_at), MAX(published_at) FROM reviews"
    ).fetchone()
    reviewers = [
        row[0] for row in conn.execute(
            "SELECT author FROM reviews WHERE author != '' GROUP BY author ORDER BY author COLLATE NOCASE"
        )
    ]
    return jsonify(count=count, earliest=earliest, latest=latest, reviewers=reviewers)


SORTS = {
    "newest": "r.published_at DESC",
    "oldest": "r.published_at ASC",
    # Unrated reviews always sort after rated ones, in both directions.
    "critic_highest": "r.rating IS NULL, r.rating DESC, r.published_at DESC",
    "critic_lowest": "r.rating IS NULL, r.rating ASC, r.published_at DESC",
    "reader_highest": "agg.average IS NULL, agg.average DESC, agg.votes DESC, r.published_at DESC",
    "reader_lowest": "agg.average IS NULL, agg.average ASC, agg.votes DESC, r.published_at DESC",
}
SORTS["highest"] = SORTS["critic_highest"]
SORTS["lowest"] = SORTS["critic_lowest"]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except ValueError:
        return default


@app.get("/api/reviews")
def reviews():
    args = request.args
    where, params = [], []

    # Every word must appear somewhere in the programme title, headline or standfirst.
    for term in args.get("q", "").split()[:8]:
        where.append(
            "(r.show_title LIKE ? ESCAPE '\\' OR r.headline LIKE ? ESCAPE '\\' OR r.summary LIKE ? ESCAPE '\\')"
        )
        params += [like_pattern(term)] * 3

    wanted = [value for value in args.get("rating", "").split(",") if value]
    stars = sorted({int(value) for value in wanted if value in {"0", "1", "2", "3", "4", "5"}})
    rating_clauses = []
    if stars:
        rating_clauses.append(f"r.rating IN ({','.join('?' * len(stars))})")
        params += stars
    if "unrated" in wanted:
        rating_clauses.append("r.rating IS NULL")
    if rating_clauses:
        where.append(f"({' OR '.join(rating_clauses)})")

    if args.get("reviewer"):
        where.append("r.author = ?")
        params.append(args["reviewer"])
    if DATE_RE.match(args.get("from", "")):
        where.append("substr(r.published_at, 1, 10) >= ?")
        params.append(args["from"])
    if DATE_RE.match(args.get("to", "")):
        where.append("substr(r.published_at, 1, 10) <= ?")
        params.append(args["to"])

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    order_sql = SORTS.get(args.get("sort", "newest"), SORTS["newest"])
    page_size = max(1, min(48, int_arg("page_size", 12)))

    conn = db()
    total = conn.execute(f"SELECT COUNT(*) FROM reviews r {where_sql}", params).fetchone()[0]
    pages = max(1, -(-total // page_size))
    page = max(1, min(pages, int_arg("page", 1)))

    rows = conn.execute(
        f"""
        SELECT r.*, agg.average AS reader_rating, COALESCE(agg.votes, 0) AS reader_votes,
               mine.rating AS user_rating
        FROM reviews r
        LEFT JOIN (
            SELECT review_id, AVG(rating) AS average, COUNT(*) AS votes
            FROM reader_ratings GROUP BY review_id
        ) agg ON agg.review_id = r.id
        LEFT JOIN reader_ratings mine ON mine.review_id = r.id AND mine.reader_id = ?
        {where_sql}
        ORDER BY {order_sql}, r.id
        LIMIT ? OFFSET ?
        """,
        [request.cookies.get(READER_COOKIE, ""), *params, page_size, (page - 1) * page_size],
    ).fetchall()

    items = [
        {
            "id": row["id"],
            "showTitle": row["show_title"],
            "headline": row["headline"],
            "summary": row["summary"],
            "publishedAt": row["published_at"],
            "rating": row["rating"],
            "author": row["author"],
            "url": row["url"],
            "thumbnail": row["thumbnail"],
            "readerRating": round(row["reader_rating"], 2) if row["reader_rating"] is not None else None,
            "readerRatingCount": row["reader_votes"],
            "userRating": row["user_rating"],
        }
        for row in rows
    ]
    return jsonify(items=items, page=page, pageSize=page_size, pages=pages, total=total)


@app.post("/api/ratings")
def rate():
    payload = request.get_json(silent=True) or {}
    review_id = payload.get("reviewId")
    rating = payload.get("rating")
    conn = db()
    valid = (
        isinstance(review_id, str)
        and isinstance(rating, int) and not isinstance(rating, bool) and 1 <= rating <= 5
        and conn.execute("SELECT 1 FROM reviews WHERE id = ?", (review_id,)).fetchone()
    )
    if not valid:
        return jsonify(error="Invalid rating."), 400

    with conn:
        conn.execute(
            """
            INSERT INTO reader_ratings (review_id, reader_id, rating, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (review_id, reader_id) DO UPDATE SET
                rating = excluded.rating, updated_at = excluded.updated_at
            """,
            (review_id, reader_id(), rating, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    average, votes = conn.execute(
        "SELECT AVG(rating), COUNT(*) FROM reader_ratings WHERE review_id = ?", (review_id,)
    ).fetchone()
    return jsonify(reviewId=review_id, readerRating=round(average, 2), readerRatingCount=votes, userRating=rating)


# Under a WSGI server main() never runs, so set up at import time instead.
# `server.py sync` skips this: that path does its own one-off sync and exits.
if sys.argv[1:2] != ["sync"]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    connect().close()  # create the database and schema before the first request
    if SYNC_ON_START:
        start_background_sync()


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if argv[:1] == ["sync"]:
        try:
            sync(full="--full" in argv)
        except CapiError as error:
            log.error("%s", error)
            return 1
        return 0

    if SYNC_ON_START:
        start_background_sync()
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=PORT, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
