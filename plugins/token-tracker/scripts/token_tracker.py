#!/usr/bin/env python3
"""
Token Tracker Stop Hook

A thin transcript parser that runs after each assistant turn. Reads the session
transcript JSONL, extracts token usage events, and POSTs them to the MCP server's
ingest endpoint for persistence. All DB operations live in the MCP server — this
hook is just a data collector.

The MCP server writes its port to ~/.claude/token-tracker/dashboard_port so
this hook knows where to send data.
"""
from __future__ import annotations

import json
import os
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = Path.home() / ".claude" / "token-tracker"
PORT_FILE = DATA_DIR / "dashboard_port"
LOG_PATH = DATA_DIR / "hook.log"
CURSOR_DIR = DATA_DIR / ".cursors"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CURSOR_DIR, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("token_hook")


# ---------------------------------------------------------------------------
# Cursor management
# ---------------------------------------------------------------------------

def get_cursor(session_id: str) -> int:
    cursor_file = CURSOR_DIR / f".cursor_{session_id}"
    if cursor_file.exists():
        try:
            return int(cursor_file.read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def set_cursor(session_id: str, line_num: int):
    cursor_file = CURSOR_DIR / f".cursor_{session_id}"
    cursor_file.write_text(str(line_num))


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

def extract_usage(entry: dict) -> dict | None:
    """Extract token usage from a transcript JSONL entry."""
    for source in [
        entry.get("usage"),
        (entry.get("message") or {}).get("usage"),
        (entry.get("response") or {}).get("usage"),
        (entry.get("result") or {}).get("usage"),
    ]:
        if isinstance(source, dict):
            return {
                "input_tokens": source.get("input_tokens", 0) or 0,
                "output_tokens": source.get("output_tokens", 0) or 0,
                "cache_read_tokens": source.get("cache_read_input_tokens", 0) or 0,
                "cache_creation_tokens": source.get("cache_creation_input_tokens", 0) or 0,
            }
    return None


def classify_event(entry: dict) -> tuple[str, str | None, str | None]:
    """Classify entry as turn or tool_use. Returns (type, tool_name, preview)."""
    event_type = "turn"
    tool_name = None
    prompt_preview = None

    if entry.get("type", "") in ("tool_use", "tool_result", "tool"):
        event_type = "tool_use"
        tool_name = entry.get("name") or entry.get("tool_name")

    # Scan content blocks from entry and entry.message
    msg = entry.get("message") or {}
    all_blocks = []
    for src in [entry.get("content", []), msg.get("content", []) if isinstance(msg, dict) else []]:
        if isinstance(src, list):
            all_blocks.extend(src)
        elif isinstance(src, str) and src:
            all_blocks.append(src)

    for block in all_blocks:
        if isinstance(block, dict):
            bt = block.get("type", "")
            if bt == "tool_use":
                event_type = "tool_use"
                tool_name = tool_name or block.get("name")
            elif bt == "tool_result":
                event_type = "tool_use"
                tool_name = tool_name or block.get("tool_name")
            elif bt == "text" and not prompt_preview:
                t = block.get("text", "")
                if t:
                    prompt_preview = t[:200]
        elif isinstance(block, str) and not prompt_preview:
            prompt_preview = block[:200]

    if not prompt_preview:
        for key in ("text", "last_assistant_message", "result"):
            val = entry.get(key)
            if isinstance(val, str) and val:
                prompt_preview = val[:200]
                break

    return event_type, tool_name, prompt_preview


def extract_model(entry: dict) -> str | None:
    for key in ("model", "metadata"):
        val = entry.get(key)
        if isinstance(val, str) and "claude" in val.lower():
            return val
        if isinstance(val, dict) and "model" in val:
            return val["model"]
    msg = entry.get("message") or {}
    if isinstance(msg, dict):
        return msg.get("model")
    return None


def parse_transcript(transcript_path: str, session_id: str) -> list[dict]:
    """Parse new events from the transcript JSONL."""
    path = Path(transcript_path)
    if not path.exists():
        log.warning("Transcript not found: %s", transcript_path)
        return []

    cursor = get_cursor(session_id)
    events = []
    now = datetime.now(timezone.utc).isoformat()
    last_line = cursor

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line_num, line in enumerate(f, start=1):
                last_line = line_num
                if line_num <= cursor:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                usage = extract_usage(entry)
                if not usage:
                    continue
                total = sum(usage.values())
                if total == 0:
                    continue

                etype, tool, preview = classify_event(entry)
                model = extract_model(entry)
                ts = entry.get("timestamp") or entry.get("created_at") or now
                if isinstance(ts, (int, float)):
                    ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

                events.append({
                    "session_id": session_id,
                    "event_type": etype,
                    "timestamp": ts,
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "cache_read_tokens": usage["cache_read_tokens"],
                    "cache_creation_tokens": usage["cache_creation_tokens"],
                    "total_tokens": total,
                    "model": model,
                    "tool_name": tool,
                    "prompt_preview": preview,
                    "transcript_line": line_num,
                })

        set_cursor(session_id, last_line)
    except Exception as e:
        log.error("Parse error: %s", e, exc_info=True)

    return events


# ---------------------------------------------------------------------------
# POST to MCP server
# ---------------------------------------------------------------------------

def get_server_port() -> int | None:
    """Read the port the MCP server's HTTP endpoint is listening on."""
    if PORT_FILE.exists():
        try:
            return int(PORT_FILE.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def post_events(events: list[dict]) -> bool:
    """POST events to the MCP server's ingest endpoint."""
    port = get_server_port()
    if not port:
        log.warning("No port file — MCP server may not be running")
        return False

    url = f"http://127.0.0.1:{port}/api/ingest"
    payload = json.dumps({"events": events}).encode("utf-8")

    try:
        req = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            log.info("Ingested %d events via server", result.get("ingested", 0))
            return True
    except URLError as e:
        log.error("Failed to POST to server at %s: %s", url, e)
        return False
    except Exception as e:
        log.error("POST error: %s", e, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        log.error("Bad hook input: %s", e)
        sys.exit(0)

    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path", "")

    log.info("Hook fired for session %s", session_id)

    if not transcript_path:
        log.warning("No transcript_path")
        sys.exit(0)

    transcript_path = os.path.expanduser(transcript_path)
    events = parse_transcript(transcript_path, session_id)

    if events:
        post_events(events)
    else:
        log.info("No new events for session %s", session_id)

    sys.exit(0)


if __name__ == "__main__":
    main()
