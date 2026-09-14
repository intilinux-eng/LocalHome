"""Command-line control for covers, useful for scripting/cron/testing
without the web dashboard. Also has `doctor`, a config sanity-checker -
see _run_doctor() below, and docs/configuration.md#checking-a-new-setup-with-doctor.

Run as `python -m localhome.cli <command>` or, after `pip install -e .`,
as `localhome-cli <command>`.
"""
from __future__ import annotations

import argparse
import logging
import sys

from localhome.config import LocalHomeConfig, load_config
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


def _run_doctor(config: LocalHomeConfig, manager: DeviceManager) -> int:
    """Cross-checks config.yaml references that DeviceManager itself
    can't catch: a driver that fails to *construct* (bad credentials, a
    missing file) already gets logged loudly, but a *name* that simply
    doesn't match anything - a typo in thermostat.zones[].sensor, a
    paired_switch pointing at the wrong switch - fails silently (the
    thermostat just never acts on that zone, the number just never gets
    embedded), which is a rough way to discover a typo in a new install.
    """
    problems: list[str] = []

    thermostat_cfg = config.raw.get("thermostat") or {}
    for zone in thermostat_cfg.get("zones", []):
        zone_name = zone.get("name", "<unnamed>")
        sensor, valve = zone.get("sensor"), zone.get("valve")
        if sensor not in manager.pollers:
            problems.append(f"thermostat zone '{zone_name}': sensor '{sensor}' matches no configured integration")
        elif manager.sensor_kinds.get(sensor) != "climate":
            problems.append(
                f"thermostat zone '{zone_name}': sensor '{sensor}' is kind "
                f"'{manager.sensor_kinds.get(sensor)}', expected 'climate'"
            )
        if valve not in manager.switches:
            problems.append(f"thermostat zone '{zone_name}': valve '{valve}' matches no configured switch")

    for number_name, switch_name in manager.paired_switch.items():
        if switch_name not in manager.switches:
            problems.append(f"number '{number_name}': paired_switch '{switch_name}' matches no configured switch")

    energy_cfg = config.raw.get("energy") or {}
    if energy_cfg.get("sensor") and energy_cfg["sensor"] not in manager.pollers:
        problems.append(f"energy.sensor '{energy_cfg['sensor']}' matches no configured integration")

    other_sensors = len(manager.pollers) - len(manager.switches) - len(manager.numbers)
    print(f"Config: {config.path}")
    print(
        f"Covers: {len(manager.covers)}  Switches: {len(manager.switches)}  "
        f"Numbers: {len(manager.numbers)}  Other sensors: {other_sensors}"
    )
    if thermostat_cfg:
        print(f"Thermostat zones: {len(thermostat_cfg.get('zones', []))} (mode: {thermostat_cfg.get('mode', 'heat')})")
    print(f"Notifier configured: {'yes' if manager.notifier else 'no'}")

    if problems:
        print(f"\n{len(problems)} problem(s) found:")
        for problem in problems:
            print(f"  x {problem}")
        print(
            "\nNote: a driver that failed to *construct* (bad credentials, a missing "
            "devices_file/secrets_file) is logged above as it happened, not repeated here."
        )
        return 1

    print("\nNo configuration problems found.")
    return 0


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Control LocalHome covers from the command line")
    parser.add_argument("--config", help="Path to config.yaml (defaults to config/config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List configured covers")

    p_status = sub.add_parser("status", help="Show the status of a cover")
    p_status.add_argument("name")

    for cmd in ("open", "close", "stop"):
        p = sub.add_parser(cmd, help=f"{cmd.capitalize()} a cover (name or 'all')")
        p.add_argument("name")

    sub.add_parser(
        "doctor",
        help="Validate config.yaml: cross-checks thermostat/paired_switch/energy name references "
        "and reports what got configured. Exits non-zero if it finds a problem.",
    )

    args = parser.parse_args(argv)
    config = load_config(args.config)
    manager = DeviceManager(config)

    if args.command == "doctor":
        sys.exit(_run_doctor(config, manager))

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
