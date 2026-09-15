"""Shelly Gen2/Gen3 RPC-over-MQTT request/response helper.

Most generic MQTT drivers in this package (sensor/switch/number/cover)
are passive: subscribe to one topic, cache whatever gets published to it.
That works well for a device that republishes its full state on every
change - Shelly's own H&T sensor does exactly that on dedicated
`status/<component>:<id>` topics.

Not every Shelly component does, though - confirmed by hand against a
real 0/1-10V Dimmer Gen3 (see docs/integrations/mqtt.md): its `light:0`
component never appears on a retained `status/light:0` topic. It only
shows up in transient `events/rpc` "NotifyStatus" messages, and those are
a fragile single source to cache passively - a later notification about
an unrelated component (`sys`, `wifi`, ...) is a *different* JSON payload
that simply doesn't mention `light:0`, so naively caching "the last
message on this topic" would blank out a field that hasn't actually
changed.

Since a component like this is mains-powered and always connected, the
robust fix is to not rely on it announcing itself at all: actively ask,
using the same request/response RPC pattern Shelly's own app and cloud
integration use - publish `{"id", "src", "method", "params"}` to
`<device_id>/rpc`, the device replies on `<src>/rpc`.
"""
from __future__ import annotations

import itertools
import json
import threading
from typing import Any

from localhome.drivers.mqtt.client import get_connection

_next_id = itertools.count(1)


class ShellyRpcClient:
    def __init__(self, broker: dict, device_id: str, reply_id: str = "localhome", timeout: float = 5.0):
        self._device_id = device_id
        self._request_topic = f"{device_id}/rpc"
        self._reply_topic = f"{reply_id}/rpc"
        self._reply_id = reply_id
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self._connection = get_connection(broker)
        self._connection.subscribe(self._reply_topic, self._on_reply)

    def _on_reply(self, topic: str, payload: bytes) -> None:
        try:
            data = json.loads(payload)
        except ValueError:
            return
        with self._lock:
            entry = self._pending.get(data.get("id"))
        if entry is not None:
            event, holder = entry
            holder["reply"] = data
            event.set()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = next(_next_id)
        event = threading.Event()
        holder: dict[str, Any] = {}
        with self._lock:
            self._pending[request_id] = (event, holder)
        try:
            request = {"id": request_id, "src": self._reply_id, "method": method}
            if params is not None:
                request["params"] = params
            self._connection.publish(self._request_topic, json.dumps(request))
            if not event.wait(self._timeout):
                raise RuntimeError(
                    f"no RPC reply from '{self._device_id}' for {method} within {self._timeout:.0f}s - "
                    "device is probably offline"
                )
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        reply = holder["reply"]
        if "error" in reply:
            raise RuntimeError(f"RPC error from '{self._device_id}' for {method}: {reply['error']}")
        return reply.get("result") or {}
