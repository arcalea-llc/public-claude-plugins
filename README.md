# Arcalea Public Claude Plugins

A public plugin marketplace for [Claude Code](https://claude.ai/claude-code) from Arcalea.

## Install this marketplace

```
/install-marketplace https://raw.githubusercontent.com/arcalea-llc/public-claude-plugins/main/.claude-plugin/marketplace.json
```

## Plugins

### token-tracker

Track Claude Code token usage in a local SQLite database and visualize it with an interactive D3.js dashboard.

**Features:**
- Per-session, per-turn, and per-tool token tracking
- Interactive D3.js dashboard with cost breakdowns by model and token type
- Auto-starts via bundled MCP server — no manual setup needed
- Persistent worker daemon that survives session restarts

See [plugins/token-tracker/README.md](plugins/token-tracker/README.md) for details.
