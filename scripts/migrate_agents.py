#!/usr/bin/env python3
"""Clean up client names and move agents' entries out of your log.

DRY RUN BY DEFAULT: nothing is written unless you pass --apply. With --apply,
every log day is copied to users/<id>/backups/logs-<timestamp>/ before it is
rewritten.

See what is there:
    python scripts/migrate_agents.py --handle joydip --summary

Fold duplicate names into one (folder, registry and log attribution):
    python scripts/migrate_agents.py --handle joydip \\
        --merge hermes-atlas=atlas --merge atlas-gate=atlas --merge hermes-cleo=cleo

Move agents' bookkeeping (entries tagged tick, route, digest, ...) out of
your log and into each agent's own journal:
    python scripts/migrate_agents.py --handle joydip --move-agent-logs

Move EVERYTHING certain agents wrote, whatever its tags:
    python scripts/migrate_agents.py --handle joydip --move-agent-logs \\
        --all-from atlas,bolt,cleo,dara,ezra

Add --apply to any of the last three to actually do it. Run it while the
agents are idle: a day an agent writes to mid-run is skipped and reported,
and you can simply run the command again.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from joyverse import config as jv_config  # noqa: E402
from joyverse import identity, maintenance  # noqa: E402


def _pairs(values) -> dict:
    out = {}
    for value in values or []:
        if "=" not in value:
            raise SystemExit(f"--merge expects OLD=NEW, got {value!r}")
        old, new = value.split("=", 1)
        out[old.strip()] = new.strip()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge duplicate client names and move agent log "
                    "entries into agent journals. Dry run unless --apply.")
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--handle", help="bearer handle of the account")
    who.add_argument("--user-id", help="the account's user id (u_...)")
    parser.add_argument("--summary", action="store_true",
                        help="show client names, folders and log counts")
    parser.add_argument("--merge", action="append", metavar="OLD=NEW",
                        help="fold client OLD into NEW (repeatable)")
    parser.add_argument("--move-agent-logs", action="store_true",
                        help="move operational entries to agent journals")
    parser.add_argument("--all-from", default="", metavar="A,B,C",
                        help="with --move-agent-logs: move every entry by "
                             "these clients, not only operational ones")
    parser.add_argument("--apply", action="store_true",
                        help="actually write the changes")
    args = parser.parse_args()

    user_id = args.user_id or identity.lookup_handle(args.handle)
    if not user_id or not identity.is_user_id(user_id):
        print("Error: no such account.", file=sys.stderr)
        return 1

    r2, bucket = jv_config.get_r2_client(), jv_config.BUCKET_NAME
    merges = _pairs(args.merge)
    did_something = False

    if args.summary or not (merges or args.move_agent_logs):
        print(json.dumps(maintenance.client_summary(r2, bucket, user_id),
                         indent=2))
        did_something = True

    if merges:
        report = maintenance.merge_clients(r2, bucket, user_id, merges,
                                           apply=args.apply)
        print("\n== merge clients ==")
        print(json.dumps(report, indent=2))
        did_something = True

    if args.move_agent_logs:
        report = maintenance.move_agent_logs(
            r2, bucket, user_id,
            all_from=[c for c in args.all_from.split(",") if c.strip()],
            rename=merges, apply=args.apply)
        print("\n== move agent logs ==")
        print(json.dumps(report, indent=2))
        did_something = True

    if did_something and not args.apply and (merges or args.move_agent_logs):
        print("\nDRY RUN -- nothing was changed. Add --apply to do it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
