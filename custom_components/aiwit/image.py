"""Image entity — latest event thumbnail (best-effort, via oss_get_files_v8)."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AiwitCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AiwitCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    known: set[str] = set()

    @callback
    def _add_for_devices() -> None:
        new = []
        for udid in coordinator.devices:
            if udid in known:
                continue
            known.add(udid)
            new.append(AiwitLastEventImage(hass, coordinator, entry, udid))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_add_for_devices))
    _add_for_devices()


class AiwitLastEventImage(CoordinatorEntity[AiwitCoordinator], ImageEntity):
    """
    Dernière vignette connue (déclenchée par un event ring/PIR, avec un
    délai pour laisser le temps à l'upload cloud -- cf. coordinator.py).

    ⚠️ Best-effort : la récupération suppose un accès direct au bucket OSS
    sans signature. Si le bucket s'avère privé, cette entité restera vide
    (pas d'exception, juste pas d'image) -- cf. logs en debug pour
    confirmer. Aucun flux vidéo live pour l'instant (protocole P2P chiffré
    nativement, cf. discussion reverse engineering).
    """

    _attr_has_entity_name = True
    _attr_name = "Last event image"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: AiwitCoordinator,
        entry: ConfigEntry,
        udid: str,
    ) -> None:
        super().__init__(coordinator)
        ImageEntity.__init__(self, hass)
        self._udid = udid
        self._attr_unique_id = f"{udid}_last_event_image"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, udid)},
            name=f"Aiwit {udid[-8:]}",
            manufacturer="Aiwit",
            model="Doorbell",
        )

    @property
    def _dev(self):
        return self.coordinator.devices.get(self._udid)

    @property
    def image_last_updated(self):
        dev = self._dev
        return dev.image_updated if dev else None

    async def async_image(self) -> bytes | None:
        dev = self._dev
        return dev.image_bytes if dev else None
