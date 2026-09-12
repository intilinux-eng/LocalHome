# Tuya (covers + air-quality sensor)

Covers the `tuya_curtain_switch` (`cover`) and `tuya_air_quality`
(`climate`) drivers. Both talk to devices directly over your LAN via
[tinytuya](https://github.com/jasonacox/tinytuya) - fully local at
runtime. The Tuya *cloud* is only involved in the one-time setup step
below, to fetch each device's `local_key`.

## 1. Get Tuya cloud credentials (one-time)

tinytuya needs a **local_key** per device to decrypt LAN traffic. The
only way to get these keys is through a free Tuya IoT developer account.

### 1.1 Create a Tuya IoT account and Cloud Project

1. Sign up at [https://iot.tuya.com](https://iot.tuya.com) (free, no card
   required).
2. Go to **Cloud** -> **Development** -> **Create Cloud Project**.
3. Fill in a name, set Industry to "Smart Home", Development Method to
   "Custom".
4. **Data Center**: pick the region matching where your device-control
   app account is registered (e.g. "Central Europe Data Center" for most
   EU accounts). This choice cannot be changed later without recreating
   the project, and getting it wrong causes the errors in
   [Troubleshooting](#troubleshooting) below.
5. On the "Select Authorization" screen, accept the default APIs. Also
   subscribe (Free Trial) to **"Smart Home Basic Service"** under
   **Service API** - it's required for the app-account linking step
   below.

### 1.2 Link your Smart Life / Tuya Smart / OEM app account

Required - without it the cloud project cannot see any of your devices,
even with valid API credentials.

1. In your Cloud Project, go to the **Devices** tab.
2. Click the **"Link App Account"** sub-tab (not "Link My App", a
   different product). This shows a QR code meant to be scanned from
   *inside* your existing device-control app (Smart Life, Tuya Smart, or
   any Tuya white-label app - they all work the same way).
3. Open the app on your phone -> Profile/Settings -> the scan icon ->
   scan the QR code shown on the platform.
4. Back on the platform, the **Devices** tab should now list your
   devices.

### 1.3 Get the Access ID / Access Secret

In the Cloud Project's **Overview** tab, copy (use the copy icon, don't
retype by hand - see [Troubleshooting](#troubleshooting)):

- **Access ID / Client ID**
- **Access Secret / Client Secret**

### 1.4 Configure tuya_cloud.json

```bash
cp config/tuya_cloud.example.json config/tuya_cloud.json
```

```json
{
  "api_key": "<Access ID>",
  "api_secret": "<Access Secret>",
  "api_region": "eu",
  "sample_device_id": "<any device ID registered in your app>"
}
```

`api_region` must match the Data Center chosen in step 1.1:

| Data Center                    | region code |
|---------------------------------|-------------|
| China Data Center               | `cn`        |
| US - Western America            | `us`        |
| US - Eastern America            | `us-e`      |
| Central Europe Data Center      | `eu`        |
| Western Europe Data Center      | `eu-w`      |
| India Data Center               | `in`        |
| Singapore Data Center           | `sg`        |

This file is only used by the tinytuya wizard below, never read by the
running app - `config/tuya_cloud.json` (like everything under `config/`
besides the `*.example.*` files) is gitignored.

### 1.5 Run the tinytuya wizard to fetch local_keys

```bash
cd config
python -m tinytuya wizard
```

Enter the Access ID, Access Secret, a sample Device ID, and the region
when prompted, pointing it at `tuya_cloud.json` if asked for a
credentials file. This downloads all your devices (with their
`local_key`) into `config/devices.json` - the file both Tuya drivers
read.

## 2. Find device IPs on your network

```bash
cd config
python -m tinytuya scan
```

Saves `config/snapshot.json` with device IDs and IPs. If a
`config/devices.json` entry is missing its `ip` field (both Tuya drivers
skip devices without one), merge it in from `snapshot.json` by matching
on the device `id`.

## 3. Enable the drivers

```yaml
integrations:
  - kind: cover
    type: tuya_curtain_switch
    devices_file: "devices.json"
    # categories: ["clkg", "tdq"]   # override if your curtain switch reports a different category

  - kind: climate
    type: tuya_air_quality
    devices_file: "devices.json"
```

Both drivers load *every* matching entry from `devices_file` as a
separate cover/sensor named after its `name` field in that file - you
don't list devices individually in `config.yaml`.

## 4. Command-line control (optional)

```bash
localhome-cli list
localhome-cli status "Living Room Shutter"
localhome-cli open "Living Room Shutter"
localhome-cli close all
```

## Troubleshooting

**Error 28841107 - "No permission. The data center is suspended."**
Shown for almost *every* region except the correct one, so trying other
region codes rarely fixes it. The real cause is usually one of:
- The Cloud Project's Data Center was left unconfirmed/unset. Open the
  project's **Overview** tab - it should show a concrete "Data Center"
  value, not a blank/pending state. If the **Devices** tab shows a popup
  saying the data center hasn't been selected, go to the project details
  and explicitly select/save it again.
- `api_region` in `tuya_cloud.json` doesn't match the project's actual
  Data Center (see the table above).

**Error 1106 - "permission deny"**
The app account hasn't been linked yet (step 1.2), or the specific device
wasn't included in that linked account. Complete the QR-code app-account
linking, then retry.

**`sign invalid`**
The Access ID / Access Secret pair is wrong - usually a copy-paste
mistake (swapped ID/Secret, or a stray character from manual retyping).
Re-copy both using the copy icons on the Overview page rather than
selecting text by hand.

**Cross-region access errors when testing `cn`/other regions**
Expected if you don't actually have a project there - it confirms your
network/IP is being correctly rejected by an unrelated data center, a
useful sanity check but not the fix.

**A cover/sensor doesn't show up on the dashboard**
Check it has an `ip` field in `devices.json` (see step 2) and that its
`category` matches what the driver expects (`clkg`/`tdq` for covers,
`hjjcy` for the air-quality sensor - override with `categories:` in
config.yaml if your device reports something else).
