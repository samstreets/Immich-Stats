import asyncio
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _pick_db_path() -> Path:
    for candidate in [Path("/data"), Path("/app/data")]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            test = candidate / ".write_test"
            test.write_text("ok")
            test.unlink()
            log.info("Using database directory: %s", candidate)
            return candidate / "immich_history.db"
        except Exception as e:
            log.warning("Cannot use %s: %s", candidate, e)
    fallback = Path(__file__).parent / "immich_history.db"
    log.warning("Falling back to local DB: %s", fallback)
    return fallback


DB_PATH = _pick_db_path()

HERE = Path(__file__).parent
SNAPSHOT_INTERVAL = 60 * 60  # 1 hour

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_db_conn: Optional[sqlite3.Connection] = None


def get_db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")
        log.info("Opened database at %s", DB_PATH)
    return _db_conn


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    INTEGER NOT NULL,
            disk_used    INTEGER,
            disk_free    INTEGER,
            disk_total   INTEGER,
            disk_pct     REAL,
            total_assets INTEGER,
            total_photos INTEGER,
            total_videos INTEGER,
            total_users  INTEGER,
            version      TEXT
        );
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    db.commit()
    cols = {row[1] for row in db.execute("PRAGMA table_info(snapshots)")}
    if "disk_pct" not in cols:
        db.execute("ALTER TABLE snapshots ADD COLUMN disk_pct REAL")
        db.commit()
    log.info("Database initialised")


def get_config(key: str) -> Optional[str]:
    row = get_db().execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_config(key: str, value: str):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
    db.commit()
    log.info("Config saved: %s", key)


def insert_snapshot(**kwargs):
    db = get_db()
    db.execute(
        """INSERT INTO snapshots
           (timestamp, disk_used, disk_free, disk_total, disk_pct,
            total_assets, total_photos, total_videos, total_users, version)
           VALUES (:timestamp, :disk_used, :disk_free, :disk_total, :disk_pct,
                   :total_assets, :total_photos, :total_videos, :total_users, :version)""",
        {
            "timestamp": int(time.time() * 1000),
            **kwargs,
        },
    )
    db.commit()


# ---------------------------------------------------------------------------
# Retention pruning
# ---------------------------------------------------------------------------


def prune_snapshots():
    """Keep full resolution for 30 days, then one per week."""
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - (30 * 24 * 60 * 60 * 1000)
    db = get_db()
    old_rows = db.execute(
        "SELECT id, timestamp FROM snapshots WHERE timestamp < ? ORDER BY timestamp ASC",
        (cutoff_ms,),
    ).fetchall()
    if not old_rows:
        return
    weeks: dict = {}
    for row in old_rows:
        dt = datetime.fromtimestamp(row["timestamp"] / 1000, tz=timezone.utc)
        week_key = dt.isocalendar()[:2]
        if week_key not in weeks:
            weeks[week_key] = row["id"]
    keep_ids = set(weeks.values())
    delete_ids = [row["id"] for row in old_rows if row["id"] not in keep_ids]
    if delete_ids:
        db.execute(
            "DELETE FROM snapshots WHERE id IN ({})".format(
                ",".join("?" * len(delete_ids))
            ),
            delete_ids,
        )
        db.commit()
        log.info("Pruned %d snapshots, kept %d weekly", len(delete_ids), len(keep_ids))


# ---------------------------------------------------------------------------
# Immich API helpers
# ---------------------------------------------------------------------------


def make_client(immich_url: str, api_key: str) -> httpx.AsyncClient:
    base = immich_url.rstrip("/") + "/api"
    return httpx.AsyncClient(base_url=base, headers={"x-api-key": api_key}, timeout=10)


async def fetch(client: httpx.AsyncClient, path: str):
    r = await client.get(path)
    r.raise_for_status()
    return r.json()


async def safe_fetch(client: httpx.AsyncClient, path: str):
    try:
        return await fetch(client, path)
    except Exception as e:
        log.debug("Optional fetch %s failed: %s", path, e)
        return None


def get_asset_count(person: dict) -> int:
    return (
        person.get("assetCount")
        or person.get("assets")
        or person.get("numberOfAssets")
        or person.get("count")
        or 0
    )


def summarise_jobs(jobs_raw: dict) -> list:
    LABELS = {
        "thumbnailGeneration": "Thumbnails",
        "metadataExtraction": "Metadata",
        "videoConversion": "Video Transcode",
        "faceDetection": "Face Detection",
        "facialRecognition": "Face Recognition",
        "smartSearch": "Smart Search",
        "duplicateDetection": "Duplicates",
        "backgroundTask": "Background Tasks",
        "storageTemplateMigration": "Storage Migration",
        "migration": "Migration",
        "search": "Search Index",
        "sidecar": "Sidecar",
        "library": "Library Scan",
    }
    result = []
    for key, data in jobs_raw.items():
        if not isinstance(data, dict):
            continue
        counts = data.get("jobCounts", {})
        status = data.get("queueStatus", {})
        result.append(
            {
                "name": LABELS.get(key, key),
                "key": key,
                "active": counts.get("active", 0),
                "waiting": counts.get("waiting", 0),
                "failed": counts.get("failed", 0),
                "completed": counts.get("completed", 0),
                "paused": status.get("isPaused", False),
            }
        )
    result.sort(key=lambda j: (-j["active"], -j["waiting"], j["name"]))
    return result


async def take_snapshot():
    url = get_config("immich_url")
    key = get_config("api_key")
    if not url or not key:
        return
    try:
        async with make_client(url, key) as client:
            about, storage, stats = await asyncio.gather(
                fetch(client, "/server/about"),
                fetch(client, "/server/storage"),
                fetch(client, "/server/statistics"),
            )
        photos = stats.get("photos", 0)
        videos = stats.get("videos", 0)
        insert_snapshot(
            disk_used=storage.get("diskUseRaw", 0),
            disk_free=storage.get("diskAvailableRaw", 0),
            disk_total=storage.get("diskSizeRaw", 0),
            disk_pct=storage.get("diskUsagePercentage", 0.0),
            total_assets=photos + videos,
            total_photos=photos,
            total_videos=videos,
            total_users=0,
            version=about.get("version", ""),
        )
        log.info("Hourly snapshot saved")
    except Exception as exc:
        log.warning("Snapshot failed: %s", exc)


async def snapshot_loop():
    while True:
        await asyncio.sleep(SNAPSHOT_INTERVAL)
        await take_snapshot()


async def cleanup_loop():
    while True:
        await asyncio.sleep(24 * 60 * 60)
        try:
            prune_snapshots()
        except Exception as exc:
            log.warning("Prune failed: %s", exc)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    prune_snapshots()
    snapshot_task = asyncio.create_task(snapshot_loop())
    cleanup_task = asyncio.create_task(cleanup_loop())
    log.info("Immich Monitor started on :8765  |  DB: %s", DB_PATH)
    yield
    snapshot_task.cancel()
    cleanup_task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ConfigIn(BaseModel):
    immich_url: str
    api_key: str


class TestIn(BaseModel):
    immich_url: str
    api_key: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/api/config")
def api_get_config():
    url = get_config("immich_url") or ""
    key = get_config("api_key") or ""
    log.info("GET /api/config  configured=%s  url=%s", bool(url and key), url)
    return {"immich_url": url, "configured": bool(url and key)}


@app.post("/api/config")
def api_set_config(body: ConfigIn):
    if not body.immich_url.strip() or not body.api_key.strip():
        raise HTTPException(status_code=400, detail="Missing fields")
    set_config("immich_url", body.immich_url.strip())
    set_config("api_key", body.api_key.strip())
    log.info("POST /api/config  url=%s", body.immich_url.strip())
    return {"success": True}


@app.get("/api/status")
async def api_status():
    url = get_config("immich_url")
    key = get_config("api_key")
    if not url or not key:
        raise HTTPException(
            status_code=400,
            detail="Not configured — save your connection details first",
        )

    log.info("Fetching status from %s", url)
    try:
        async with make_client(url, key) as client:
            about, storage, stats = await asyncio.gather(
                fetch(client, "/server/about"),
                fetch(client, "/server/storage"),
                fetch(client, "/server/statistics"),
            )
            users_raw, albums_raw, jobs_raw, people_raw = await asyncio.gather(
                safe_fetch(client, "/users"),
                safe_fetch(client, "/albums"),
                safe_fetch(client, "/jobs"),
                safe_fetch(client, "/people?withCount=true"),
            )
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot connect to Immich: {exc}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Immich timed out")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    photos = stats.get("photos", 0)
    videos = stats.get("videos", 0)
    disk_pct = storage.get("diskUsagePercentage", 0.0)

    # Users
    total_users = len(users_raw) if isinstance(users_raw, list) else 0
    user_stats = []
    if isinstance(users_raw, list):
        for u in users_raw:
            user_stats.append(
                {
                    "name": u.get("name", u.get("email", "Unknown")),
                    "email": u.get("email", ""),
                    "quotaUsed": u.get("quotaUsageInBytes", 0),
                    "quotaTotal": u.get("quotaSizeInBytes"),
                }
            )
        user_stats.sort(key=lambda u: u["quotaUsed"], reverse=True)

    # Albums
    total_albums = len(albums_raw) if isinstance(albums_raw, list) else 0

    # Jobs
    jobs = summarise_jobs(jobs_raw) if isinstance(jobs_raw, dict) else []
    active_jobs = sum(j["active"] for j in jobs)
    failed_jobs = sum(j["failed"] for j in jobs)
    waiting_jobs = sum(j["waiting"] for j in jobs)

    # People
    people_list = []
    if isinstance(people_raw, dict):
        people_list = people_raw.get("people", [])
    elif isinstance(people_raw, list):
        people_list = people_raw

    named = [p for p in people_list if p.get("name")]
    total_people = len(named)

    top_candidates = named[:5]

    async def fetch_person_count(client, person):
        try:
            detail = await fetch(client, f"/people/{person['id']}")
            return {**person, "assetCount": detail.get("assetCount", 0)}
        except Exception:
            return {**person, "assetCount": 0}

    top_people = []
    if top_candidates:
        async with make_client(url, key) as pclient:
            top_people = await asyncio.gather(
                *[fetch_person_count(pclient, p) for p in top_candidates]
            )
        top_people = sorted(
            top_people, key=lambda p: p.get("assetCount", 0), reverse=True
        )
        log.info("Top people: %s", [(p["name"], p["assetCount"]) for p in top_people])

    insert_snapshot(
        disk_used=storage.get("diskUseRaw", 0),
        disk_free=storage.get("diskAvailableRaw", 0),
        disk_total=storage.get("diskSizeRaw", 0),
        disk_pct=disk_pct,
        total_assets=photos + videos,
        total_photos=photos,
        total_videos=videos,
        total_users=total_users,
        version=about.get("version", ""),
    )

    return {
        "diskUseRaw": storage.get("diskUseRaw", 0),
        "diskAvailableRaw": storage.get("diskAvailableRaw", 0),
        "diskSizeRaw": storage.get("diskSizeRaw", 0),
        "diskUsagePercentage": disk_pct,
        "version": about.get("version", ""),
        "build": about.get("build", ""),
        "stats": stats,
        "totalUsers": total_users,
        "userStats": user_stats,
        "totalAlbums": total_albums,
        "jobs": jobs,
        "activeJobs": active_jobs,
        "failedJobs": failed_jobs,
        "waitingJobs": waiting_jobs,
        "topPeople": top_people,
        "totalPeople": total_people,
    }


@app.get("/api/history")
def api_history(limit: int = 500):
    rows = (
        get_db()
        .execute("SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT ?", (limit,))
        .fetchall()
    )
    return [dict(r) for r in reversed(rows)]


@app.get("/api/history/range")
def api_history_range(from_ts: int = 0, to_ts: Optional[int] = None):
    if to_ts is None:
        to_ts = int(time.time() * 1000)
    rows = (
        get_db()
        .execute(
            "SELECT * FROM snapshots WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
            (from_ts, to_ts),
        )
        .fetchall()
    )
    return [dict(r) for r in rows]


@app.get("/api/debug/people")
async def api_debug_people():
    url = get_config("immich_url")
    key = get_config("api_key")
    if not url or not key:
        raise HTTPException(status_code=400, detail="Not configured")
    async with make_client(url, key) as client:
        raw = await fetch(client, "/people?withCount=true")
    if isinstance(raw, dict):
        sample = raw.get("people", [])[:3]
        return {
            "type": "dict",
            "keys": list(raw.keys()),
            "sample_fields": [list(p.keys()) for p in sample],
            "sample": sample,
        }
    elif isinstance(raw, list):
        return {"type": "list", "length": len(raw), "sample": raw[:3]}
    return {"type": str(type(raw)), "raw": raw}


@app.get("/api/debug/db")
def api_debug_db():
    try:
        url = get_config("immich_url")
        key = get_config("api_key")
        count = get_db().execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        return {
            "db_path": str(DB_PATH),
            "db_exists": DB_PATH.exists(),
            "snapshot_count": count,
            "configured": bool(url and key),
            "immich_url": url or "",
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/test")
async def api_test(body: TestIn):
    try:
        async with make_client(body.immich_url, body.api_key) as client:
            about = await fetch(client, "/server/about")
        return {"ok": True, "version": about.get("version", "unknown")}
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"ok": False, "error": str(exc)})
