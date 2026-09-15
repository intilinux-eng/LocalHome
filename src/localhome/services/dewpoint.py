"""Dew point from temperature + relative humidity - the Magnus-Tetens
approximation (Sonntag 1990's constants, accurate to within ~0.35°C over
the ordinary indoor range this project cares about), used on the
climate tab to flag condensation risk while cooling: a zone's radiant
panel/circuit running colder than the room's own dew point will sweat,
independent of whatever target temperature the thermostat is chasing -
see docs/configuration.md and the `cool_enabled` note in
services/thermostat.py for the same underlying concern on the bathroom
zone's own valve.
"""
from __future__ import annotations

import math

_B = 17.62
_C = 243.12  # °C


def dew_point_c(temperature_c: float, humidity_pct: float) -> float:
    gamma = math.log(humidity_pct / 100.0) + (_B * temperature_c) / (_C + temperature_c)
    return (_C * gamma) / (_B - gamma)
