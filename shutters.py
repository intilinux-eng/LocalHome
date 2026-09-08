#!/usr/bin/env python3
import argparse
import json
import sys

import tinytuya

DEVICES_FILE = "devices.json"
SHUTTER_CATEGORY = "clkg"


def load_shutters():
    with open(DEVICES_FILE) as f:
        devices = json.load(f)
    shutters = [d for d in devices if d.get("category") == SHUTTER_CATEGORY and d.get("ip")]
    if not shutters:
        print(f"No shutters found in {DEVICES_FILE} (each entry needs an 'ip' field)")
        sys.exit(1)
    return shutters


def get_device(shutter):
    d = tinytuya.Device(shutter["id"], shutter["ip"], shutter["key"], version=3.4)
    d.set_socketTimeout(5)
    return d


def find_shutter(shutters, name):
    name_lower = name.lower()
    exact = [s for s in shutters if s["name"].lower() == name_lower]
    if exact:
        return exact[0]
    matches = [s for s in shutters if name_lower in s["name"].lower()]
    if not matches:
        print(f"No shutter found for '{name}'. Use 'list' to see available names.")
        sys.exit(1)
    if len(matches) > 1:
        print(f"Ambiguous name '{name}', matches: {[s['name'] for s in matches]}")
        sys.exit(1)
    return matches[0]


def send_action(shutter, action):
    d = get_device(shutter)
    result = d.set_value(1, action)
    print(f"{shutter['name']}: sent '{action}' -> {result}")


def cmd_list(shutters):
    for s in shutters:
        print(f"- {s['name']}  (ip: {s['ip']})")


def cmd_status(shutters, name):
    shutter = find_shutter(shutters, name)
    d = get_device(shutter)
    status = d.status()
    dps = status.get("dps", {})
    state = dps.get("1", "unknown")
    travel_time = dps.get("10", "n/a")
    print(f"{shutter['name']}: state='{state}'  travel_time={travel_time}s")


def cmd_action(shutters, name, action):
    if name.lower() == "all":
        targets = shutters
    else:
        targets = [find_shutter(shutters, name)]
    for shutter in targets:
        send_action(shutter, action)


def main():
    parser = argparse.ArgumentParser(description="Control LoraTap roller shutters via tinytuya")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List available shutters")

    p_status = sub.add_parser("status", help="Show the status of a shutter")
    p_status.add_argument("name")

    p_open = sub.add_parser("open", help="Open a shutter (name or 'all')")
    p_open.add_argument("name")

    p_close = sub.add_parser("close", help="Close a shutter (name or 'all')")
    p_close.add_argument("name")

    p_stop = sub.add_parser("stop", help="Stop a shutter (name or 'all')")
    p_stop.add_argument("name")

    args = parser.parse_args()
    shutters = load_shutters()

    if args.command == "list":
        cmd_list(shutters)
    elif args.command == "status":
        cmd_status(shutters, args.name)
    else:
        cmd_action(shutters, args.name, args.command)


if __name__ == "__main__":
    main()
