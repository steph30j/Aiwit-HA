# Aiwit / EKEN Doorbell — Home Assistant integration

<p align="center"><img src="https://raw.githubusercontent.com/steph30j/Aiwit-HA/main/custom_components/aiwit/brand/icon.png" width="128" alt="Aiwit / EKEN Doorbell"></p>

**Current version: 0.7.14**

Unofficial integration for **Aiwit / EKEN** video doorbells (API `api.v2.gdxp.com`).

Inspired by [cloudedge-ha](https://github.com/fradaloisio/cloudedge-ha).

> **Personal use only** with your own account and device. Protocol is not publicly documented by the vendor.

## Features

| Entity | Type | Description |
|--------|------|-------------|
| Ring | `binary_sensor` | Doorbell button (`wakeup_type=RING`) |
| Motion | `binary_sensor` | PIR motion (`wakeup_type=PIR`) |
| Charging | `binary_sensor` | Battery charging (USB plugged in) |
| Battery | `sensor` | Battery % (+ charging attribute, redundant with above) |
| Wi‑Fi signal | `sensor` | RSSI (dBm) |
| Connection | `sensor` | online / offline |
| SSID | `sensor` | Current Wi‑Fi network name |
| PIR level | `sensor` | Raw PIR property (diagnostic, read-only, calibrated: 10=Low/5=Standard/1=High) |
| Humanoid detection level | `sensor` | Raw nn_sens property (diagnostic, read-only) |
| Ring volume | `sensor` | Device ring volume % |
| Firmware | `sensor` | Firmware version (+ MCU / MAC attrs) |
| Refresh | `button` | Wake device + pull status (like CloudEdge) |
| Test image fetch | `button` | Manually trigger a thumbnail fetch (diagnostic, no delay) |
| Last event image | `image` | Real thumbnail after a ring/PIR event (~10s delay), signed OSS fetch |
| PIR sensitivity | `select` | Write confirmed via REST `app_update_property` (or socket `save-property`), **disabled by default** — see below |
| Humanoid detection | `select` | `nn_sens` property, 4 options incl. Off, **unconfirmed mapping**, **disabled by default** — see below |
| Motion detection | `switch` | `motion_enable` property, same master-account caveat, **disabled by default** |
| Human detection | `switch` | `nn_label` property (on/off). Writes need a master account |

Events come from the **realtime TCP socket**. Ring stays `on` ~5s, Motion ~30s, then auto-resets.

### Detection types

The cloud only reports `wakeup_type` = `RING` | `PIR` | `WIFI`.  
There is **no separate "person detected" event** on the socket we reversed.  
Person AI, if any, stays in the paid cloud path of the official app.

### PIR sensitivity

- **Read**: works via `device-properties.PIR` (always-on diagnostic sensor).
- **Calibrated (2026-09-16)**: `10` = Low, `5` = Standard, `1` = High
  (empirically confirmed — inverted from what you'd intuitively expect).
- **Write**: two paths confirmed by decompilation — a REST endpoint
  `app_update_property` (`session_id` + `device_sn` + property fields,
  preferred, used by `set_pir_level`) and a socket cmd `save-property`
  (`{"cmd":"save-property","peer":<device_udid>,"properties":{...}}`,
  kept in `client.py` as a fallback, not currently used). Both still
  require a **master/admin account** on the device — with the recommended
  secondary account, the cloud silently ignores the write, which is why
  the `select` control is **disabled by default**.
- Even with a master account, changes made from elsewhere (the official
  app) aren't pushed to us proactively — the value only refreshes the next
  time the device wakes up (e.g. via the Refresh button).

### Motion detection on/off

`switch.*_motion_detection` maps to the `motion_enable` property (key name
confirmed by decompilation). Same master-account limitation as PIR
sensitivity — **disabled by default**.

### Humanoid detection (switch + select, 3 levels)

Confirmed by disassembling the app (2.6.2): the *Human Detection* screen writes
`nn_label` (0/1 switch) and `nn_sens` (slider **15..50**, app default 25) in one
`app_update_property` call. The app shows three zones:

| `nn_sens` | Select option | App text |
|-----------|---------------|----------|
| < 27 | Faible (writes 20) | Low accuracy: more clips, shortest battery life |
| 27 - 38 | Standard (writes 33) | Standard accuracy |
| >= 39 | Haut (writes 45) | High accuracy: fewer clips, longest battery life |

`switch.*_human_detection` = `nn_label`. Releases <= 0.7.6 wrote 0/1/5/10 which is outside
the slider range: if the select shows *unknown*, pick a level once to restore a valid value.

### Not yet mapped: motion/ring alert toggles

The official app also exposes "Motion Alert" and "Ring Alert" toggles
(distinct from motion/humanoid detection itself). We could **not** find
matching property keys for these in the decompiled code — they may be
per-account notification preferences rather than device properties at all.

If you want to help nail these down: unlike the video P2P channel, this
looks like a plain REST call (`app_update_property` over HTTPS) — so a
simple **mitmproxy** capture while toggling these settings in the official
app (no Wireshark/UDP capture needed) would very likely reveal the missing
property names directly in the request body — this is exactly how PIR and
`nn_sens` were confirmed.

### Device brand icon

Ships its own icon under `custom_components/aiwit/brand/` (HA 2026.3+
local brand images feature — no external submission needed). Sourced from
the official app's own `icon.png` inside the APK/XAPK package.

### Last event image

After a ring/PIR event, we wait ~10s (time for the cloud to finish
uploading the clip) then fetch its thumbnail from Aliyun OSS with a proper
signed request:
1. `oss_get_files_v8` (date format `yyyyMMdd`, confirmed by mitmproxy
   capture) → latest clip's `fileName`/`bucket`/`endpoint`
2. `sts/token/{session_id}/{endpoint}/9` → temporary `AccessKeyId` /
   `AccessKeySecret` / `SecurityToken`
3. `GET https://{bucket}.{endpoint}/{clip}.jpg` signed with the classic
   Aliyun OSS v1 HMAC-SHA1 scheme (`Authorization: OSS {key}:{sig}` +
   `x-oss-security-token` + `Date` headers) — algorithm confirmed byte-for-
   byte against a real request captured from the official app.

Requires clips to actually exist server-side for that day (cloud storage
subscription and/or a trial must be active on the account — an empty
`items`/`item_count: 0` response means nothing to fetch, not a bug).

**Not yet available:** live video stream (P2P channel is natively
encrypted inside `libVCTP2P.so` — see project history for the reverse
engineering dead-end reached there), full cloud clip playback.

## Thumbnails in automations

`aiwit_event` / the ring and motion sensors fire immediately, **before** the cloud has the clip.
Trigger notifications on `aiwit_thumbnail_ready` instead; it carries `image_path`
(absolute filesystem path) and optionally `image_url` (`/local/...` only when
using the default www folder).

**Default** (empty option): files go to `<config>/www/aiwit/` →
`image_path` like `/config/www/aiwit/<id>_last.jpg`, `image_url` = `/local/aiwit/<id>_last.jpg`.

**Custom path** (Configure → options → *Thumbnail directory*): set e.g.
`/media/your_storage/aiwit/thumbnails` (must be in your HA allowlist for
`notify` / `telegram` / etc.). Then `image_path` points there and `image_url`
is not set.

```yaml
triggers:
  - trigger: event
    event_type: aiwit_thumbnail_ready
actions:
  - action: telegram_bot.send_photo
    data:
      file: "{{ trigger.event.data.image_path }}"
      caption: "Aiwit {{ trigger.event.data.event_type }}"
```

## Cloud clip archiving

Version 0.7.1 adds automatic archival of cloud clips while keeping storage and retention policy outside the integration. The integration emits a Home Assistant event named `aiwit_event` whenever a RING or PIR event is received. The event data contains:

- `device_udid`: Aiwit device UDID
- `event_type`: `ring` or `pir`
- `timestamp`: ISO-8601 timestamp of the socket notification

The `aiwit.archive_event_clip` service receives that event timestamp, lists the corresponding cloud clip category, and selects the `.ts2` whose filename timestamp (`YYYYMMDDHHMMSS.ts2`) is closest to the event. It then downloads the object using its per-clip `bucket`, `endpoint` and `pk`, and invokes the bundled `aiwit_ts2_decode.py` decoder to create an MP4.

The service requires an `output_root`, but the integration does not impose any host-specific path. The MP4 is written as:

```text
<output_root>/YYYYMMDD/YYYYMMDDHHMMSS.mp4
```

For example, an automation can choose `/media/your_storage/aiwit`, producing:

```text
/media/your_storage/aiwit/20250115/20250115112018.mp4
```

RING and PIR events are handled independently. If a PIR event and a doorbell press produce two clips, both are archived. Repeated socket notifications of the same event type within the integration's short duplicate-notification window are coalesced.

### Recommended automation: archive every RING/PIR event

```yaml
alias: Aiwit - Archive event clip
description: Archive the Aiwit cloud video generated by each RING/PIR event
mode: parallel
max: 10
triggers:
  - trigger: event
    event_type: aiwit_event
actions:
  - action: aiwit.archive_event_clip
    data:
      device_udid: "{{ trigger.event.data.device_udid }}"
      event_type: "{{ trigger.event.data.event_type }}"
      event_timestamp: "{{ trigger.event.data.timestamp }}"
      output_root: /media/your_storage/aiwit
      delay: 20
      retries: 8
      retry_delay: 15
```

The `output_root` value above is only an **automation choice**; it is not hard-coded in the integration. Change it to any absolute path exposed to Home Assistant.

### Recommended automation: purge old archives

Retention is deliberately **not implemented by the Aiwit integration**. Keep it in a separate Home Assistant automation. One portable approach is a Home Assistant `shell_command` plus a daily automation. Configure the command with the archive path that applies to your installation; the integration itself remains unaware of that path.

With a retention of 7 days, keep the folder dated exactly today minus 7 days and remove only folders strictly older than that date.

### Requirements for MP4 conversion

The integration bundles the supplied `aiwit_ts2_decode.py` decoder and relies on the `cryptography` package already provided by Home Assistant. The Home Assistant host/container must also provide the `ffmpeg` executable because the decoder uses it to remux the H.264/AAC streams into MP4.

See `CHANGELOG.md` for the complete release history.

## Client keys (required)

The Aiwit/EKEN cloud expects two fixed client keys that are embedded in the
official app. They belong to the vendor, so **this repository does not ship
them**: you extract them once from the app package you downloaded yourself,
then enter them in the setup form.

1. Get the official Android package of the Aiwit app (`.xapk` or `.apk`) from a
   source you trust. Version 2.6.2 is the one this integration was built against.
2. Run the helper (Python 3.8+, no extra dependency, nothing is sent over the network):

   ```bash
   python3 tools/extract_keys.py Aiwit_2_6_2.xapk
   ```

   Example of output:

   ```text
   Login signature key : <value shown here>
   Socket login key    : <value shown here>
   ```

   Use `--json` for machine-readable output. If several socket-key candidates
   are listed, try them in order. If a key is reported as `NOT FOUND`, the app
   version probably changed its internal layout: open an issue with the app version.
3. Paste both values in the integration setup form (or later in *Configure*).

If you upgrade from a version older than 0.7.12, Home Assistant will ask you
for the two keys once (a "reconfigure required" notification appears on the integration).

Use the app package only for your own device and account, in line with the
vendor's terms.

## Installation

### Manual

1. Copy `custom_components/aiwit` into your HA `config/custom_components/` folder.
2. Restart Home Assistant.
3. Settings → Devices & services → **Add integration** → **Aiwit / EKEN Doorbell**.
4. Enter email + password used in the Aiwit/EKEN app, plus the two **client keys** (see above).

### HACS (custom repository)

1. In HACS → menu ⋮ (top right) → **Custom repositories**.
2. Paste the repository URL (`https://github.com/steph30j/Aiwit-HA`), category **Integration**.
3. Search for "Aiwit" in HACS → Integrations, then **Download**.
4. Restart Home Assistant, then configure the integration (Settings → Devices & services).

## Recommendation: secondary account

Like CloudEdge, the cloud may allow only one active session. Prefer:

1. Create a second Aiwit account  
2. Share the doorbell with it  
3. Use that account in Home Assistant  

## Debug logging

```yaml
logger:
  default: warning
  logs:
    custom_components.aiwit: debug
```

## Automation example

```yaml
automation:
  - alias: Doorbell pressed
    trigger:
      - platform: state
        entity_id: binary_sensor.aiwit_xxxxxxxx_ring
        to: "on"
    action:
      - service: notify.mobile_app_phone
        data:
          title: Sonnette
          message: Quelqu'un sonne à la porte
```

## Credits

Protocol reverse-engineered from `com.eken.doorbell` APK and live socket captures.  
Structure inspired by [fradaloisio/cloudedge-ha](https://github.com/fradaloisio/cloudedge-ha).
