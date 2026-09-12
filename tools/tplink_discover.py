#!/usr/bin/env python3
"""One-off diagnostic: broadcast-discover every TP-Link Kasa/Tapo device on
the LAN and print its IP/alias/model - useful for finding the `host` to put
in config.yaml for the tplink_led_strip driver (see
docs/integrations/tplink.md).

If a device requires Tapo account credentials to identify itself fully,
pass them so it shows up with its alias/model instead of just an IP.

Usage: python tools/tplink_discover.py [--username U --password P]
"""
import argparse
import asyncio

from kasa import Discover


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", help="TP-Link account email (needed for Tapo devices)")
    parser.add_argument("--password", help="TP-Link account password (needed for Tapo devices)")
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args()

    devices = await Discover.discover(
        username=args.username, password=args.password, timeout=args.timeout
    )
    if not devices:
        print("No devices found. If this LAN uses Wi-Fi client isolation or the")
        print("device needs Tapo credentials, try again with --username/--password,")
        print("or check the IP directly in the Kasa/Tapo app's device info screen.")
        return

    print(f"Found {len(devices)} device(s):")
    for ip, dev in devices.items():
        try:
            await dev.update()
            print(f"- {ip} | {dev.alias} | {dev.model} | {dev.device_type}")
        except Exception as e:
            print(f"- {ip} | (could not read details: {e})")


if __name__ == "__main__":
    asyncio.run(main())
