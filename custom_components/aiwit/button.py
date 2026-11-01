"""Buttons: Refresh (wakeup device)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EntityCategory
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
            new.append(AiwitRefreshButton(coordinator, entry, udid))
            new.append(AiwitTestImageButton(coordinator, entry, udid))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_add_for_devices))
    _add_for_devices()


class AiwitRefreshButton(CoordinatorEntity[AiwitCoordinator], ButtonEntity):
    """Wake the doorbell so it pushes battery / RSSI / properties."""

    _attr_has_entity_name = True
    _attr_name = "Refresh"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator)
        self._udid = udid
        self._attr_unique_id = f"{udid}_refresh"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, udid)},
            name=f"Aiwit {udid[-8:]}",
            manufacturer="Aiwit",
            model="Doorbell",
        )

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.refresh_device, self._udid
        )


class AiwitTestImageButton(CoordinatorEntity[AiwitCoordinator], ButtonEntity):
    """
    Déclenche immédiatement une tentative de récupération de vignette
    (sans attendre un event ring/PIR réel ni le délai de 18s) -- pratique
    pour diagnostiquer la fonctionnalité image sans devoir physiquement
    déclencher la sonnette à chaque essai. Active les logs debug
    (custom_components.aiwit: debug) pour voir le détail de la tentative.
    """

    _attr_has_entity_name = True
    _attr_name = "Test image fetch"
    _attr_icon = "mdi:image-search"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator)
        self._udid = udid
        self._attr_unique_id = f"{udid}_test_image"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, udid)},
            name=f"Aiwit {udid[-8:]}",
            manufacturer="Aiwit",
            model="Doorbell",
        )

    async def async_press(self) -> None:
        await self.coordinator.async_request_thumbnail(self._udid)
