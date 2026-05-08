#!/usr/bin/env bash
# restart-mcp.sh — kill the running MCP server child so the VS Code (or other
# MCP client) supervisor respawns it with the latest code on disk.
#
# Usage:  bash scripts/restart-mcp.sh [--force]
#   --force   send SIGKILL instead of SIGTERM
#
# The script matches python processes whose argv contains the absolute path of
# this repository's mcp_server.py. It will not touch unrelated MCP servers.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$REPO_ROOT/mcp_server.py"
SIG="TERM"

if [[ "${1:-}" == "--force" ]]; then
    SIG="KILL"
fi

mapfile -t PIDS < <(pgrep -af -- "$TARGET" | awk '{print $1}')

if [[ ${#PIDS[@]} -eq 0 ]]; then
    echo "[restart-mcp] no running mcp_server.py for $TARGET — nothing to kill."
    echo "[restart-mcp] start the MCP client (VS Code) to spawn it fresh."
    exit 0
fi

echo "[restart-mcp] sending SIG$SIG to: ${PIDS[*]}"
for pid in "${PIDS[@]}"; do
    kill "-$SIG" "$pid" 2>/dev/null || echo "[restart-mcp] could not signal $pid"
done

# brief grace period so the supervisor notices EOF on stdio
sleep 1

REMAINING=()
for pid in "${PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
        REMAINING+=("$pid")
    fi
done

if [[ ${#REMAINING[@]} -gt 0 ]]; then
    echo "[restart-mcp] still alive after SIG$SIG: ${REMAINING[*]}"
    echo "[restart-mcp] re-run with --force to SIGKILL."
    exit 1
fi

echo "[restart-mcp] killed. Your MCP client should respawn the server on next tool call."
