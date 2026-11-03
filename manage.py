"""Small command-line tool to inspect and fix the tracked-paper registry.

Examples:
    python manage.py show 2605.15029
    python manage.py list --awaiting
    python manage.py set 2605.15029 --unpublished
    python manage.py set 2611.00272 --posted --published

`set` writes flags exactly as given, so it can also turn them off, which the
bot itself never does. Don't run it while the bot is running.
"""

import argparse
import json
import sys
from dataclasses import asdict

from registry import PaperRegistry
from settings import AppSettings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Inspect and fix the tracked-paper registry.")
    parser.add_argument("--registry", default=AppSettings().posted_papers_file, help="Registry file (default: posted_papers.txt next to the bot).")
    commands = parser.add_subparsers(dest="command", required=True)

    show = commands.add_parser("show", help="Print one record.")
    show.add_argument("paper_id")

    listing = commands.add_parser("list", help="List records.")
    listing.add_argument("--awaiting", action="store_true", help="Only papers still waiting for a publication announcement.")

    set_flags = commands.add_parser("set", help="Set posted/published flags.")
    set_flags.add_argument("paper_id")
    posted = set_flags.add_mutually_exclusive_group()
    posted.add_argument("--posted", dest="posted", action="store_true", default=None)
    posted.add_argument("--unposted", dest="posted", action="store_false")
    published = set_flags.add_mutually_exclusive_group()
    published.add_argument("--published", dest="published", action="store_true", default=None)
    published.add_argument("--unpublished", dest="published", action="store_false")

    args = parser.parse_args(argv)
    # Titles can hold characters the Windows console encoding lacks (e.g. Greek).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    registry = PaperRegistry.load(args.registry)

    if args.command == "show":
        if args.paper_id not in registry:
            print(f"{args.paper_id} is not in the registry.", file=sys.stderr)
            return 1
        print(json.dumps(asdict(registry.get(args.paper_id)), indent=2, ensure_ascii=False))
    elif args.command == "list":
        records = registry.awaiting_publication().items() if args.awaiting else registry.items()
        for paper_id, record in sorted(records):
            flags = f"{'posted' if record.posted else '-':6} {'published' if record.published else '-':9}"
            print(f"{paper_id:12} {flags} {(record.title or '')[:70]}")
    elif args.command == "set":
        if args.posted is None and args.published is None:
            parser.error("set needs at least one of --posted/--unposted/--published/--unpublished")
        if args.paper_id not in registry:
            print(f"Note: {args.paper_id} was not in the registry; adding it.", file=sys.stderr)
        record = registry.set_flags(args.paper_id, posted=args.posted, published=args.published)
        registry.save()
        print(f"{args.paper_id}: posted={record.posted}, published={record.published}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
