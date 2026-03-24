#!/usr/bin/env python3
"""
Token Tracker MCP Server — thin STDIO proxy.

Handles the MCP JSON-RPC protocol over STDIO and proxies tool calls to the
persistent worker daemon (worker.py) via HTTP. On startup, spawns the worker
if it isn't already running.

The worker owns the database and HTTP dashboard; this process is ephemeral
and dies when the Claude Code session ends.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import logging
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# Venv bootstrap (must run before any third-party imports)
# ---------------------------------------------------------------------------

VENV_DIR = Path.home() / ".claude" / "token-tracker" / "venv"
REQUIRED_PACKAGES = ["fastapi", "uvicorn"]


def _ensure_venv_and_reexec():
    """Bootstrap a venv and re-exec under it if needed."""
    venv_python = VENV_DIR / "bin" / "python3"

    if sys.prefix != sys.base_prefix:
        return  # already in venv

    data_dir = Path.home() / ".claude" / "token-tracker"
    data_dir.mkdir(parents=True, exist_ok=True)
    install_log = data_dir / "pip-install.log"

    if not venv_python.exists():
        subprocess.check_call(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            stdout=open(str(install_log), "a"),
            stderr=subprocess.STDOUT,
        )

    missing = []
    for pkg in REQUIRED_PACKAGES:
        result = subprocess.run(
            [str(venv_python), "-c", f"import {pkg}"],
            capture_output=True,
        )
        if result.returncode != 0:
            missing.append(pkg)

    if missing:
        subprocess.check_call(
            [str(VENV_DIR / "bin" / "pip"), "install", "--quiet", *missing],
            stdout=open(str(install_log), "a"),
            stderr=subprocess.STDOUT,
        )

    os.execv(str(venv_python), [str(venv_python), __file__])


_ensure_venv_and_reexec()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = Path.home() / ".claude" / "token-tracker"
PORT_FILE = DATA_DIR / "dashboard_port"
PID_FILE = DATA_DIR / "worker.pid"
LOG_PATH = DATA_DIR / "mcp-server.log"
WORKER_SCRIPT = Path(__file__).parent / "worker.py"

DEFAULT_PORT = int(os.environ.get("TOKEN_TRACKER_PORT", "47700"))

os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("mcp_server")


def _read_plugin_version():
    """Read version from plugin.json."""
    pj = Path(__file__).parent.parent / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(pj.read_text(encoding="utf-8"))["version"]
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Worker management
# ---------------------------------------------------------------------------

def _is_process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _get_worker_port():
    """Read the worker port from the port file."""
    if PORT_FILE.exists():
        try:
            return int(PORT_FILE.read_text().strip())
        except (ValueError, OSError):
            pass
    return DEFAULT_PORT


def _worker_health():
    """Check worker health. Returns health dict or None if unhealthy."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if not _is_process_alive(pid):
                log.info("Worker PID %d is dead", pid)
                return None
        except (ValueError, OSError):
            return None
    else:
        return None

    port = _get_worker_port()
    try:
        req = Request(f"http://127.0.0.1:{port}/api/health")
        with urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            if data.get("status") == "ok":
                return data
    except Exception:
        pass
    return None


def _worker_healthy():
    """Check if the worker daemon is alive and responding."""
    return _worker_health() is not None


def _kill_worker():
    """Kill the running worker daemon. Returns True if killed."""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        if _is_process_alive(pid):
            os.kill(pid, signal.SIGTERM)
            # Wait for it to die
            for _ in range(20):  # up to 2 seconds
                time.sleep(0.1)
                if not _is_process_alive(pid):
                    log.info("Worker PID %d terminated", pid)
                    return True
            log.warning("Worker PID %d did not terminate in 2s, sending SIGKILL", pid)
            os.kill(pid, signal.SIGKILL)
            return True
    except (ValueError, OSError, ProcessLookupError):
        pass
    return False


def _spawn_worker():
    """Spawn a new worker daemon. Returns the port once healthy."""
    log.info("Spawning worker daemon...")
    venv_python = VENV_DIR / "bin" / "python3"
    python = str(venv_python) if venv_python.exists() else sys.executable

    worker_log = open(str(DATA_DIR / "worker-spawn.log"), "a")
    subprocess.Popen(
        [python, str(WORKER_SCRIPT)],
        stdin=subprocess.DEVNULL,
        stdout=worker_log,
        stderr=worker_log,
        start_new_session=True,
    )

    for _ in range(30):  # up to 3 seconds
        time.sleep(0.1)
        if _worker_healthy():
            port = _get_worker_port()
            log.info("Worker is up on port %d", port)
            return port

    log.error("Worker failed to start within 3 seconds")
    return DEFAULT_PORT


def _ensure_worker():
    """Spawn the worker daemon if not already running. Restart if version is stale. Returns the port."""
    health = _worker_health()
    if health:
        worker_version = health.get("version", "unknown")
        expected_version = _read_plugin_version()
        if worker_version != expected_version and expected_version != "unknown":
            log.info("Worker version %s is stale (expected %s), restarting...", worker_version, expected_version)
            _kill_worker()
            return _spawn_worker()

        port = _get_worker_port()
        log.info("Worker already healthy on port %d (version %s)", port, worker_version)
        return port

    return _spawn_worker()


def _worker_get(path):
    """GET from worker HTTP API. Returns parsed JSON or None."""
    port = _get_worker_port()
    try:
        req = Request(f"http://127.0.0.1:{port}{path}")
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.error("Worker GET %s failed: %s", path, e)
        return None


def _worker_post(path):
    """POST to worker HTTP API. Returns parsed JSON or None."""
    port = _get_worker_port()
    try:
        req = Request(f"http://127.0.0.1:{port}{path}", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.error("Worker POST %s failed: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# MCP Protocol (JSON-RPC 2.0 over STDIO)
# ---------------------------------------------------------------------------

SERVER_INFO = {"name": "token-tracker", "version": "0.1.6"}
CAPABILITIES = {"tools": {}}

TOOLS = [
    {
        "name": "get_dashboard_url",
        "description": "Get the URL of the token usage dashboard. The dashboard shows interactive D3.js charts of token usage across all-time, quarter, month, week, and day views with drill-down to individual prompts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_usage_summary",
        "description": "Get a summary of token usage for a given time period. Returns total tokens, cost estimate, session counts, and breakdowns by type (input/output/cache).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string", "enum": ["all", "quarter", "month", "week", "day"],
                    "description": "Time period to summarize. Defaults to 'all'.",
                },
            },
        },
    },
    {
        "name": "get_recent_sessions",
        "description": "Get a list of recent Claude Code sessions with token totals for each. Useful for seeing which sessions consumed the most tokens.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string", "enum": ["all", "quarter", "month", "week", "day"],
                    "description": "Time period to filter. Defaults to 'all'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max sessions to return. Defaults to 20.",
                },
            },
        },
    },
    {
        "name": "restart_worker",
        "description": "Restart the token tracker worker daemon. Use this after a plugin update or when the dashboard is unresponsive. Kills the running worker and spawns a fresh one with the latest code.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def handle_initialize(params):
    return {"protocolVersion": "2024-11-05", "capabilities": CAPABILITIES, "serverInfo": SERVER_INFO}


def handle_tools_list(params):
    return {"tools": TOOLS}


def handle_tools_call(params):
    name = params.get("name", "")
    args = params.get("arguments", {})

    if name == "get_dashboard_url":
        port = DEFAULT_PORT
        if PORT_FILE.exists():
            try:
                port = int(PORT_FILE.read_text().strip())
            except (ValueError, OSError):
                pass
        if _worker_healthy():
            return {"content": [{"type": "text", "text": f"Token Tracker dashboard is running at: http://localhost:{port}"}]}
        return {"content": [{"type": "text", "text": "Dashboard worker is not running. It will auto-start on next session."}], "isError": True}

    elif name == "get_usage_summary":
        period = args.get("period", "all")
        data = _worker_get(f"/api/summary?period={period}")
        if data:
            return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]}
        return {"content": [{"type": "text", "text": "Failed to reach worker — is the daemon running?"}], "isError": True}

    elif name == "get_recent_sessions":
        period = args.get("period", "all")
        limit = args.get("limit", 20)
        data = _worker_get(f"/api/sessions?period={period}&per_page={limit}")
        if data:
            return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]}
        return {"content": [{"type": "text", "text": "Failed to reach worker — is the daemon running?"}], "isError": True}

    elif name == "restart_worker":
        try:
            old_health = _worker_health()
            old_version = old_health.get("version", "unknown") if old_health else "not running"

            # Try graceful restart via API first, fall back to kill
            if old_health:
                _worker_post("/api/restart")
                time.sleep(1)

            # If still alive, force kill
            if _worker_healthy():
                _kill_worker()

            # Spawn fresh worker
            port = _spawn_worker()
            new_health = _worker_health()
            new_version = new_health.get("version", "unknown") if new_health else "failed"

            msg = f"Worker restarted. Old version: {old_version}, new version: {new_version}, port: {port}"
            log.info(msg)
            return {"content": [{"type": "text", "text": msg}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Restart failed: {e}"}], "isError": True}

    return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}


HANDLERS = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
}


def send_response(msg_id, result):
    response = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})
    sys.stdout.write(response + "\n")
    sys.stdout.flush()


def send_error(msg_id, code, message):
    response = json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})
    sys.stdout.write(response + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("MCP server starting...")

    # Ensure the worker daemon is running
    port = _ensure_worker()
    log.info("Worker available on port %d", port)

    # STDIO MCP loop
    log.info("Entering STDIO loop")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            log.error("Invalid JSON: %s", e)
            continue

        msg_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        log.debug("Request: method=%s id=%s", method, msg_id)

        if msg_id is None:
            if method == "notifications/initialized":
                log.info("Client initialized")
            continue

        handler = HANDLERS.get(method)
        if handler:
            try:
                send_response(msg_id, handler(params))
            except Exception as e:
                log.error("Handler error for %s: %s", method, e, exc_info=True)
                send_error(msg_id, -32603, str(e))
        else:
            log.warning("Unknown method: %s", method)
            send_error(msg_id, -32601, f"Method not found: {method}")

    log.info("STDIO loop ended")


if __name__ == "__main__":
    main()
