# Changelog

All notable changes to this project are documented here.

## [0.7.14]

### Fixed
- Hassfest: removed `requests` and `cryptography` from the manifest requirements
  (both are provided by Home Assistant itself).
- Hassfest: translation texts no longer contain `<...>` (read as HTML).

## [0.7.13]

### Fixed
- Hassfest validation: manifest keys sorted (`domain`, `name`, then alphabetical)
  and `CONFIG_SCHEMA` declared (`cv.config_entry_only_config_schema`).
- Added the `unknown` abort message used by the reconfiguration flow.

## [0.7.12]

### Changed
- The two vendor client keys (login signature key and socket login key) are no
  longer embedded in the integration. They are now entered in the setup form and
  stored in the config entry.
- Existing installations are asked for the keys once (reauth flow) after the update.

### Added
- `tools/extract_keys.py`: extracts both keys from the official app package
  (`.xapk`, `.apk` or `classes.dex`). Pure standard library.
- Keys can be changed later in *Configure*.

## [0.7.11]

### Changed
- Public release cleanup: removed host-specific paths, example device identifiers
  and personal references from documentation, examples and metadata.
  No functional change.

## [0.7.10]

### Added
- Service `aiwit.archive_event_clip` accepts optional **`require_mount`** again.
  When set (e.g. `/media/your_storage`), the service waits until that path is a
  directory before downloading/converting the clip (same `retries` /
  `retry_delay` as cloud list). Prevents archive failures when external media
  is still mounting after reboot.
- Documented in `services.yaml` for the UI service call form.

## [0.7.9]

### Added
- **Configurable thumbnail directory** (`thumbnail_path` in integration options).
  - Empty (default): keep previous behaviour — write under `<config>/www/aiwit`
    and expose `image_url` as `/local/aiwit/...`.
  - Absolute path (e.g. `/media/your_storage/aiwit/thumbnails`): write there;
    `image_path` in `aiwit_thumbnail_ready` is the absolute file path (usable
    with allowlisted notification services). `image_url` is then omitted.
- Options UI documents both credentials and the thumbnail path.

### Changed
- Options flow no longer re-validates cloud login when only the thumbnail
  path changed.

## [0.7.8]

### Added
- **Options flow**: username and password can now be changed after installation
  (Configure → integration options) without removing the config entry.
  Credentials remain stored in `entry.data` (same location as initial setup).
  The client UDID is preserved. Changing credentials triggers a reload of the
  integration so the live client picks up the new login.
- Password field is optional: leave it empty to keep the current password
  (no need to re-type if nothing changed). If neither login nor password
  changed, the form closes without reload.

## [0.7.7]

### Added
- Event `aiwit_thumbnail_ready` fired once the event's thumbnail is really available
  (`device_udid`, `event_type`, `timestamp`, `clip_name`, `image_path`, `image_url`).
  Thumbnails are also written to `<config>/www/aiwit/` (`*_last.jpg` + one file per clip).
- `switch.*_human_detection` (property `nn_label`, on/off).
- Service `aiwit.archive_event_clip`: new `retry_delay` field.
- A `<clip>.jpg` thumbnail is saved next to each archived `<clip>.mp4` (best effort).

### Changed
- `select.*_humanoid_detection` now has **3 levels** (Faible / Standard / Haut). The app's
  `nn_sens` is a 15..50 slider (not 0/1/5/10): <27 low accuracy, 27-38 standard, >=39 high.
  Written values: 20 / 33 / 45. "Désactivé" removed (use the new switch).
- Archive defaults: `retries` 3 -> 8, `retry_delay` 5 s -> 15 s (the cloud often publishes the
  clip 30-90 s after the event). Thumbnail retries 8 -> 16.
- Cloud REST calls re-login once when the session is rejected; non-zero `resultCode` is logged.
- Property writes check `resultCode` and update the entity immediately (optimistic).
- `aiwit.archive_event_clip` waits with `asyncio.sleep`: no executor thread is pinned while the cloud publishes the clip.
- Coalesced socket notifications no longer trigger extra thumbnail polling; `aiwit_thumbnail_ready` is fired once per (clip, event type).
- One lock per target clip: concurrent PIR events pointing at the same clip no longer race.

## [0.7.2]

- Sélection des clips/vignettes basée sur le timestamp de l’événement, pas uniquement sur le dernier clip cloud visible.
- Recherche avec fenêtre temporelle et nouvelles tentatives lorsque le cloud Aiwit publie le clip en retard.
- Contrôle secondaire des éventuels timestamps fournis dans les métadonnées cloud.
- La sélection commune est réutilisée par la vignette et l’archivage des clips.

## [0.7.1] - 2026-09-29

### Changed
- Clip archiving now matches a cloud `.ts2` to the timestamp of the triggering Aiwit event instead of blindly selecting the latest clip.
- RING and PIR events are independent: a motion event followed by a doorbell press can archive two distinct clips.
- Removed the per-device archive lock; duplicate socket notifications are still coalesced by event type, while distinct RING/PIR events are preserved.
- The archive directory is derived from the clip filename (`YYYYMMDDHHMMSS.ts2`), so delayed processing still stores the MP4 under the clip's actual `YYYYMMDD` directory.
- Removed the `aiwit.purge_clips` service. Retention and purge policy are intentionally left to Home Assistant automations.

### Fixed
- Repeated processing of the same clip is avoided when its target MP4 already exists.
- No host-specific storage path or retention period is embedded in the integration.

### Documentation
- Updated README and service documentation for the separate archive and purge automations.
- Bumped integration version to `0.7.1`.

## [0.7.0] - 2026-09-29

### Added
- Initial automatic cloud clip archiving through the `aiwit.archive_event_clip` Home Assistant service.
- `aiwit_event` Home Assistant events for RING and PIR notifications.
- Bundled `aiwit_ts2_decode.py` support for `.ts2` to MP4 conversion.

## [0.6.0]

Previous release.
