"""Shelly Gen2/Gen3 "Light" component over RPC-over-MQTT: covers both a
real dimmable light and a 0/1-10V analog output add-on used to modulate
something else (a VMC's speed, say) - Shelly exposes both the same way,
as a `light:0` component with an `output` (on/off) and a `brightness`
(0-100) field. See drivers/mqtt/rpc.py for why this polls via RPC instead
of the passive subscribe-and-cache pattern the other generic MQTT drivers
use, and docs/integrations/mqtt.md for the real device this was verified
against.

Registers under both `switch` (on/off) and `number` (brightness) kinds,
same "two config.yaml entries, one physical device" split as
drivers/tplink/led_strip.py - use `paired_switch` on the number entry so
the dashboard shows one card with a toggle and a slider instead of two.

config.yaml options:
  broker_file (or inline broker: {...}): connection details
  device_id: the Shelly RPC id, e.g. "shelly0110dimg3-XXXXXXXXXXXX" -
             visible on the device's own web UI / status page
  component_id: which light component, default 0 (a single-channel
               dimmer only ever has one)
  min_value / max_value / unit: number-kind only, defaults 0-100 "%"

Example - a Shelly 0/1-10V Dimmer Gen3 modulating a VMC's speed:
  kind: switch
  type: shelly_light
  name: "VMC Dimmer"
  broker_file: "mqtt_broker.json"
  device_id: "shelly0110dimg3-XXXXXXXXXXXX"
  dashboard_tab: "home"
  kind: number
  type: shelly_light
  name: "VMC Dimmer Speed"
  broker_file: "mqtt_broker.json"
  device_id: "shelly0110dimg3-XXXXXXXXXXXX"
  paired_switch: "VMC Dimmer"
  dashboard_tab: "home"
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import NumberDriver, SwitchDriver
from localhome.core.registry import register_driver
from localhome.drivers.mqtt.client import load_broker_config
from localhome.drivers.mqtt.rpc import ShellyRpcClient


class ShellyLightSwitchDriver(SwitchDriver):
    def __init__(
        self,
        name: str,
        broker: dict,
        device_id: str,
        component_id: int = 0,
        poll_interval_seconds: float = 20.0,
    ):
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds
        self.history_fields = ()
        self._component_id = component_id
        self._rpc = ShellyRpcClient(broker, device_id)

    def read(self) -> dict[str, Any]:
        result = self._rpc.call("Light.GetStatus", {"id": self._component_id})
        return {"ok": True, "name": self.name, "is_on": bool(result.get("output"))}

    def turn_on(self) -> None:
        self._rpc.call("Light.Set", {"id": self._component_id, "on": True})

    def turn_off(self) -> None:
        self._rpc.call("Light.Set", {"id": self._component_id, "on": False})


class ShellyLightNumberDriver(NumberDriver):
    def __init__(
        self,
        name: str,
        broker: dict,
        device_id: str,
        component_id: int = 0,
        min_value: float = 0.0,
        max_value: float = 100.0,
        unit: str = "%",
        poll_interval_seconds: float = 20.0,
    ):
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds
        self.min_value = min_value
        self.max_value = max_value
        self.unit = unit
        self.history_fields = ("value",)
        self._component_id = component_id
        self._rpc = ShellyRpcClient(broker, device_id)

    def read(self) -> dict[str, Any]:
        result = self._rpc.call("Light.GetStatus", {"id": self._component_id})
        brightness = result.get("brightness")
        if brightness is None:
            raise RuntimeError(f"'{self.name}': no brightness field in Light.GetStatus reply")
        return {"ok": True, "name": self.name, "value": float(brightness)}

    def set_value(self, value: float) -> None:
        clamped = float(max(self.min_value, min(self.max_value, float(value))))
        self._rpc.call("Light.Set", {"id": self._component_id, "brightness": clamped})


def _create_switch(options: dict) -> ShellyLightSwitchDriver:
    return ShellyLightSwitchDriver(
        name=options["name"],
        broker=load_broker_config(options),
        device_id=options["device_id"],
        component_id=options.get("component_id", 0),
        poll_interval_seconds=options.get("poll_interval_seconds", 20.0),
    )


def _create_number(options: dict) -> ShellyLightNumberDriver:
    return ShellyLightNumberDriver(
        name=options["name"],
        broker=load_broker_config(options),
        device_id=options["device_id"],
        component_id=options.get("component_id", 0),
        min_value=options.get("min_value", 0.0),
        max_value=options.get("max_value", 100.0),
        unit=options.get("unit", "%"),
        poll_interval_seconds=options.get("poll_interval_seconds", 20.0),
    )


register_driver("switch", "shelly_light")(_create_switch)
register_driver("number", "shelly_light")(_create_number)
