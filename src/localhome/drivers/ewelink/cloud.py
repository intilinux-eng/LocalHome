"""Minimal eWeLink cloud client (v2 API).

Used by drivers/ewelink/power.py to poll a Sonoff POWCT's live
power/energy readings, and by its rolling 31-day hourly-kWh history (via
the websocket dispatch channel below - `getHoursKwh`/`hoursKwhData`, an
undocumented capability not present in the public v2 REST API, found by
inspecting this device's own params dict; see `get_hours_kwh`).

Endpoint/signing derived from the actively maintained AlexxIT/SonoffLAN
project; the app id/secret are the ones the official eWeLink app itself
uses, reverse-engineered by that community project (not a key issued to
this project). See ../../../../THIRD_PARTY_NOTICES.md for what that
means legally before relying on this driver.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import aiohttp

APP_ID = "R8Oq3y0eSZSYdKccHlrQzT1ACCOUT9Gv"
APP_SECRET = b"1ve5Qk9GXfUhKAn1svnKwpAlxXkMarru"

API_HOSTS = {
    "cn": "https://cn-apia.coolkit.cn",
    "as": "https://as-apia.coolkit.cc",
    "us": "https://us-apia.coolkit.cc",
    "eu": "https://eu-apia.coolkit.cc",
}

WS_DISPATCH_HOSTS = {
    "cn": "https://cn-dispa.coolkit.cn/dispatch/app",
    "as": "https://as-dispa.coolkit.cc/dispatch/app",
    "us": "https://us-dispa.coolkit.cc/dispatch/app",
    "eu": "https://eu-dispa.coolkit.cc/dispatch/app",
}


def _sign(data: bytes) -> str:
    digest = hmac.new(APP_SECRET, data, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class EweLinkCloud:
    def __init__(self):
        self.region = None
        self.token = None
        self.apikey = None

    async def _post_login(self, session, data, headers):
        url = API_HOSTS[self.region] + "/v2/user/login"
        async with session.post(url, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()

    async def login(self, username: str, password: str, region: str = "eu", country_code: str = "+1") -> bool:
        self.region = region
        payload = {"password": password, "countryCode": country_code}
        if "@" in username:
            payload["email"] = username
        else:
            payload["phoneNumber"] = username if username.startswith("+") else "+" + username

        data = json.dumps(payload).encode()
        headers = {
            "Authorization": "Sign " + _sign(data),
            "Content-Type": "application/json",
            "X-CK-Appid": APP_ID,
        }

        async with aiohttp.ClientSession() as session:
            resp = await self._post_login(session, data, headers)
            if resp.get("error") == 10004:
                self.region = resp["data"]["region"]
                resp = await self._post_login(session, data, headers)
            if resp.get("error") != 0:
                raise RuntimeError(f"eWeLink login failed: {resp}")
            self.token = resp["data"]["at"]
            self.apikey = resp["data"]["user"]["apikey"]
        return True

    async def get_devices(self) -> list:
        headers = {"Authorization": "Bearer " + self.token, "X-CK-Appid": APP_ID}
        url = API_HOSTS[self.region] + "/v2/device/thing"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params={"num": 0}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                resp = await r.json()
        if resp.get("error") != 0:
            raise RuntimeError(f"eWeLink get_devices failed: {resp}")
        return [item["itemData"] for item in resp["data"]["thingList"] if "deviceid" in item["itemData"]]

    async def get_hours_kwh(self, device: dict, start: int, end: int) -> list:
        """Fetch hourly Wh buckets [start, end] (inclusive) from the device's
        rolling ~31-day (744-hour) on-device history, via the same cloud
        websocket channel the official app uses to send commands.

        Returns a list of `end - start + 1` raw integers, oldest first. The
        index-to-wallclock-hour mapping is not documented; callers must
        calibrate it (e.g. against yesterday_kwh) before trusting it.
        """
        headers = {"Authorization": "Bearer " + self.token, "X-CK-Appid": APP_ID}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                WS_DISPATCH_HOSTS[self.region], headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                dispatch = await r.json()
            if dispatch.get("error") != 0:
                raise RuntimeError(f"eWeLink dispatch failed: {dispatch}")

            ws_url = f"wss://{dispatch['domain']}:{dispatch['port']}/api/ws"
            async with session.ws_connect(ws_url, heartbeat=90, timeout=10) as ws:
                ts = time.time()
                await ws.send_json({
                    "action": "userOnline",
                    "at": self.token,
                    "apikey": self.apikey,
                    "appid": APP_ID,
                    "nonce": str(int(ts / 100)),
                    "ts": int(ts),
                    "userAgent": "app",
                    "sequence": str(int(ts * 1000)),
                    "version": 8,
                })
                handshake = await ws.receive_json(timeout=10)
                if handshake.get("error") != 0:
                    raise RuntimeError(f"eWeLink WS handshake failed: {handshake}")

                sequence = str(int(time.time() * 1000))
                await ws.send_json({
                    "action": "update",
                    "apikey": device["apikey"],
                    "selfApikey": self.apikey,
                    "deviceid": device["deviceid"],
                    "params": {"getHoursKwh": {"start": start, "end": end}},
                    "userAgent": "app",
                    "sequence": sequence,
                })

                deadline = time.time() + 10
                while time.time() < deadline:
                    msg = await ws.receive(timeout=deadline - time.time())
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    data = json.loads(msg.data)
                    if data.get("sequence") != sequence:
                        continue
                    if data.get("error") not in (0, None):
                        raise RuntimeError(f"eWeLink getHoursKwh failed: {data}")
                    hex_data = data.get("config", {}).get("hoursKwhData")
                    if hex_data is None:
                        raise RuntimeError(f"eWeLink getHoursKwh: no hoursKwhData in {data}")
                    return [int(hex_data[i:i + 4], 16) for i in range(0, len(hex_data), 4)]

        raise RuntimeError("eWeLink getHoursKwh: timed out waiting for response")
