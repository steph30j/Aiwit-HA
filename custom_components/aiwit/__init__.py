"""Aiwit / EKEN Doorbell integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .client import AiwitClient
from .clip_archive import (
    _download_and_convert,
    _event_datetime,
    _find_event_clip,
)
from .const import (
    CONF_LOGIN_SIGN_KEY,
    CONF_SOCKET_KEY,
    CONF_THUMBNAIL_PATH,
    CONF_UDID,
    DOMAIN,
)
from .coordinator import AiwitCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.IMAGE,
]

SERVICE_ARCHIVE = "archive_event_clip"

ARCHIVE_SCHEMA = vol.Schema(
    {
        vol.Optional("device_udid"): cv.string,
        vol.Required("event_type"): vol.In(["ring", "pir"]),
        vol.Required("event_timestamp"): cv.string,
        vol.Required("output_root"): cv.string,
        vol.Optional("require_mount"): cv.string,
        vol.Optional("delay", default=20): vol.All(vol.Coerce(float), vol.Range(min=0, max=120)),
        vol.Optional("retries", default=8): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
        vol.Optional("retry_delay", default=15): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=120)
        ),
    }
)


def _find_client(hass: HomeAssistant, device_udid: str | None) -> tuple[AiwitClient, str]:
    entries = hass.data.get(DOMAIN, {})
    matches: list[tuple[AiwitClient, str]] = []
    for data in entries.values():
        if not isinstance(data, dict) or "client" not in data:
            continue
        client: AiwitClient = data["client"]
        for known_udid in client.known_devices:
            if device_udid is None or known_udid == device_udid:
                matches.append((client, known_udid))

    if device_udid:
        if not matches:
            raise HomeAssistantError(f"Aiwit device {device_udid} is not known")
        return matches[0]
    if len(matches) != 1:
        raise HomeAssistantError(
            "device_udid is required when the configured Aiwit accounts expose "
            "zero or more than one device"
        )
    return matches[0]


async def _wait_for_mount(
    path: str, retries: int, retry_delay: float
) -> None:
    """Wait until path exists as a directory (USB/media mount ready).

    Accepts a normal directory or a mount point. Used so archive does not
    fail when the external disk is still coming online after a reboot.
    """
    mount = Path(path)
    for attempt in range(max(1, retries)):
        try:
            if mount.is_dir():
                _LOGGER.debug(
                    "Aiwit: require_mount %s available (attempt %d)", path, attempt + 1
                )
                return
        except OSError as err:
            _LOGGER.debug("Aiwit: require_mount check %s: %s", path, err)
        if attempt + 1 < retries:
            _LOGGER.debug(
                "Aiwit: waiting for mount %s (%d/%d)", path, attempt + 1, retries
            )
            await asyncio.sleep(retry_delay)
    raise HomeAssistantError(
        f"Aiwit: mount path {path} not available after {retries} attempts"
    )


async def _async_archive(call: ServiceCall) -> None:
    """Archive one event's clip.

    Les attentes (delay + retry_delay) se font avec asyncio.sleep : aucun
    thread de l'executor HA n'est bloque pendant que le cloud publie le clip
    (avant : 1 a 3 min de thread bloque par evenement).

    Optional require_mount: wait until that path is a directory before writing
    (typical use: /media/your_storage so USB is mounted after reboot).
    """
    hass = call.hass
    client, device_udid = _find_client(hass, call.data.get("device_udid"))
    event_type = call.data["event_type"]
    event_ts = call.data["event_timestamp"]
    retries = call.data["retries"]
    retry_delay = call.data["retry_delay"]
    output_root = Path(call.data["output_root"])
    require_mount = call.data.get("require_mount")

    try:
        if require_mount:
            await _wait_for_mount(require_mount, retries, retry_delay)

        event_dt = _event_datetime(event_ts)
        await asyncio.sleep(call.data["delay"])
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                clip = await hass.async_add_executor_job(
                    _find_event_clip, client, device_udid, event_dt, event_type
                )
                if clip:
                    path = await hass.async_add_executor_job(
                        _download_and_convert, client, clip, output_root, 15
                    )
                    _LOGGER.info("Aiwit: archived %s -> %s", device_udid, path)
                    return
                _LOGGER.debug(
                    "Aiwit: clip %s/%s pas encore publie (essai %d/%d)",
                    device_udid, event_type, attempt + 1, retries,
                )
            except Exception as err:  # noqa: BLE001
                last_error = err
                _LOGGER.debug("Aiwit: archive essai %d/%d: %s", attempt + 1, retries, err)
            if attempt + 1 < retries:
                await asyncio.sleep(retry_delay)
        if last_error:
            raise last_error
        _LOGGER.warning(
            "Aiwit: aucun clip cloud pour l'event %s/%s a %s apres %d essais",
            device_udid, event_type, event_ts, retries,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Aiwit: archive_event_clip failed for %s event=%s at %s",
            device_udid, event_type, event_ts,
        )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up global Aiwit services."""
    hass.services.async_register(
        DOMAIN, SERVICE_ARCHIVE, _async_archive, schema=ARCHIVE_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Aiwit from a config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    udid = entry.data.get(CONF_UDID)
    # Options take precedence; empty = default www/aiwit
    thumbnail_path = entry.options.get(CONF_THUMBNAIL_PATH) or entry.data.get(
        CONF_THUMBNAIL_PATH
    )

    login_sign_key = entry.data.get(CONF_LOGIN_SIGN_KEY, "")
    socket_key = entry.data.get(CONF_SOCKET_KEY, "")
    if not login_sign_key or not socket_key:
        # Entries created before 0.7.12 have no keys: ask for them (reauth flow)
        raise ConfigEntryAuthFailed(
            "Aiwit client keys are missing. Run tools/extract_keys.py on the "
            "official app package and enter the keys."
        )

    client = AiwitClient(
        username,
        password,
        udid=udid,
        login_sign_key=login_sign_key,
        socket_key=socket_key,
    )
    coordinator = AiwitCoordinator(hass, client, thumbnail_path=thumbnail_path)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }

    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator: AiwitCoordinator = data["coordinator"]
        await coordinator.async_stop()
    return unload_ok
