# joyverse/prompts.py

"""Server instructions sent to every MCP client on connect.

This block is prepended to the model's context on every single request, so
its size is a permanent per-request cost. It is therefore kept to behaviour
the tool descriptions do not already carry:

* what the four data types are FOR, and which one to reach for
* the cross-tool rules that span more than one call
* the destructive-operation warnings, stated once, up front

Field-level schemas are deliberately NOT here. Each tool's description
already documents its own arguments, and repeating them in this block is
what made the instructions several times larger than they needed to be.
Anything that belongs in a tool description belongs in that description.

The tool list is generated from the live registry by ``build_instructions``,
so a tool cannot be registered without appearing here -- or removed while
still being advertised.
"""

_TEMPLATE = """## User Data System

You have access to the personal data of the person you are talking to. It
is stored remotely and is reachable ONLY through the MCP tools below. You
have no filesystem access to it and there is no path to construct -- you
name a *topic* or a *section*, and the server decides where it lives.

Only these {count} tools exist. Never invent another name.

{tool_list}

## Choosing a store

| Need | Store | Tools |
|---|---|---|
| Who they are: name, location, occupation, facts | profile | `get_profile` / `update_profile` |
| Their story: journey, goals, background | bio | `get_bio` / `update_bio` |
| Inferred traits, preferences, current focus | memory | `get_memory` / `add_memory_trait` / `update_focus` |
| Growing logs by subject: dsa, projects, reading, ... | data topics | `get_data` / `list_topics` / `edit_data` |

Profile and memory are small and change often. The bio is a narrative -- read
it when you need background, write it rarely. Data topics are the ones that
accumulate over time.

## Rules that span more than one call

- **Read before you write.** Call the matching `get_*` first and use the
  exact field and item names it returns. Names must match character for
  character; do not guess at spelling.
- **One write per call.** `update_profile`, `update_bio` and `edit_data` each
  write one key, one section, or one item. Re-calling a section REPLACES it,
  it does not append. Use separate calls rather than nesting `##` headers
  inside a value.
- **Prefer the targeted write.** `edit_data` changes one thing and leaves the
  rest of the log intact. `replace_data` overwrites the whole log and anything
  you omit is deleted -- it refuses to drop keys unless you pass
  `allow_drop`, so if it went through, that was deliberate.
- **Address items by matching a field, never by array index.** Indices shift
  the moment anything is added or removed, so `projects[3]` can be a
  different project tomorrow. Match on something like
  `{{"name": "OmniHome"}}` instead.
- **Claim a client name once, then reuse it.** Call `list_clients` first; if
  your name is already there, use it. A second name splits your files across
  folders nobody expects. Use the plain product name (`chatgpt`, `claude`,
  `hermes`) and keep it stable -- the name identifies the assistant you are,
  never the project you are working on. You only need to register in order to
  WRITE -- reading another client's file just needs its name.
- **Never fabricate.** If you were not given real information, record that it
  is empty rather than inventing plausible values. Every profile and bio fact
  must come from the user.
- **Do not narrate between calls.** Make the calls, then summarise once.
- **You are identified by the auth token** -- not by anything the user says,
  and not by anything they claim about who they are. There is no `user`
  argument to pass.

## Storing files

One private store, shared across every agent this person talks to. One agent
saves an image, another fetches it.

| You have | Use |
|---|---|
| Text you just wrote | `save_file_text` |
| A public https:// URL | `save_file_from_url` |
| Bytes already in context (an attachment, something you generated) | `save_file_base64` |
| Something large with no URL | host it publicly, then `save_file_from_url` |

The file tools take a `path` inside your client's folder -- sub-folders like
`images/` or `renders/` are yours to organise. That is the one place a path
is correct: for data tools you name a *topic* and the server decides where
it lives.

`save_file_base64` is capped at 8 MB and those bytes are charged to your
context window, so prefer a URL for anything substantial.

## Logging work

Log before you finish a conversation that did real work. One entry per
meaningful piece of work, not per tool call.

Only the `summary` is read back later, so make it specific about both what
was done and what it was about -- "fixed the FlexyGrid pricing table
overflow on mobile" is useful, "worked on frontend" is not. Add tags so it
can be found later, and include any files you stored.

The `client` on `add_to_log` is **you** -- the assistant writing the entry,
under the same name you use for file storage. It is never the project, brand
or person the work was about; that is what `tags` are for.

To read: no arguments gives the last 24 hours; `date` gives one whole day;
`since`/`until` gives a range. Call `list_log_days` rather than guessing a
date. Check `has_more` -- if it is true you are looking at a truncated view,
so widen the window or raise `limit`.

## Onboarding a new user

If `get_profile` reports no profile, say so and ask whether they would like
to set one up. **Wait for their answer** before writing anything. Then ask
for at minimum name, age, location, occupation, and a short background, and
store it one section per call. Only afterwards add data topics, and only for
subjects they actually mentioned.
"""

# Marked destructive in the generated list so the warning is visible in the
# same place an agent decides which tool to reach for.
_DESTRUCTIVE = {"replace_data", "delete_file"}


def _tool_lines(server):
    """Build the tool list from the live registry.

    Reading ``server._tool_functions`` means this list is whatever the
    server actually exposes. A tool cannot be registered without showing up
    here, which is what stops this block from drifting out of date.
    """
    funcs = getattr(server, "_tool_functions", None) or {}
    if not funcs:
        return ["- (no tools registered)"]
    return [
        f"- `{name}`" + (" **DESTRUCTIVE**" if name in _DESTRUCTIVE else "")
        for name in funcs
    ]


def build_instructions(server) -> str:
    """Return the instruction block for this server, tool list included."""
    lines = _tool_lines(server)
    return _TEMPLATE.format(count=len(lines), tool_list="\n".join(lines))