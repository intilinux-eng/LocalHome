"""Minimal eWeLink cloud client (v2 API) — only used once to fetch each
device's devicekey, which the LAN protocol then uses for AES decryption.
No ongoing cloud dependency after that.

Endpoint/signing derived from the actively maintained AlexxIT/SonoffLAN
project; the app id/secret are the ones the official eWeLink app itself
uses, reverse-engineered by that community project (not our own API key).
"""
import base64
import hashlib
import hmac
import json

import aiohttp

APP_ID = "R8Oq3y0eSZSYdKccHlrQzT1ACCOUT9Gv"
APP_SECRET = b"1ve5Qk9GXfUhKAn1svnKwpAlxXkMarru"

API_HOSTS = {
    "cn": "https://cn-apia.coolkit.cn",
    "as": "https://as-apia.coolkit.cc",
    "us": "https://us-apia.coolkit.cc",
    "eu": "https://eu-apia.coolkit.cc",
}


def _sign(data: bytes) -> str:
    digest = hmac.new(APP_SECRET, data, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class EweLinkCloud:
    def __init__(self):
        self.region = None
        self.token = None

    async def _post_login(self, session, data, headers):
        url = API_HOSTS[self.region] + "/v2/user/login"
        async with session.post(url, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()

    async def login(self, username: str, password: str, region: str = "eu", country_code: str = "+39") -> bool:
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
