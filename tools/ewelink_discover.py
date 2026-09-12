#!/usr/bin/env python3
"""One-off diagnostic: log into the eWeLink cloud and print every device on
the account, with its device_id/model/raw params - useful for finding the
device_id to put in config/ewelink_devices.json (see
docs/integrations/ewelink.md).

Usage: python tools/ewelink_discover.py [path/to/secrets_ewelink.json]
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from localhome.drivers.ewelink.cloud import EweLinkCloud


async def main():
    secrets_path = sys.argv[1] if len(sys.argv) > 1 else "config/secrets_ewelink.json"
    creds = json.load(open(secrets_path, encoding="utf-8"))
    cloud = EweLinkCloud()
    await cloud.login(
        creds["username"], creds["password"],
        region=creds.get("region", "eu"),
        country_code=creds.get("country_code", "+1"),
    )
    print("Login ok, region:", cloud.region)

    devices = await cloud.get_devices()
    print(f"Found {len(devices)} device(s)")
    for d in devices:
        print(f"- {d.get('name')} | device_id: {d['deviceid']} | model: {d.get('productModel')}")
        print("  params:", d.get("params"))


if __name__ == "__main__":
    asyncio.run(main())
