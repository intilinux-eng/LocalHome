# Third-party notices

LocalHome itself is [MIT licensed](LICENSE). Most of its dependencies
are under permissive licenses compatible with MIT, imposing no
copyleft/share-alike obligations on this project - one exception,
`python-kasa`, is called out separately below the table:

| Package      | License        | Used for |
|---------------|-----------------|----------|
| [tinytuya](https://github.com/jasonacox/tinytuya)   | MIT             | Local LAN control of Tuya-based devices (covers, air-quality sensor) |
| [Flask](https://github.com/pallets/flask)           | BSD-3-Clause    | The web dashboard/API |
| [Waitress](https://github.com/Pylons/waitress)      | ZPL-2.1         | The production web server the dashboard/API runs on |
| [aiohttp](https://github.com/aio-libs/aiohttp)       | Apache-2.0 / MIT | eWeLink cloud HTTP/WebSocket client |
| [meross-iot](https://github.com/albertogeniola/MerossIot) | MIT       | Meross smartplug cloud/MQTT client |
| [requests](https://github.com/psf/requests)          | Apache-2.0      | Telegram notifications |
| [PyYAML](https://github.com/yaml/pyyaml)             | MIT             | Reading config.yaml |
| [paho-mqtt](https://github.com/eclipse-paho/paho.mqtt.python) | EPL-2.0 OR BSD-3-Clause (dual-licensed; used here under the BSD-3-Clause option) | The generic MQTT driver family |
| [python-kasa](https://github.com/python-kasa/python-kasa) | **GPL-3.0-or-later** - see below | TP-Link Kasa/Tapo LED strip control (`tplink_led_strip`) |

None of their source is vendored into this repository; they're declared
as ordinary pip dependencies in `pyproject.toml`/`requirements.txt` and
installed from PyPI, so their own license files travel with their own
distributions.

## A note on `python-kasa` specifically

Unlike every other dependency above, `python-kasa` (used only by the
`tplink_led_strip` driver) is **GPL-3.0-or-later**, a copyleft license -
not permissive, and not something this notice can just wave through the
way it does for the MIT/BSD/Apache packages. Because of that, it's kept
out of the base install on purpose: it lives in its own `tplink` extra
in `pyproject.toml` (`pip install -e ".[tplink]"`) rather than the main
`dependencies` list, and `core/registry.py`'s driver loader tolerates it
not being installed (logs it, skips that one driver, starts everything
else normally). A plain `pip install -e .`/`pip install -r
requirements.txt` never pulls in GPL-licensed code at all - you only
get it if you deliberately opt into TP-Link support.

The FSF's own stated position is that a program which calls into a GPL
library and is distributed together with it forms a combined work that,
*as distributed*, needs to be under GPL-compatible terms as a whole -
not just the GPL library's own files. Running LocalHome yourself, or
pointing your own checkout/fork at your own devices, isn't the kind of
"distribution to a third party" that typically triggers this; where it
plausibly does matter is packaging and handing this project (with the
`tplink` extra installed) to someone else - a built image, a
redistributed fork, anything beyond "clone the repo and run it
yourself." If that's a distinction you care about: simply never install
the `tplink` extra, and this project's own dependency tree stays
entirely under permissive licenses. This isn't legal advice; if the
distinction actually matters for how you plan to use or redistribute
this project, check with someone qualified to advise on it.

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
by, or sponsored by Coolkit, eWeLink, Sonoff, Tuya, Meross, LoraTap, or
Telegram - all trademarks belong to their respective owners. If you use
the eWeLink driver, you're relying on the same interoperability
precedent SonoffLAN and similar projects have operated under for years,
at your own risk; if that's not a tradeoff you want, skip that one
integration (everything else in this project stays fully local and
doesn't raise this question).
