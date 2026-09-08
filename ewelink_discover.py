#!/usr/bin/env python3
import asyncio
import json

from ewelink_cloud import EweLinkCloud


async def main():
    creds = json.load(open("secrets_ewelink.json"))
    cloud = EweLinkCloud()
    await cloud.login(
        creds["username"], creds["password"],
        region=creds.get("region", "eu"),
        country_code=creds.get("country_code", "+39"),
    )
    print("Login ok, region:", cloud.region)

    devices = await cloud.get_devices()
    print(f"Found {len(devices)} device(s)")
    for d in devices:
        print(f"- {d.get('name')} | device_id: {d['deviceid']} | model: {d.get('productModel')}")
        print("  params:", d.get("params"))


if __name__ == "__main__":
    asyncio.run(main())
