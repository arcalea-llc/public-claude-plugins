---
name: token-tracker
description: >
  Track Claude Code token usage in a local SQLite database and visualize it with an interactive
  D3.js dashboard. The dashboard auto-starts via a bundled MCP server — no manual launch needed.
  Use this skill whenever the user mentions token tracking, token usage, token budget, usage
  dashboard, cost tracking, monitoring Claude usage, or wants visibility into how much context or
  tokens their Claude sessions consume. Also trigger when users say "how many tokens am I using",
  "track my usage", "token budget", "usage analytics", "show me my Claude costs", "I want to see
  my token history", or "launch the token dashboard". Even if they just ask about recent Claude
  spending or want a usage breakdown, this skill is the right one.
---

# Token Tracker

Track every token Claude Code consumes — per-session, per-turn, and per-tool-use — in a local
SQLite database, with an interactive D3.js dashboard that's always available.

## Architecture

Everything is automatic once the plugin is installed:

```
Plugin loads
  └─> .mcp.json starts mcp-server.py (STDIO MCP server)
        ├─> Spawns worker.py daemon if not already running
        ├─> Registers MCP tools (get_dashboard_url, get_usage_summary, ...)
        └─> Proxies tool calls to worker via HTTP

worker.py (persistent daemon, survives session restarts)
  ├─> Owns SQLite database
  ├─> Serves HTTP dashboard on fixed port (default 47700)
  └─> Exposes /api/ingest, /api/summary, /api/sessions, etc.

Each assistant turn
  └─> hooks.json fires token_tracker.py (Stop hook)
        ├─> Parses session transcript JSONL
        ├─> Extracts token usage events
        └─> POSTs events to worker's /api/ingest endpoint
```

The worker daemon is the single owner of all data persistence. The MCP server is a thin STDIO
proxy. The hook is a thin transcript parser that forwards events over HTTP.

## How to Use

### Show the dashboard

Call the `get_dashboard_url` MCP tool — it returns a localhost URL the user can open in
their browser. The dashboard is already running; no manual start is needed. The default
view is "Day" for the most relevant recent usage.

### Query usage directly

Call the `get_usage_summary` MCP tool with a period (all, quarter, month, week, day) to
get aggregate stats. Or call `get_recent_sessions` to see per-session breakdowns.

### Quick SQL queries

If the user wants raw data, query `~/.claude/token-tracker/token_tracker.db` directly:

```sql
-- Total tokens this week
SELECT SUM(total_tokens) FROM token_events WHERE timestamp >= datetime('now', '-7 days');

-- Top tools by token usage
SELECT tool_name, COUNT(*) as uses, SUM(total_tokens) as total
FROM token_events WHERE tool_name IS NOT NULL
GROUP BY tool_name ORDER BY total DESC;
```

## MCP Tools Available

| Tool | Description |
|------|-------------|
| `get_dashboard_url` | Returns the localhost URL where the dashboard is running |
| `get_usage_summary` | Aggregate stats for a period (tokens, cost, sessions, turns) |
| `get_recent_sessions` | List of recent sessions with per-session token totals |

## Data Storage

All persistent data lives under `~/.claude/token-tracker/`:

| File | Purpose |
|------|---------|
| `token_tracker.db` | SQLite database with all usage records |
| `dashboard_port` | The port the worker daemon is listening on |
| `worker.pid` | PID file for the persistent worker daemon |
| `worker.log` | Worker daemon log |
| `mcp-server.log` | STDIO MCP proxy log |
| `hook.log` | Stop hook log |
| `.cursors/` | Tracks which transcript lines have been processed per session |

## Troubleshooting

If the dashboard shows no data:
1. Check `~/.claude/token-tracker/hook.log` — is the hook firing?
2. Check `~/.claude/token-tracker/worker.log` — is the worker running?
3. Check `~/.claude/token-tracker/worker.pid` — does it contain a valid PID?
4. Test the health endpoint: `curl http://localhost:47700/api/health`
5. Verify the DB: `python3 -c "import sqlite3; print(sqlite3.connect('$HOME/.claude/token-tracker/token_tracker.db').execute('SELECT COUNT(*) FROM token_events').fetchone())"`

## Prerequisites

- **Python 3.9+** (compatible with macOS system Python)
- **fastapi + uvicorn** — auto-installed into a venv at `~/.claude/token-tracker/venv/`
