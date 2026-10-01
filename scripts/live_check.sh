#!/usr/bin/env bash
# Live smoke test: boots the real server and checks the OAuth discovery
# endpoints an MCP client would hit. No LLM, no R2 writes.
set -u

PY=.venv/bin/python
PORT=8001
BASE="http://127.0.0.1:$PORT"

"$PY" run.py > /tmp/joyverse-live.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

for _ in $(seq 1 25); do
  curl -sf "$BASE/health" > /dev/null 2>&1 && break
  sleep 0.4
done

echo "--- GET /health ---"
curl -s "$BASE/health"; echo

echo "--- GET /.well-known/oauth-protected-resource (expect 404, not mounted yet) ---"
curl -s -o /dev/null -w 'status=%{http_code}\n' "$BASE/.well-known/oauth-protected-resource"

echo "--- POST /mcp with no token (expect 401) ---"
curl -s -o /dev/null -w 'status=%{http_code}\n' -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

echo "--- POST /mcp tools/list with a bad token (expect 401) ---"
curl -s -o /dev/null -w 'status=%{http_code}\n' -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer not-a-real-token' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'