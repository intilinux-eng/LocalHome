# Third-party notices

LocalHome itself is licensed under the [GNU Affero General Public License
v3.0 or later](LICENSE). All of its runtime dependencies are under
permissive licenses compatible with it, adding no obligations of their own
on top of the AGPL's:

| Package      | License        | Used for |
|---------------|-----------------|----------|
| [tinytuya](https://github.com/jasonacox/tinytuya)   | MIT             | Local LAN control of Tuya roller shutter modules |
| [Flask](https://github.com/pallets/flask)           | BSD-3-Clause    | The web dashboard/API |
| [Waitress](https://github.com/Pylons/waitress)      | ZPL-2.1         | The production web server the dashboard/API runs on |
| [aiohttp](https://github.com/aio-libs/aiohttp)       | Apache-2.0 / MIT | eWeLink cloud HTTP/WebSocket client |
| [meross-iot](https://github.com/albertogeniola/MerossIot) | MIT       | Meross smartplug cloud/MQTT client |
| [PyYAML](https://github.com/yaml/pyyaml)             | MIT             | Reading config.yaml |
| [paho-mqtt](https://github.com/eclipse-paho/paho.mqtt.python) | EPL-2.0 OR BSD-3-Clause (dual-licensed; used here under the BSD-3-Clause option) | The generic MQTT driver family |
| [msgpack](https://github.com/msgpack/msgpack-python) | Apache-2.0      | RPC to Arduino MCU bridge (LED matrix) |
| [paramiko](https://github.com/paramiko/paramiko)     | LGPL-2.1        | `tools/deploy_board.py` / deploy tools on the PC - optional (`.[deploy]`), never installed on the board |

None of their source is vendored into this repository; they're declared
as ordinary pip dependencies in `pyproject.toml`/`requirements.txt` and
installed from PyPI, so their own license files travel with their own
distributions.

## A note on the eWeLink driver specifically

`src/localhome/drivers/ewelink/cloud.py` talks to eWeLink's cloud API
using an app ID/secret pair that identifies the *official eWeLink
mobile app*, not a key issued to this project by Coolkit/eWeLink. There
is no public developer program that issues these for the POWCT's
cloud-polling use case, so this - like several well-known open-source
projects before it (most notably
[AlexxIT/SonoffLAN](https://github.com/AlexxIT/SonoffLAN), which this
driver's endpoint/signing logic is derived from) - relies on values that
are the same across every install of the official app, and have been
independently rediscovered and published by that and other community
projects rather than obtained under any confidentiality obligation to
Coolkit.

This is a common, long-standing pattern in the home-automation
interoperability community, but it is **not sanctioned by Coolkit/eWeLink**,
isn't covered by any license grant from them, and may be inconsistent
with their Terms of Service. LocalHome is not affiliated with, endorsed
by, or sponsored by Coolkit, eWeLink, Sonoff, Tuya, Meross or LoraTap -
all trademarks belong to their respective owners. If you use
the eWeLink driver, you're relying on the same interoperability
precedent SonoffLAN and similar projects have operated under for years,
at your own risk; if that's not a tradeoff you want, skip that one
integration (everything else in this project stays fully local and
doesn't raise this question).
