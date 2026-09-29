# Run Log

Each entry is one `--auto` run against the live Cloudflare R2 bucket as
`aarav-test`. The LLM chooses its own tool calls; nothing is scripted.

Before each run the `users/aarav-test/` prefix is deleted so runs are
comparable, except where noted.

---

## Run 1 — 2026-09-29

**State:** clean bucket (0 objects).
**Code at this point:** section-replace fix applied; prompt fully rewritten to be
tool-centric.

### Result: INCOMPLETE — ran out of rounds

| Metric | Value |
|---|---|
| Tool calls | 17 |
| LLM turns | 12 (hit `MAX_TOOL_ROUNDS`) |
| Objects written | 3 (`profile.md`, `bio.md`, `memory.json`) |
| Data logs written | **0 of 4** |
| Errors | 6 — all expected first-read "not found" misses |

### Tool call breakdown

```
4  update_profile      4  update_bio
4  get_data            2  add_memory_trait
1  get_profile         1  get_memory     1  get_bio
```

### What went right

- `profile.md` created with clean structure: `# User Profile` / `## Identity` /
  `## Background` / `## Skills`. No `Identity: name: Aarav` garbage, no nested
  headers, no duplicated sections. The section-replace fix and the prompt
  documentation both worked.
- 34 lines / 1,923 chars — well under the 100-line cap.
- The LLM read all 4 topics before writing, as the prompt instructs.

### What went wrong

**It hit `MAX_TOOL_ROUNDS = 12` and never reached the data logs.** It spent its
budget on reads and profile/bio, then the loop terminated mid-workflow. The
prompt rewrite made the LLM more thorough (it now reads before writing), which
in turn pushed it past the cap.

This is a harness limit, not a server bug — but it means a "successful" run can
still leave data half-written. Worth noting for anyone reading the bucket
afterwards.

### Actions

1. Raise `MAX_TOOL_ROUNDS` from 12 to 25.
2. Keep the prompt as is — the structure it produced was correct.

---

## Run 2 — 2026-09-29

**State:** clean bucket. `MAX_TOOL_ROUNDS` raised 12 → 25.

### Result: COMPLETE, but a prompt bug found

| Metric | Run 1 | Run 2 |
|---|---|---|
| Tool calls | 17 | **45** |
| LLM turns | 12 (capped) | 22 |
| Objects written | 3 | **7** |
| Data logs | 0/4 | **4/4** |
| Tools exercised | 6/9 | **9/9** |

All 7 expected objects created. Every tool used at least once.

### What went right

- `profile.md` clean: 30 lines, sections `Identity` / `Background` / `Skills`,
  **zero inline field leftovers** — the section-replace fix holds.
- `bio.md` with all three sections.
- All four data logs written with correct topic→filename mapping.

### What went wrong — my own prompt bug

Two data logs were missing required fields:

| Topic | Keys present | Problem |
|---|---|---|
| `dsa` | 8 | OK |
| `projects` | `projects`, `last_updated` | **no `summary`** |
| `skills` | 6 keys | **no `summary`** |
| `reading` | 4 | OK |

Root cause: the prompt's rules section said every data JSON must include
`summary` and `last_updated`, but the **sample JSON schemas for `projects` and
`skills` only showed `last_updated`**. The LLM followed the concrete schema
over the prose rule.

That was a self-inflicted contradiction in the prompt rewrite — the two places
disagreed with each other.

### Actions

1. Add `"summary": "string"` to the `projects` and `skills` sample schemas so
   the examples and the rules agree.
2. Fixed the `test_honours_max_tool_rounds` test, which was coupled to the old
   hardcoded value of 12 rather than reading the constant.

---

## Run 3 — 2026-09-29

**State:** clean bucket. Prompt schemas corrected.

### Result: PASS

| Metric | Run 2 | Run 3 |
|---|---|---|
| Tool calls | 45 | 44 |
| LLM turns | 22 | 21 |
| Objects written | 7 | **7** |
| Data logs valid | 2/4 | **4/4** |
| Tools exercised | 9/9 | **9/9** |

### Verification

```
users/aarav-test/bio.md                       3496b
users/aarav-test/data/dsa/progress.json        380b
users/aarav-test/data/projects/active.json    1579b
users/aarav-test/data/reading/list.json        169b
users/aarav-test/data/skills/stack.json       1148b
users/aarav-test/memory.json                  1290b
users/aarav-test/profile.md                   2274b
```

- All 4 data logs contain `summary` + `last_updated` — **OK across the board**
- `profile.md` sections: `Identity` / `Background` / `Skills` / `Work` — the LLM
  invented a person-appropriate `## Work` section, which is what the prompt's
  "create whatever sections fit this person" rule asks for
- No inline field leftovers, no nested headers, no duplicate sections
- Full test suite: **347 passed**

### Stable across runs 2 and 3

- 7 objects, 4 data logs, 9 tools exercised
- Clean section structure
- The "never fabricate" rule holds — the `reading` topic again records that
  nothing was supplied rather than inventing books

---

## Summary of fixes made across the loop

| # | Found in | Issue | Fix |
|---|----------|-------|-----|
| 1 | live run | `update_profile` had no create path | seed document on `NoSuchKey` |
| 2 | live run | section re-calls appended instead of replacing | replace block, mirroring `bio.py` |
| 3 | run 1 | LLM ran out of rounds | `MAX_TOOL_ROUNDS` 12 → 25 |
| 4 | run 2 | prompt schemas contradicted the rules on `summary` | added `summary` to both schemas |
| 5 | run 2 | test coupled to a hardcoded constant | read the constant instead |

Issues 1 and 2 were server bugs. Issues 3 and 5 were harness bugs. Issue 4 was a
prompt bug. The LLM itself behaved correctly in all three runs — every problem
was in code I wrote.
