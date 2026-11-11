"""Prompts: ready-made requests the user picks from their client's menu.

A tool is something the model decides to call. A prompt is something the
PERSON chooses -- "Daily review" in a slash menu -- and it arrives as a
message already worded to use this server's tools well. They cost nothing
until picked, unlike the instructions, which ride along on every request.

Each function returns the text of that message. Nothing here reads storage:
the prompt tells the model which tools to call, and the tools enforce who
may read what.
"""


def daily_review(date: str = "") -> str:
    day = f"on {date}" if date else "today"
    arg = f'date="{date}"' if date else "no arguments, then narrow to today"
    return (
        f"Give me a review of what I did {day}.\n\n"
        "1. Call `whoami` for my timezone and today's date.\n"
        f"2. Call `get_log` ({arg}). If `has_more` is true, raise `limit` "
        "until you have the whole day.\n"
        "3. Group the entries by what they were about, not by time. Say "
        "plainly what got done, what was started but not finished, and "
        "anything that repeats from earlier days.\n"
        "4. If the log is empty, say so -- do not fill it in from memory or "
        "from agents' journals.\n\n"
        "Keep it short: a few lines per subject, no preamble.")


def weekly_review() -> str:
    return (
        "Give me a review of my last seven days.\n\n"
        "1. Call `whoami` for today's date in my timezone.\n"
        "2. Call `get_log` with `since` set to seven days ago and "
        "`limit=500`.\n"
        "3. Call `get_memory` for my current focus, and `list_topics` so "
        "you can check the week against the projects I track.\n"
        "4. Tell me: where the time went, by subject; what moved forward; "
        "what I said I was focused on but did not touch; and one thing "
        "worth doing next week.\n\n"
        "Only use what the log says. Where a day has no entries, say it has "
        "none rather than guessing.")


def find_in_my_data(what: str) -> str:
    return (
        f"Find what I have stored about: {what}\n\n"
        f"Call `search` with query \"{what}\". If nothing matches, try the "
        "single most distinctive word. For each useful hit, read the real "
        "thing -- `get_log` for that date, `get_data` for that topic, "
        "`get_file` for that file -- and tell me what it says and where it "
        "is. If there is nothing, say so.")


def pick_up_where_i_left_off() -> str:
    return (
        "You are an agent resuming work. Before doing anything else:\n\n"
        "1. Call `whoami` and note `you_are` -- that is your name here.\n"
        "2. Call `get_agent_log` (no arguments) for your own journal of "
        "the last 24 hours; if it is empty, widen `since` to a week.\n"
        "3. Call `get_memory` for the user's current focus and next step.\n"
        "4. Tell me in a few lines what you were last doing, what is "
        "unfinished, and what you propose to do now. Wait for my answer "
        "before changing anything.")


def set_me_up() -> str:
    return (
        "Help me set up my profile.\n\n"
        "Call `whoami`. If I already have a profile, show me what is there "
        "with `get_profile` and ask what I want to change. If I do not, ask "
        "me for my name, location, timezone, occupation and a short "
        "background -- a few questions at a time -- and store each with "
        "`update_profile`, one field per call. Set `timezone` as an IANA "
        "name such as Asia/Kolkata. Store only what I actually tell you.")


def register(server):
    """Attach the library to an MCPServer."""
    server.prompt(
        "daily_review", title="Daily review",
        description="What I did today, or on a given date, from my log.",
        arguments=[{"name": "date", "description": "YYYY-MM-DD. Leave out "
                    "for today.", "required": False}])(daily_review)
    server.prompt(
        "weekly_review", title="Weekly review",
        description="Where my last seven days went, against my stated "
                    "focus.")(weekly_review)
    server.prompt(
        "find_in_my_data", title="Find in my data",
        description="Search everything stored about me for a subject.",
        arguments=[{"name": "what", "description": "What to look for.",
                    "required": True}])(find_in_my_data)
    server.prompt(
        "pick_up_where_i_left_off", title="Pick up where I left off",
        description="For an agent starting a session: read its own journal "
                    "and report before acting.")(pick_up_where_i_left_off)
    server.prompt(
        "set_me_up", title="Set up my profile",
        description="Create or update my profile, one question at a "
                    "time.")(set_me_up)
