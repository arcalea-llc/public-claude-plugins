#!/usr/bin/env python3
"""
Token Tracker Worker — persistent daemon process.

Owns the SQLite database and serves the HTTP dashboard + ingest endpoint.
Spawned by mcp-server.py on first session; survives across Claude Code
session restarts. Identified by a PID file at ~/.claude/token-tracker/worker.pid.

Usage:
    python3 worker.py              # foreground (for debugging)
    python3 worker.py --daemon     # detached background process
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import socket
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = Path.home() / ".claude" / "token-tracker"
DB_PATH = DATA_DIR / "token_tracker.db"
PORT_FILE = DATA_DIR / "dashboard_port"
PID_FILE = DATA_DIR / "worker.pid"
LOG_PATH = DATA_DIR / "worker.log"
PLUGIN_ROOT = Path(__file__).parent.parent
ASSETS_DIR = PLUGIN_ROOT / "assets"

DEFAULT_PORT = int(os.environ.get("TOKEN_TRACKER_PORT", "47700"))

os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("worker")

# Thread lock for DB writes
_db_lock = threading.Lock()


def _read_plugin_version():
    """Read version from plugin.json."""
    pj = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(pj.read_text(encoding="utf-8"))["version"]
    except Exception:
        return "unknown"


WORKER_VERSION = _read_plugin_version()

# ---------------------------------------------------------------------------
# Model pricing (per million tokens)
# ---------------------------------------------------------------------------

# Builtin defaults — updated with each plugin release
_BUILTIN_PRICING = {
    "opus": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_creation": 18.75},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_creation": 3.75},
    "haiku": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_creation": 1.0},
}

PRICING_FILE = DATA_DIR / "pricing.json"


def _load_pricing():
    """Load pricing: local override > builtin fallback.

    Local override: ~/.claude/token-tracker/pricing.json
    """
    if PRICING_FILE.exists():
        try:
            data = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and any(k in data for k in ("opus", "sonnet", "haiku")):
                log.info("Using local pricing override from %s", PRICING_FILE)
                return data
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Bad local pricing file, ignoring: %s", e)

    return _BUILTIN_PRICING


# Loaded once at startup, refreshed on next startup after cache expires
MODEL_PRICING = _load_pricing()
DEFAULT_PRICING = MODEL_PRICING.get("sonnet", _BUILTIN_PRICING["sonnet"])


def _classify_model(model_str):
    """Normalize a model string like 'claude-opus-4-6' to 'opus'."""
    if not model_str:
        return "unknown"
    m = model_str.lower()
    for family in ("opus", "sonnet", "haiku"):
        if family in m:
            return family
    return "unknown"


def _cost_for_row(row, pricing):
    """Calculate cost from a row dict using the given pricing."""
    return (
        (row.get("input_tokens", 0) or 0) / 1e6 * pricing["input"]
        + (row.get("output_tokens", 0) or 0) / 1e6 * pricing["output"]
        + (row.get("cache_read", 0) or row.get("cache_read_tokens", 0) or 0) / 1e6 * pricing["cache_read"]
        + (row.get("cache_creation", 0) or row.get("cache_creation_tokens", 0) or 0) / 1e6 * pricing["cache_creation"]
    )

# ---------------------------------------------------------------------------
# PID file management
# ---------------------------------------------------------------------------

def _is_process_alive(pid):
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _acquire_pidfile():
    """Write our PID. Exit if another worker is already running."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            if _is_process_alive(old_pid):
                log.info("Worker already running at PID %d, exiting", old_pid)
                sys.exit(0)
            log.info("Stale PID file (PID %d dead), taking over", old_pid)
        except (ValueError, OSError):
            pass

    PID_FILE.write_text(str(os.getpid()))
    atexit.register(_release_pidfile)


def _release_pidfile():
    """Remove PID file on exit."""
    try:
        if PID_FILE.exists():
            stored = int(PID_FILE.read_text().strip())
            if stored == os.getpid():
                PID_FILE.unlink()
                log.info("PID file removed")
    except (ValueError, OSError):
        pass


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_activity TEXT NOT NULL,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_cache_read INTEGER DEFAULT 0,
    total_cache_creation INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS token_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    model TEXT,
    tool_name TEXT,
    prompt_preview TEXT,
    transcript_line INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_events_session ON token_events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON token_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON token_events(event_type);
"""


def _get_conn():
    db = os.environ.get("TOKEN_TRACKER_DB", str(DB_PATH))
    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def get_db():
    conn = _get_conn()
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    conn = _get_conn()
    conn.executescript(SCHEMA_SQL)
    conn.close()
    log.info("Database initialized at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def ingest_events(events):
    if not events:
        return {"ingested": 0}

    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            for ev in events:
                sid = ev["session_id"]
                cur.execute("""
                    INSERT INTO sessions (session_id, first_seen, last_activity,
                        total_input_tokens, total_output_tokens, total_cache_read, total_cache_creation)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        last_activity = excluded.last_activity,
                        total_input_tokens = total_input_tokens + excluded.total_input_tokens,
                        total_output_tokens = total_output_tokens + excluded.total_output_tokens,
                        total_cache_read = total_cache_read + excluded.total_cache_read,
                        total_cache_creation = total_cache_creation + excluded.total_cache_creation
                """, (
                    sid, ev["timestamp"], ev["timestamp"],
                    ev.get("input_tokens", 0), ev.get("output_tokens", 0),
                    ev.get("cache_read_tokens", 0), ev.get("cache_creation_tokens", 0),
                ))
                cur.execute("""
                    INSERT INTO token_events
                        (session_id, event_type, timestamp, input_tokens, output_tokens,
                         cache_read_tokens, cache_creation_tokens, total_tokens,
                         model, tool_name, prompt_preview, transcript_line)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sid, ev.get("event_type", "turn"), ev["timestamp"],
                    ev.get("input_tokens", 0), ev.get("output_tokens", 0),
                    ev.get("cache_read_tokens", 0), ev.get("cache_creation_tokens", 0),
                    ev.get("total_tokens", 0), ev.get("model"),
                    ev.get("tool_name"), ev.get("prompt_preview"),
                    ev.get("transcript_line"),
                ))
            conn.commit()
            log.info("Ingested %d events for session(s): %s",
                     len(events), ", ".join(set(e["session_id"] for e in events)))
            return {"ingested": len(events)}
        except Exception as e:
            conn.rollback()
            log.error("Ingest error: %s", e, exc_info=True)
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def period_to_start(period):
    now = datetime.now(timezone.utc)
    deltas = {"day": 1, "week": 7, "month": 30, "quarter": 90}
    if period in deltas:
        return (now - timedelta(days=deltas[period])).isoformat()
    return None


def query_summary(period="all"):
    start = period_to_start(period)
    with get_db() as conn:
        where = "WHERE timestamp >= ?" if start else ""
        params = (start,) if start else ()
        row = conn.execute(f"""
            SELECT
                COUNT(*) as total_events,
                COUNT(DISTINCT session_id) as total_sessions,
                COALESCE(SUM(input_tokens), 0) as total_input,
                COALESCE(SUM(output_tokens), 0) as total_output,
                COALESCE(SUM(cache_read_tokens), 0) as total_cache_read,
                COALESCE(SUM(cache_creation_tokens), 0) as total_cache_creation,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(CASE WHEN event_type='turn' THEN 1 ELSE 0 END), 0) as total_turns,
                COALESCE(SUM(CASE WHEN event_type='tool_use' THEN 1 ELSE 0 END), 0) as total_tool_uses,
                MIN(timestamp) as earliest,
                MAX(timestamp) as latest
            FROM token_events {where}
        """, params).fetchone()

        # Model-aware cost calculation
        model_rows = conn.execute(f"""
            SELECT model,
                COALESCE(SUM(input_tokens), 0) as input_tokens,
                COALESCE(SUM(output_tokens), 0) as output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) as cache_read,
                COALESCE(SUM(cache_creation_tokens), 0) as cache_creation
            FROM token_events {where}
            GROUP BY model
        """, params).fetchall()
        cost = sum(
            _cost_for_row(dict(mr), MODEL_PRICING.get(_classify_model(mr["model"]), DEFAULT_PRICING))
            for mr in model_rows
        )
        return {
            "period": period,
            "total_events": row["total_events"],
            "total_sessions": row["total_sessions"],
            "total_turns": row["total_turns"],
            "total_tool_uses": row["total_tool_uses"],
            "total_input_tokens": row["total_input"],
            "total_output_tokens": row["total_output"],
            "total_cache_read_tokens": row["total_cache_read"],
            "total_cache_creation_tokens": row["total_cache_creation"],
            "total_tokens": row["total_tokens"],
            "estimated_cost_usd": round(cost, 4),
            "earliest_event": row["earliest"],
            "latest_event": row["latest"],
        }


def query_sessions(period="all", limit=20):
    start = period_to_start(period)
    with get_db() as conn:
        where = "WHERE s.last_activity >= ?" if start else ""
        params = (start,) if start else ()
        rows = conn.execute(f"""
            SELECT s.session_id, s.first_seen, s.last_activity,
                s.total_input_tokens, s.total_output_tokens,
                s.total_cache_read, s.total_cache_creation,
                (s.total_input_tokens + s.total_output_tokens +
                 s.total_cache_read + s.total_cache_creation) as total_tokens
            FROM sessions s {where}
            ORDER BY s.last_activity DESC LIMIT ?
        """, (*params, limit)).fetchall()
        return [dict(r) for r in rows]


def query_cost_breakdown(period="all"):
    """Per-model cost breakdown."""
    start = period_to_start(period)
    with get_db() as conn:
        where = "WHERE timestamp >= ?" if start else ""
        params = (start,) if start else ()
        rows = conn.execute(f"""
            SELECT model,
                COALESCE(SUM(input_tokens), 0) as input_tokens,
                COALESCE(SUM(output_tokens), 0) as output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) as cache_read,
                COALESCE(SUM(cache_creation_tokens), 0) as cache_creation,
                COALESCE(SUM(total_tokens), 0) as total_tokens
            FROM token_events {where}
            GROUP BY model
        """, params).fetchall()

        by_model = []
        total_cost = 0.0
        for r in rows:
            rd = dict(r)
            family = _classify_model(rd["model"])
            pricing = MODEL_PRICING.get(family, DEFAULT_PRICING)
            cost = _cost_for_row(rd, pricing)
            total_cost += cost
            by_model.append({
                "model": family,
                "raw_model": rd["model"] or "unknown",
                "input_tokens": rd["input_tokens"],
                "output_tokens": rd["output_tokens"],
                "cache_read_tokens": rd["cache_read"],
                "cache_creation_tokens": rd["cache_creation"],
                "total_tokens": rd["total_tokens"],
                "cost_usd": round(cost, 4),
            })
        by_model.sort(key=lambda x: x["cost_usd"], reverse=True)
        return {
            "period": period,
            "total_cost_usd": round(total_cost, 4),
            "by_model": by_model,
            "pricing": MODEL_PRICING,
        }


def query_model_timeseries(period="all", bucket="auto"):
    """Token usage over time grouped by model."""
    start = period_to_start(period)
    if bucket == "auto":
        bucket = {"day": "hour", "week": "day", "month": "day",
                  "quarter": "week", "all": "month"}.get(period, "day")
    fmt = {"hour": "%Y-%m-%dT%H:00:00", "day": "%Y-%m-%d",
           "week": "%Y-W%W", "month": "%Y-%m"}.get(bucket, "%Y-%m-%d")
    where = "WHERE timestamp >= ?" if start else ""
    params = (start,) if start else ()
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT strftime('{fmt}', timestamp) as bucket, model,
                SUM(total_tokens) as total_tokens
            FROM token_events {where}
            GROUP BY strftime('{fmt}', timestamp), model
            ORDER BY bucket
        """, params).fetchall()

        # Pivot: {bucket: {model: tokens}}
        models_set = set()
        buckets = {}
        for r in rows:
            b = r["bucket"]
            family = _classify_model(r["model"])
            models_set.add(family)
            if b not in buckets:
                buckets[b] = {}
            buckets[b][family] = buckets[b].get(family, 0) + (r["total_tokens"] or 0)

        models = sorted(models_set)
        data = []
        for b in sorted(buckets.keys()):
            entry = {"bucket": b}
            for m in models:
                entry[m] = buckets[b].get(m, 0)
            data.append(entry)

        return {"period": period, "bucket_size": bucket, "models": models, "data": data}


# ---------------------------------------------------------------------------
# HTTP Server (FastAPI)
# ---------------------------------------------------------------------------

def start_http_server(port):
    """Start the FastAPI server. Blocks the calling thread."""
    try:
        from fastapi import FastAPI, Query as FQ, HTTPException, Body
        from fastapi.responses import HTMLResponse, JSONResponse
        import uvicorn
    except ImportError:
        log.error("fastapi/uvicorn not installed — cannot start HTTP server")
        sys.exit(1)

    app = FastAPI(title="Claude Token Tracker")

    # -- Health endpoint ---------------------------------------------------

    @app.get("/api/health")
    async def api_health():
        return {"status": "ok", "pid": os.getpid(), "port": port, "version": WORKER_VERSION}

    # -- Ingest endpoint ---------------------------------------------------
    # NOTE: We use Body(...) instead of request: Request because
    # `from __future__ import annotations` breaks FastAPI's Request injection.

    @app.post("/api/ingest")
    async def api_ingest(body: dict = Body(...)):
        try:
            events = body if isinstance(body, list) else body.get("events", [])
            return ingest_events(events)
        except Exception as e:
            log.error("Ingest endpoint error: %s", e, exc_info=True)
            return JSONResponse(status_code=500, content={"error": str(e)})

    # -- Dashboard UI ------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def serve_dashboard():
        html_path = ASSETS_DIR / "dashboard.html"
        if html_path.exists():
            return HTMLResponse(html_path.read_text(encoding="utf-8"))
        raise HTTPException(status_code=404, detail="Dashboard HTML not found")

    # -- Read-only API endpoints -------------------------------------------

    @app.get("/api/summary")
    async def api_summary(period: str = FQ("all", pattern="^(all|quarter|month|week|day)$")):
        return query_summary(period)

    @app.get("/api/timeseries")
    async def api_timeseries(
        period: str = FQ("all", pattern="^(all|quarter|month|week|day)$"),
        bucket: str = FQ("auto", pattern="^(auto|hour|day|week|month)$"),
    ):
        start = period_to_start(period)
        if bucket == "auto":
            bucket = {"day": "hour", "week": "day", "month": "day",
                      "quarter": "week", "all": "month"}.get(period, "day")
        fmt = {"hour": "%Y-%m-%dT%H:00:00", "day": "%Y-%m-%d",
               "week": "%Y-W%W", "month": "%Y-%m"}.get(bucket, "%Y-%m-%d")
        where = "WHERE timestamp >= ?" if start else ""
        params = (start,) if start else ()
        with get_db() as conn:
            rows = conn.execute(f"""
                SELECT strftime('{fmt}', timestamp) as bucket,
                    SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,
                    SUM(cache_read_tokens) as cache_read_tokens,
                    SUM(cache_creation_tokens) as cache_creation_tokens,
                    SUM(total_tokens) as total_tokens, COUNT(*) as event_count,
                    COUNT(DISTINCT session_id) as session_count
                FROM token_events {where}
                GROUP BY strftime('{fmt}', timestamp) ORDER BY bucket
            """, params).fetchall()
            return {"period": period, "bucket_size": bucket, "data": [dict(r) for r in rows]}

    @app.get("/api/sessions")
    async def api_sessions(
        period: str = FQ("all", pattern="^(all|quarter|month|week|day)$"),
        page: int = FQ(1, ge=1), per_page: int = FQ(50, ge=1, le=200),
    ):
        start = period_to_start(period)
        offset = (page - 1) * per_page
        with get_db() as conn:
            where = "WHERE s.last_activity >= ?" if start else ""
            params = (start,) if start else ()
            total = conn.execute(f"SELECT COUNT(*) FROM sessions s {where}", params).fetchone()[0]
            rows = conn.execute(f"""
                SELECT s.session_id, s.first_seen, s.last_activity,
                    s.total_input_tokens, s.total_output_tokens, s.total_cache_read, s.total_cache_creation,
                    (s.total_input_tokens + s.total_output_tokens + s.total_cache_read + s.total_cache_creation) as total_tokens,
                    (SELECT COUNT(*) FROM token_events e WHERE e.session_id=s.session_id AND e.event_type='turn') as turn_count,
                    (SELECT COUNT(*) FROM token_events e WHERE e.session_id=s.session_id AND e.event_type='tool_use') as tool_use_count
                FROM sessions s {where} ORDER BY s.last_activity DESC LIMIT ? OFFSET ?
            """, (*params, per_page, offset)).fetchall()
            return {"period": period, "page": page, "per_page": per_page, "total": total,
                    "total_pages": (total + per_page - 1) // per_page, "sessions": [dict(r) for r in rows]}

    @app.get("/api/session/{session_id}/events")
    async def api_session_events(session_id: str, page: int = FQ(1, ge=1), per_page: int = FQ(100, ge=1, le=500)):
        offset = (page - 1) * per_page
        with get_db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM token_events WHERE session_id=?", (session_id,)).fetchone()[0]
            if total == 0:
                raise HTTPException(status_code=404, detail="Session not found")
            rows = conn.execute("""
                SELECT * FROM token_events WHERE session_id=? ORDER BY timestamp ASC LIMIT ? OFFSET ?
            """, (session_id, per_page, offset)).fetchall()
            return {"session_id": session_id, "page": page, "per_page": per_page, "total": total,
                    "total_pages": (total + per_page - 1) // per_page, "events": [dict(r) for r in rows]}

    @app.get("/api/breakdown")
    async def api_breakdown(period: str = FQ("all", pattern="^(all|quarter|month|week|day)$")):
        start = period_to_start(period)
        where = "WHERE timestamp >= ?" if start else ""
        params = (start,) if start else ()
        with get_db() as conn:
            row = conn.execute(f"""
                SELECT COALESCE(SUM(input_tokens),0) as input_tokens, COALESCE(SUM(output_tokens),0) as output_tokens,
                    COALESCE(SUM(cache_read_tokens),0) as cache_read_tokens, COALESCE(SUM(cache_creation_tokens),0) as cache_creation_tokens
                FROM token_events {where}
            """, params).fetchone()
            by_type = conn.execute(f"""
                SELECT event_type, COALESCE(SUM(input_tokens),0) as input_tokens, COALESCE(SUM(output_tokens),0) as output_tokens,
                    COALESCE(SUM(cache_read_tokens),0) as cache_read_tokens, COALESCE(SUM(cache_creation_tokens),0) as cache_creation_tokens,
                    COALESCE(SUM(total_tokens),0) as total_tokens, COUNT(*) as count
                FROM token_events {where} GROUP BY event_type
            """, params).fetchall()
            top_tools = conn.execute(f"""
                SELECT tool_name, COUNT(*) as count, SUM(total_tokens) as total_tokens,
                    SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens
                FROM token_events {where} {"AND" if start else "WHERE"} tool_name IS NOT NULL
                GROUP BY tool_name ORDER BY total_tokens DESC LIMIT 20
            """, params).fetchall()
            # Per-type cost breakdown (model-aware)
            model_rows = conn.execute(f"""
                SELECT model,
                    COALESCE(SUM(input_tokens),0) as input_tokens,
                    COALESCE(SUM(output_tokens),0) as output_tokens,
                    COALESCE(SUM(cache_read_tokens),0) as cache_read_tokens,
                    COALESCE(SUM(cache_creation_tokens),0) as cache_creation_tokens
                FROM token_events {where} GROUP BY model
            """, params).fetchall()
            cost_by_type = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_creation": 0.0}
            for mr in model_rows:
                mrd = dict(mr)
                pricing = MODEL_PRICING.get(_classify_model(mrd["model"]), DEFAULT_PRICING)
                cost_by_type["input"] += (mrd["input_tokens"] or 0) / 1e6 * pricing["input"]
                cost_by_type["output"] += (mrd["output_tokens"] or 0) / 1e6 * pricing["output"]
                cost_by_type["cache_read"] += (mrd["cache_read_tokens"] or 0) / 1e6 * pricing["cache_read"]
                cost_by_type["cache_creation"] += (mrd["cache_creation_tokens"] or 0) / 1e6 * pricing["cache_creation"]
            cost_by_type = {k: round(v, 6) for k, v in cost_by_type.items()}
            return {"period": period, "totals": dict(row), "by_event_type": [dict(r) for r in by_type],
                    "top_tools": [dict(r) for r in top_tools], "cost_by_type": cost_by_type}

    # -- Cost / model endpoints --------------------------------------------

    @app.get("/api/cost")
    async def api_cost(period: str = FQ("all", pattern="^(all|quarter|month|week|day)$")):
        return query_cost_breakdown(period)

    @app.get("/api/model-timeseries")
    async def api_model_timeseries(
        period: str = FQ("all", pattern="^(all|quarter|month|week|day)$"),
        bucket: str = FQ("auto", pattern="^(auto|hour|day|week|month)$"),
    ):
        return query_model_timeseries(period, bucket)

    # -- Restart endpoint --------------------------------------------------

    @app.post("/api/restart")
    async def api_restart():
        """Gracefully shutdown so the MCP server can respawn a fresh worker."""
        import asyncio
        log.info("Restart requested via API")

        def _graceful_exit():
            log.info("Exiting for restart")
            _release_pidfile()
            os._exit(0)

        asyncio.get_event_loop().call_later(0.5, _graceful_exit)
        return {"status": "restarting", "version": WORKER_VERSION}

    # -- Run ---------------------------------------------------------------

    log.info("Starting HTTP server on port %d (PID %d), version %s", port, os.getpid(), WORKER_VERSION)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Worker starting (PID %d)...", os.getpid())

    _acquire_pidfile()
    init_db()

    port = DEFAULT_PORT

    # Test if port is available
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
    except OSError:
        log.error("Port %d is already in use (but no live PID found) — exiting", port)
        sys.exit(1)

    PORT_FILE.write_text(str(port))
    log.info("Dashboard + ingest available at http://localhost:%d", port)

    # Handle graceful shutdown
    def _shutdown(signum, frame):
        log.info("Received signal %d, shutting down", signum)
        _release_pidfile()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    start_http_server(port)  # blocks


if __name__ == "__main__":
    main()
