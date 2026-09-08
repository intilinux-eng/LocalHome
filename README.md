# Roller Shutter Control (LoraTap / Tuya)

Control LoraTap WiFi roller shutter modules (Tuya-based, "Curtain switch" category)
over the local network using [tinytuya](https://github.com/jasonacox/tinytuya).

## 1. Install dependencies

```
pip install -r requirements.txt
```

## 2. Get Tuya API credentials

tinytuya talks to your devices directly over the local network, but it needs a
**local_key** per device to decrypt traffic. The only way to get these keys is
through a free Tuya IoT developer account.

### 2.1 Create a Tuya IoT account and Cloud Project

1. Sign up at [https://iot.tuya.com](https://iot.tuya.com) (free, no card required).
2. Go to **Cloud** → **Development** → **Create Cloud Project**.
3. Fill in a name, set Industry to "Smart Home", Development Method to "Custom".
4. **Data Center**: pick the region matching where your app account is registered
   (e.g. "Central Europe Data Center" for most EU accounts). This choice cannot
   be changed later without recreating the project, and getting it wrong causes
   the errors described in the Troubleshooting section below.
5. On the "Select Authorization" screen, accept the default APIs. Also
   subscribe (Free Trial) to **"Smart Home Basic Service"** under
   **Service API** — it's required for the app-account linking step below.

### 2.2 Link your Smart Life / Tuya Smart / OEM app account

This step is required — without it the cloud project cannot see any of your
devices, even if the API credentials are valid.

1. In your Cloud Project, go to the **Devices** tab.
2. Click the **"Link App Account"** sub-tab (not "Link My App", which is for a
   different Tuya product). This shows a QR code meant to be scanned from
   *inside* your existing device-control app (Smart Life, Tuya Smart, or any
   Tuya white-label app such as "SmartHome" — they all work the same way).
3. Open the app on your phone → Profile/Settings → the scan icon → scan the QR
   code shown on the platform.
4. Back on the platform, the **Devices** tab should now list your devices.

### 2.3 Get the Access ID / Access Secret

In the Cloud Project's **Overview** tab, copy (use the copy icon, don't
retype by hand — see Troubleshooting):

- **Access ID / Client ID**
- **Access Secret / Client Secret**

### 2.4 Configure secrets.json

Copy the template and fill in your values:

```
cp secrets.json.example secrets.json
```

```json
{
  "api_key": "<Access ID>",
  "api_secret": "<Access Secret>",
  "api_region": "eu",
  "sample_device_id": "<any device ID registered in your app>"
}
```

`api_region` must match the Data Center chosen in step 2.1:

| Data Center                    | region code |
|---------------------------------|-------------|
| China Data Center               | `cn`        |
| US - Western America            | `us`        |
| US - Eastern America            | `us-e`      |
| Central Europe Data Center      | `eu`        |
| Western Europe Data Center      | `eu-w`      |
| India Data Center               | `in`        |
| Singapore Data Center           | `sg`        |

`secrets.json` is gitignored — it never gets committed.

### 2.5 Run the tinytuya wizard to fetch local_keys

```
python -m tinytuya wizard
```

Enter the Access ID, Access Secret, a sample Device ID, and the region when
prompted. This downloads all your devices (with their local_key) into
`devices.json`.

## 3. Discover devices on the local network (optional)

To find device IPs on the current WiFi network:

```
python -m tinytuya scan
```

This saves `snapshot.json` with device IDs and IPs. If `devices.json` entries
are missing an `ip` field, merge it in from `snapshot.json` by matching on the
device `id`.

## 4. Usage

```
python shutters.py list
python shutters.py status "Living Room Shutter"
python shutters.py open "Living Room Shutter"
python shutters.py close "Living Room Shutter"
python shutters.py stop "Living Room Shutter"
python shutters.py open all
```

## Troubleshooting

**Error 28841107 — "No permission. The data center is suspended."**
This is almost always shown for *every* region except the correct one, so
trying other region codes rarely fixes it. The real cause is usually one of:
- The Cloud Project's Data Center was left unconfirmed/unset. Open the
  project's **Overview** tab — it should show a concrete "Data Center" value
  (e.g. "Central Europe Data Center"), not a blank/pending state. If the
  **Devices** tab shows a popup saying "the current data center has not been
  selected for this project", go to the project details and explicitly
  select/save the Data Center again.
- `api_region` in `secrets.json` doesn't match the project's actual Data
  Center (see the table above).

**Error 1106 — "permission deny"**
The app account hasn't been linked yet (step 2.2), or the specific device
wasn't included in that linked account. Complete the QR-code app-account
linking, then retry.

**`sign invalid`**
The Access ID / Access Secret pair is wrong — usually a copy-paste mistake
(swapped ID/Secret, or a stray character from manual retyping). Re-copy both
using the copy icons on the Overview page rather than selecting text by hand.

**Cross-region access errors when testing `cn`/other regions**
Expected if you don't actually have a project there — it confirms your
network/IP is being correctly rejected by an unrelated data center, which is
a useful sanity check but not the fix.
