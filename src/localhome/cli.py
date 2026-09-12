"""Command-line control for covers, useful for scripting/cron/testing
without the web dashboard.

Run as `python -m localhome.cli <command>` or, after `pip install -e .`,
as `localhome-cli <command>`.
"""
from __future__ import annotations

import argparse
import sys

from localhome.config import load_config
from localhome.core.manager import DeviceManager


def _find_covers(manager: DeviceManager, name: str):
    if name.lower() == "all":
        return list(manager.covers.values())
    cover = manager.covers.get(name)
    if cover is not None:
        return [cover]
    matches = [c for c in manager.covers.values() if name.lower() in c.name.lower()]
    if not matches:
        print(f"No cover found for '{name}'. Use 'list' to see available names.", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"Ambiguous name '{name}', matches: {[c.name for c in matches]}", file=sys.stderr)
        sys.exit(1)
    return matches


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Control LocalHome covers from the command line")
    parser.add_argument("--config", help="Path to config.yaml (defaults to config/config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List configured covers")

    p_status = sub.add_parser("status", help="Show the status of a cover")
    p_status.add_argument("name")

    for cmd in ("open", "close", "stop"):
        p = sub.add_parser(cmd, help=f"{cmd.capitalize()} a cover (name or 'all')")
        p.add_argument("name")

    args = parser.parse_args(argv)
    config = load_config(args.config)
    manager = DeviceManager(config)

    if args.command == "list":
        for name in manager.covers:
            print(f"- {name}")
        return

    if args.command == "status":
        (cover,) = _find_covers(manager, args.name)
        status = cover.status()
        print(f"{cover.name}: state='{status['state']}'")
        return

    for cover in _find_covers(manager, args.name):
        cover.send(args.command)
        print(f"{cover.name}: sent '{args.command}'")


if __name__ == "__main__":
    main()
