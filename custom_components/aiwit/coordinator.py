"""Data coordinator for Aiwit — bridges threaded client → HA event loop."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import AiwitClient
from .const import (
    DOMAIN,
    EVENT_AIWIT,
    EVENT_THUMBNAIL,
    THUMBNAIL_WWW_DIR,
    MOTION_OFF_DELAY,
    RING_OFF_DELAY,
    THUMBNAIL_FETCH_DELAY,
    THUMBNAIL_RETRIES,
    THUMBNAIL_RETRY_DELAY,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class DeviceData:
    udid: str
    ring: bool = False
    motion: bool = False
    battery: Optional[int] = None
    charging: bool = False
    rssi: Optional[int] = None
    online: bool = False
    ssid: Optional[str] = None
    firmware: Optional[str] = None
    mcu_ver: Optional[str] = None
    pir_level: Optional[int] = None
    motion_enable: Optional[bool] = None
    nn_sens: Optional[int] = None
    nn_label: Optional[bool] = None  # detection humaine on/off
    image_path: Optional[str] = None
    image_clip: Optional[str] = None
    ring_volume: Optional[int] = None
    led_mode: Optional[int] = None
    mac: Optional[str] = None
    last_ring: Optional[datetime] = None
    last_motion: Optional[datetime] = None
    image_bytes: Optional[bytes] = None
    image_updated: Optional[datetime] = None
    extra: dict[str, Any] = field(default_factory=dict)


class AiwitCoordinator(DataUpdateCoordinator[dict[str, DeviceData]]):
    """Holds latest state per device; updated from socket callbacks."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AiwitClient,
        thumbnail_path: str | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # push-driven
        )
        self.client = client
        # Absolute dir for thumbnails, or None/empty = default <config>/www/aiwit
        self.thumbnail_path = (thumbnail_path or "").strip() or None
        self.devices: dict[str, DeviceData] = {}
        self._ring_unsub: dict[str, Any] = {}
        self._motion_unsub: dict[str, Any] = {}
        self._refresh_scheduled = False
        self._last_archive_event: dict[tuple[str, str], datetime] = {}
        self._announced_thumbs: set[tuple[str, str, str]] = set()

        client.on_ring = self._on_ring
        client.on_pir = self._on_pir
        client.on_device_info = self._on_device_info
        client.on_device_properties = self._on_device_properties
        client.on_state = self._on_state
        client.on_connected = self._on_connected
        client.on_disconnected = self._on_disconnected

    def _ensure_device(self, udid: str) -> DeviceData:
        if udid not in self.devices:
            self.devices[udid] = DeviceData(udid=udid)
        return self.devices[udid]

    def _schedule_refresh(self) -> None:
        """
        Planifie un rafraîchissement des entités, avec un debounce de 0.5s.

        IMPORTANT : sans ce debounce, un flot de messages `device-heartbeat`
        rapprochés (observés en rafale de plusieurs par seconde pendant le
        reverse engineering) déclenchait un `async_set_updated_data()` -- et
        donc une écriture d'état sur TOUTES les entités -- à chaque message,
        ce qui pouvait saturer le recorder et le websocket frontend et
        ralentir toute l'instance HA, pas seulement cette intégration.
        """
        if self._refresh_scheduled:
            return
        self._refresh_scheduled = True

        def _do_refresh() -> None:
            self._refresh_scheduled = False
            self.async_set_updated_data(dict(self.devices))

        def _arm() -> None:
            self.hass.loop.call_later(0.5, _do_refresh)

        self.hass.loop.call_soon_threadsafe(_arm)

    def _on_ring(self, udid: str, raw: dict) -> None:
        dev = self._ensure_device(udid)
        dev.ring = True
        dev.last_ring = datetime.now(timezone.utc)
        self._schedule_refresh()
        self._schedule_auto_off(udid, "ring")
        event_time = datetime.now(timezone.utc)
        if self._fire_aiwit_event(udid, "ring", event_time):
            self._schedule_thumbnail_fetch(udid, event_time, "ring")

    def _on_pir(self, udid: str, raw: dict) -> None:
        dev = self._ensure_device(udid)
        dev.motion = True
        dev.last_motion = datetime.now(timezone.utc)
        self._schedule_refresh()
        self._schedule_auto_off(udid, "motion")
        event_time = datetime.now(timezone.utc)
        if self._fire_aiwit_event(udid, "pir", event_time):
            self._schedule_thumbnail_fetch(udid, event_time, "pir")

    def _fire_aiwit_event(
        self, udid: str, event_type: str, event_time: Optional[datetime] = None
    ) -> bool:
        """Publish one HA event per physical socket notification."""
        now = event_time or datetime.now(timezone.utc)
        key = (udid, event_type)
        previous = self._last_archive_event.get(key)
        if previous and (now - previous).total_seconds() < 5:
            return False
        self._last_archive_event[key] = now
        self.hass.loop.call_soon_threadsafe(
            self.hass.bus.async_fire,
            EVENT_AIWIT,
            {
                "device_udid": udid,
                "event_type": event_type,
                "timestamp": now.isoformat(),
            },
        )
        return True

    def _on_device_info(self, udid: str, info: dict) -> None:
        dev = self._ensure_device(udid)
        changed = False
        if info.get("battery_percent") is not None and dev.battery != info["battery_percent"]:
            dev.battery = info["battery_percent"]
            changed = True
        charging = bool(info.get("battery_charging"))
        if dev.charging != charging:
            dev.charging = charging
            changed = True
        if "rssi" in info and dev.rssi != info["rssi"]:
            dev.rssi = info["rssi"]
            changed = True
        if changed:
            self._schedule_refresh()

    def _on_device_properties(self, udid: str, props: dict) -> None:
        dev = self._ensure_device(udid)
        changed = False
        if "rssi" in props and dev.rssi != props["rssi"]:
            dev.rssi = props["rssi"]
            changed = True
        if "ssid" in props and dev.ssid != props["ssid"]:
            dev.ssid = props["ssid"]
            changed = True
        if "firmware_ver" in props and dev.firmware != props["firmware_ver"]:
            dev.firmware = props["firmware_ver"]
            changed = True
        if "mcu_ver" in props and dev.mcu_ver != props["mcu_ver"]:
            dev.mcu_ver = props["mcu_ver"]
            changed = True
        if "PIR" in props:
            try:
                pir_level = int(props["PIR"])
                if dev.pir_level != pir_level:
                    dev.pir_level = pir_level
                    changed = True
            except (TypeError, ValueError):
                pass
        if "motion_enable" in props:
            try:
                motion_enable = bool(int(props["motion_enable"]))
                if dev.motion_enable != motion_enable:
                    dev.motion_enable = motion_enable
                    changed = True
            except (TypeError, ValueError):
                pass
        if "nn_sens" in props:
            try:
                nn_sens = int(props["nn_sens"])
                if dev.nn_sens != nn_sens:
                    dev.nn_sens = nn_sens
                    changed = True
            except (TypeError, ValueError):
                pass
        if "nn_label" in props:
            try:
                nn_label = bool(int(props["nn_label"]))
                if dev.nn_label != nn_label:
                    dev.nn_label = nn_label
                    changed = True
            except (TypeError, ValueError):
                pass
        if "ring_volume" in props:
            try:
                ring_volume = int(props["ring_volume"])
                if dev.ring_volume != ring_volume:
                    dev.ring_volume = ring_volume
                    changed = True
            except (TypeError, ValueError):
                pass
        if "led_mode" in props:
            try:
                led_mode = int(props["led_mode"])
                if dev.led_mode != led_mode:
                    dev.led_mode = led_mode
                    changed = True
            except (TypeError, ValueError):
                pass
        if "mac" in props and dev.mac != props["mac"]:
            dev.mac = props["mac"]
            changed = True
        if changed:
            self._schedule_refresh()

    def refresh_device(self, udid: str) -> None:
        """Wake device + request state (Refresh button)."""
        self.client.wakeup_device(udid)
        self.client.request_devices_state()

    def set_pir_level(self, udid: str, level: int) -> None:
        """Write PIR property via REST (plus fiable que la commande socket)."""
        try:
            self.client.update_properties_rest(udid, {"PIR": level})
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Aiwit: echec set_pir_level(%s): %s", udid, err)
        # Follow-up wakeup so the device eventually reports the new value
        self.client.wakeup_device(udid)

    def set_motion_enable(self, udid: str, enabled: bool) -> None:
        """Write motion_enable property via REST."""
        try:
            self.client.update_properties_rest(udid, {"motion_enable": int(enabled)})
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Aiwit: echec set_motion_enable(%s): %s", udid, err)
        self.client.wakeup_device(udid)

    def set_nn_sens(self, udid: str, level: int) -> None:
        """Write nn_sens (precision humanoide, curseur 15..50) via REST.

        L'app envoie nn_sens ET nn_label dans le meme appel : on fait pareil
        avec la derniere valeur connue de nn_label.
        """
        dev = self._ensure_device(udid)
        props: dict[str, Any] = {"nn_sens": level}
        if dev.nn_label is not None:
            props["nn_label"] = int(dev.nn_label)
        try:
            self.client.update_properties_rest(udid, props)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Aiwit: echec set_nn_sens(%s): %s", udid, err)
            return
        dev.nn_sens = level  # optimiste : le device ne repousse la valeur qu'au prochain reveil
        self._schedule_refresh()
        self.client.wakeup_device(udid)

    def set_nn_label(self, udid: str, enabled: bool) -> None:
        """Write nn_label (detection humaine on/off) via REST."""
        dev = self._ensure_device(udid)
        props: dict[str, Any] = {"nn_label": int(enabled)}
        if dev.nn_sens is not None:
            props["nn_sens"] = dev.nn_sens
        try:
            self.client.update_properties_rest(udid, props)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Aiwit: echec set_nn_label(%s): %s", udid, err)
            return
        dev.nn_label = enabled
        self._schedule_refresh()
        self.client.wakeup_device(udid)

    def _on_state(self, udid: str, state: dict) -> None:
        dev = self._ensure_device(udid)
        # "device-heartbeat" arrive très fréquemment (observé en rafale de
        # plusieurs par seconde) et ne fait quasiment jamais que confirmer
        # que le device est déjà en ligne -- on ne déclenche un
        # rafraîchissement que lors d'un vrai changement (offline->online),
        # sinon chaque heartbeat forcerait une écriture d'état HA inutile.
        was_online = dev.online
        if "state" in state:
            dev.online = True
        if dev.online != was_online:
            self._schedule_refresh()

    def _on_connected(self) -> None:
        for udid in self.client.known_devices:
            self._ensure_device(udid).online = True
        self._schedule_refresh()

    def _on_disconnected(self) -> None:
        for dev in self.devices.values():
            dev.online = False
        self._schedule_refresh()

    def _schedule_auto_off(self, udid: str, kind: str) -> None:
        """Turn ring/motion back to OFF after EVENT_OFF_DELAY seconds."""

        def _off() -> None:
            dev = self.devices.get(udid)
            if not dev:
                return
            if kind == "ring":
                dev.ring = False
            else:
                dev.motion = False
            self.async_set_updated_data(dict(self.devices))

        key = f"{udid}:{kind}"
        handle = self._ring_unsub if kind == "ring" else self._motion_unsub

        # Tout (annulation de l'ancien timer + planification du nouveau) doit
        # s'exécuter sur la boucle asyncio de HA : on est appelé depuis le
        # thread réseau du client, et TimerHandle.cancel() n'est pas garanti
        # thread-safe si appelé depuis un autre thread.
        def schedule() -> None:
            old = handle.get(key)
            if old:
                old.cancel()
            delay = RING_OFF_DELAY if kind == "ring" else MOTION_OFF_DELAY
            handle[key] = self.hass.loop.call_later(delay, _off)

        self.hass.loop.call_soon_threadsafe(schedule)

    def _save_thumbnail_file(
        self, udid: str, clip_name: str, img: bytes
    ) -> tuple[str, str | None]:
        """Write the thumbnail to the configured directory (executor thread).

        Default: <config>/www/aiwit → image_url = /local/aiwit/...
        Custom absolute path (e.g. /media/.../thumbnails): image_url is None
        (use image_path with allowlist for notifications).

        Returns (absolute_path, image_url_or_None). Two files: a stable
        "last" file and one named after the clip.
        """
        if self.thumbnail_path:
            out_dir = Path(self.thumbnail_path)
        else:
            out_dir = Path(self.hass.config.path("www", THUMBNAIL_WWW_DIR))
        out_dir.mkdir(parents=True, exist_ok=True)
        short = re.sub(r"[^A-Za-z0-9_-]+", "_", udid[-12:])
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(clip_name).stem) or "clip"
        clip_file = out_dir / f"{short}_{stem}.jpg"
        last_file = out_dir / f"{short}_last.jpg"
        for target in (clip_file, last_file):
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(img)
            tmp.replace(target)  # atomique : pas de lecture d'un fichier a moitie ecrit
        abs_path = str(last_file)
        # /local/ URL only when writing under the HA www folder
        if self.thumbnail_path:
            url: str | None = None
        else:
            url = f"/local/{THUMBNAIL_WWW_DIR}/{last_file.name}"
        return abs_path, url

    def _schedule_thumbnail_fetch(
        self, udid: str, event_time: datetime, event_type: str = "unknown"
    ) -> None:
        """Fetch the thumbnail matching this event, retrying while cloud lags.

        Quand la vignette est prete : image entity mise a jour, fichier ecrit
        dans le repertoire configure (defaut www/aiwit) et evenement ``aiwit_thumbnail_ready`` publie. Les
        automations doivent declencher sur CET evenement (et non sur
        ``aiwit_event`` / le binary_sensor, qui arrivent ~10-60 s plus tot).
        """

        async def _delayed() -> None:
            await asyncio.sleep(THUMBNAIL_FETCH_DELAY)
            for attempt in range(THUMBNAIL_RETRIES):
                try:
                    res = await self.hass.async_add_executor_job(
                        self.client.fetch_event_thumbnail_ex, udid, event_time
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Aiwit: vignette erreur: %s", err)
                    res = None
                if res:
                    img, clip_name = res
                    dev = self._ensure_device(udid)
                    dev.image_bytes = img
                    dev.image_updated = datetime.now(timezone.utc)
                    dev.image_clip = clip_name
                    path = url = None
                    try:
                        path, url = await self.hass.async_add_executor_job(
                            self._save_thumbnail_file, udid, clip_name, img
                        )
                        dev.image_path = path
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning("Aiwit: ecriture vignette impossible: %s", err)
                    self._schedule_refresh()
                    announce_key = (udid, clip_name, event_type)
                    if announce_key in self._announced_thumbs:
                        return  # meme clip deja annonce pour ce type d'event
                    self._announced_thumbs.add(announce_key)
                    if len(self._announced_thumbs) > 200:
                        self._announced_thumbs.clear()
                    self.hass.bus.async_fire(
                        EVENT_THUMBNAIL,
                        {
                            "device_udid": udid,
                            "event_type": event_type,
                            "timestamp": event_time.isoformat(),
                            "clip_name": clip_name,
                            "image_path": path,
                            "image_url": url,
                        },
                    )
                    _LOGGER.debug(
                        "Aiwit: vignette %s (event %s %s) prete, tentative %d/%d",
                        clip_name,
                        event_type,
                        event_time.isoformat(),
                        attempt + 1,
                        THUMBNAIL_RETRIES,
                    )
                    return

                if attempt + 1 < THUMBNAIL_RETRIES:
                    await asyncio.sleep(THUMBNAIL_RETRY_DELAY)

            _LOGGER.warning(
                "Aiwit: aucune vignette pour l'event %s/%s (%s) apres %d tentatives",
                udid,
                event_type,
                event_time.isoformat(),
                THUMBNAIL_RETRIES,
            )

        def _arm() -> None:
            self.hass.async_create_task(_delayed())

        self.hass.loop.call_soon_threadsafe(_arm)

    async def _async_fetch_thumbnail(self, udid: str) -> None:
        """Manual thumbnail request without an event timestamp."""
        img = await self.hass.async_add_executor_job(
            self.client.fetch_latest_thumbnail, udid
        )
        if img:
            dev = self._ensure_device(udid)
            dev.image_bytes = img
            dev.image_updated = datetime.now(timezone.utc)
            self._schedule_refresh()
        else:
            _LOGGER.debug("Aiwit: pas de vignette recuperee pour %s", udid)

    async def async_request_thumbnail(self, udid: str) -> None:
        """Déclenchement manuel (ex: service ou bouton) sans timestamp d'event."""
        await self._async_fetch_thumbnail(udid)

    async def async_start(self) -> None:
        """Start background client thread."""
        await self.hass.async_add_executor_job(self.client.start_background)

    async def async_stop(self) -> None:
        await self.hass.async_add_executor_job(self.client.stop)

    async def _async_update_data(self) -> dict[str, DeviceData]:
        return dict(self.devices)
