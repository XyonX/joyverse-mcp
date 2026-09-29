# CLIENT.md — how to run the LLM client test

Everything here is automated. The manual path is documented at the end for when
you want to drive the conversation yourself.

## Setup (once)

```bash
pip install -e '.[dev]'
cp mcp_client/.env.example mcp_client/.env
```

Then edit `mcp_client/.env`:

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=          # blank = api.openai.com
```

`OPENAI_BASE_URL` is what makes this work with any OpenAI-compatible provider —
OpenRouter, Groq, Together, or a local vLLM/llama.cpp server. Only the key and
model are provider-specific.

The R2 and `JWT_SECRET` values are read from the repo-root `.env`; you don't
need to duplicate them.

## Verify the harness first (no LLM, no writes)

```bash
python mcp_client/mcp_test_client.py --dry-run
```

Confirms: server boots, JWT auth works, `initialize` returns instructions, and
all 9 tools are listed. Creates nothing in R2. If this fails, stop — the
problem is your setup, not the LLM.

## The real run

```bash
python mcp_client/mcp_test_client.py --auto
```

The LLM reads `persona.md` and decides which tools to call. It is not scripted —
the tool calls come from the model.

## What to send the LLM, and when

`--auto` sends one message. If you're driving it manually (no flags), send
these in order.

| # | Send | Expected tool calls | Watch for |
|---|---|---|---|
| 1 | "Set up my profile. I'm Aarav, 24, backend engineer in Bengaluru." | `update_profile` ×N | It should make **many** calls — one per field, since `update_profile` takes a single field. Silence between calls is correct. |
| 2 | "Write a short bio for me with a Background and a Journey section." | `update_bio` ×2 | One call per section. Sections must be separate `##` blocks. |
| 3 | "Save my DSA progress: 120 problems solved, weak in graph DP." | `update_data(topic="dsa", ...)` | `data` must be a **JSON string**, not an object. |
| 4 | "What's in my profile and bio so far?" | `get_profile`, `get_bio` | Pure read. Text should match what was written. |
| 5 | "I'm currently focused on the reconciliation service." | `update_focus` | Also `add_memory_trait` is fair game here. |

### The one thing that will trip you up

`update_data` takes `data: str`. If the LLM passes an object instead of a
string, the server returns `Error: Invalid JSON data`. The tool schema declares
`string`, so a well-behaved model gets this right — but if you see that error,
this is why. The fix is in the tool contract, not the model.

## Modes

| Flag | Behaviour |
|---|---|
| *(none)* | Interactive REPL. Type messages, `quit` to exit. |
| `--auto` | Sends the persona message, runs the loop unattended. |
| `--message "..."` | Sends one custom message. |
| `--dry-run` | Runs the loop but never executes tools. No R2 writes. |

`MAX_TOOL_ROUNDS = 12` caps the loop. If the LLM gets stuck in a cycle the run
stops and says so rather than burning tokens.

## Checking what was written

```bash
# List the test persona's objects
python -c "
import sys; sys.path.insert(0,'.')
from joyverse.config import get_r2_client, BUCKET_NAME
r = get_r2_client().list_objects_v2(Bucket=BUCKET_NAME, Prefix='users/aarav-test/')
for o in r.get('Contents', []): print(o['Key'], o['Size'])
"
```

## Cleanup

Everything is under the `users/aarav-test/` prefix. To remove it, either delete
those objects in the Cloudflare dashboard, or:

```bash
python -c "
import sys; sys.path.insert(0,'.')
from joyverse.config import get_r2_client, BUCKET_NAME
r = get_r2_client().list_objects_v2(Bucket=BUCKET_NAME, Prefix='users/aarav-test/')
objs = [{'Key': o['Key']} for o in r.get('Contents', [])]
if objs: get_r2_client().delete_objects(Bucket=BUCKET_NAME, Delete={'Objects': objs})
print('deleted', len(objs))
"
```

## Offline tests

```bash
pytest mcp_client/    # 31 tests, no network, no API key
pytest                # 321 tests total
```

These cover token minting, SSE parsing, tool dispatch, schema conversion, and
the tool-calling loop via a stub LLM. They never call OpenAI, so they never
cost money and never flake.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `No OPENAI_API_KEY set` | `.env` not filled, or filename not exactly `.env` |
| `401` on every call | `JWT_SECRET` mismatch between root `.env` and the running process |
| `Error: Invalid JSON data` | LLM passed an object to `update_data`; it needs a JSON string |
| `R2 credentials not configured` | `CLOUDFLARE_*` missing from root `.env` |
| Tool call returns `{"error": "No data found..."}` | Expected on first read — the file doesn't exist yet |
| LLM stops after 1-2 calls | It thinks it's done. Re-send a message asking it to verify with a `get_*` tool. |
