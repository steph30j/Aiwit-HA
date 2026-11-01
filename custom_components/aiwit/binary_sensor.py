"""Binary sensors: doorbell ring + PIR motion."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SENSOR_CHARGING, SENSOR_MOTION, SENSOR_RING
from .coordinator import AiwitCoordinator, DeviceData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AiwitCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    # Entities may appear before first device list; create when devices known
    known: set[str] = set()

    @callback
    def _add_for_devices() -> None:
        new = []
        for udid, dev in coordinator.devices.items():
            if udid in known:
                continue
            known.add(udid)
            new.append(AiwitRingSensor(coordinator, entry, udid))
            new.append(AiwitMotionSensor(coordinator, entry, udid))
            new.append(AiwitChargingSensor(coordinator, entry, udid))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_add_for_devices))
    _add_for_devices()


class AiwitBaseBinary(CoordinatorEntity[AiwitCoordinator], BinarySensorEntity):
    """Base binary sensor for one device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AiwitCoordinator,
        entry: ConfigEntry,
        udid: str,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._udid = udid
        self._key = key
        self._attr_unique_id = f"{udid}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, udid)},
            name=f"Aiwit {udid[-8:]}",
            manufacturer="Aiwit",
            model="Doorbell",
        )

    @property
    def _dev(self) -> DeviceData | None:
        return self.coordinator.devices.get(self._udid)


class AiwitRingSensor(AiwitBaseBinary):
    """Doorbell button pressed."""

    _attr_name = "Ring"
    _attr_device_class = BinarySensorDeviceClass.SOUND
    _attr_icon = "mdi:doorbell"

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator, entry, udid, SENSOR_RING)

    @property
    def is_on(self) -> bool:
        dev = self._dev
        return bool(dev and dev.ring)


class AiwitMotionSensor(AiwitBaseBinary):
    """PIR motion."""

    _attr_name = "Motion"
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator, entry, udid, SENSOR_MOTION)

    @property
    def is_on(self) -> bool:
        dev = self._dev
        return bool(dev and dev.motion)


class AiwitChargingSensor(AiwitBaseBinary):
    """Batterie en charge (câble USB branché)."""

    _attr_name = "Charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator, entry, udid, SENSOR_CHARGING)

    @property
    def is_on(self) -> bool:
        dev = self._dev
        return bool(dev and dev.charging)
