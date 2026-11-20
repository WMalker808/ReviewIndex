# The TV Review Index

A searchable index of Guardian TV reviews, backed by the Content API (CAPI).

## Files

- `server.py` - Flask app plus the CAPI sync. Reviews and reader ratings live in
  SQLite (`reviews.db`).
- `index.html` - the page. Served by `server.py` it calls `/api/meta`,
  `/api/reviews` and `/api/ratings`. Opened straight from disk it falls back to
  its built-in sample data.
- `render.yaml` - Render Blueprint for deployment.

## Run it locally

```
pip install -r requirements.txt
export GUARDIAN_API_KEY=your-capi-key
python3 server.py
```

Open http://127.0.0.1:5000. The first start pulls the whole archive in the
background (a minute or two); reviews appear as each year lands. After that the
server tops up from CAPI every 30 minutes.

```
python3 server.py sync          # top up now, without starting the web server
python3 server.py sync --full   # re-pull everything
```

## Deploy on Render

1. Push these files to a GitHub or GitLab repo, with `render.yaml` at the root.
2. In Render: **New -> Blueprint**, pick the repo, and apply.
3. Render prompts for `GUARDIAN_API_KEY` (it is the one variable marked
   `sync: false`, so it is never committed). Paste your CAPI key.
4. First deploy takes a couple of minutes to build, then the archive syncs in the
   background. `GET /healthz` reports progress: `{"reviews": 1234, "syncing": true}`.

The blueprint sets up a single web service:

- **Start command** `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 ... server:app`.
  One worker is deliberate - one process owns the SQLite file and the sync loop,
  and the 8 threads handle the read-mostly API. Don't raise `--workers`; raise
  `--threads`, or move to Postgres first.
- **Health check** `/healthz`, which stays healthy while the index is still filling.
- **Persistent disk** 1 GB at `/var/data`, with `DB_PATH=/var/data/reviews.db`, so
  the index and reader ratings survive deploys and restarts.
- **Region** `frankfurt`. Change it if your readers are elsewhere.

### The free plan

Render doesn't allow persistent disks on free instances. To run this on `plan: free`,
delete the `disk:` block and the `DB_PATH` variable from `render.yaml`. It works,
with two consequences:

- Every deploy and restart wipes the database, so **reader ratings are lost** and
  the whole archive re-syncs from CAPI (a few hundred requests).
- Free services sleep after 15 minutes of inactivity. The sync loop sleeps with
  them and resumes on the next request.

Fine for showing the POC around; the disk is worth it for anything with readers
on it.

### Settings (environment variables)

| Variable | Default | Purpose |
| --- | --- | --- |
| `GUARDIAN_API_KEY` | none | CAPI key. Without it the server serves whatever is already indexed. |
| `DB_PATH` | `reviews.db` next to `server.py` | SQLite file. Point this at the mounted disk in production. |
| `SYNC_INTERVAL_MINUTES` | `30` | Gap between background top-ups. |
| `SYNC_ON_START` | `1` | Set to `0` to serve without syncing at all. |
| `CAPI_BASE_URL` | `https://content.guardianapis.com` | Point at a different CAPI host. |
| `CAPI_TAG` | `tv-and-radio/series/tv-review` | Tag expression defining a "TV review". |
| `CAPI_FIRST_YEAR` | `1999` | Earliest year a full sync asks for. |
| `PORT` | `5000` | Render sets this itself. |

Render's default Python is recent enough; pin it with a `PYTHON_VERSION` variable
if you need a specific one.

## Known limits

- Reader ratings are one per browser, held against an anonymous cookie. Clearing
  cookies allows a second vote; a real launch would tie ratings to signed-in readers.
- SQLite on one instance means the service can't scale horizontally. Moving the
  two tables to Render Postgres is the next step if it needs to.
- The programme name is taken from the headline ("X review - ..."). Older
  headlines that don't follow that pattern show the full headline instead.
- Search is a simple all-words match over programme, headline and standfirst.
