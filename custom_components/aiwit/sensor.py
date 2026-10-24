"""Sensors: battery, RSSI, online."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SENSOR_BATTERY, SENSOR_ONLINE, SENSOR_RSSI, nn_sens_label
from .coordinator import AiwitCoordinator, DeviceData


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
            new.append(AiwitBatterySensor(coordinator, entry, udid))
            new.append(AiwitRssiSensor(coordinator, entry, udid))
            new.append(AiwitOnlineSensor(coordinator, entry, udid))
            new.append(AiwitSsidSensor(coordinator, entry, udid))
            new.append(AiwitPirLevelSensor(coordinator, entry, udid))
            new.append(AiwitHumanoidLevelSensor(coordinator, entry, udid))
            new.append(AiwitRingVolumeSensor(coordinator, entry, udid))
            new.append(AiwitFirmwareSensor(coordinator, entry, udid))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_add_for_devices))
    _add_for_devices()


class AiwitBaseSensor(CoordinatorEntity[AiwitCoordinator], SensorEntity):
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


class AiwitBatterySensor(AiwitBaseSensor):
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator, entry, udid, SENSOR_BATTERY)

    @property
    def native_value(self) -> int | None:
        dev = self._dev
        return dev.battery if dev else None

    @property
    def extra_state_attributes(self) -> dict:
        dev = self._dev
        if not dev:
            return {}
        return {"charging": dev.charging}


class AiwitRssiSensor(AiwitBaseSensor):
    _attr_name = "Wi‑Fi signal"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    # Chaîne littérale plutôt qu'une constante importée : HA valide ce
    # device_class contre "dB"/"dBm" en tant que valeurs, et les constantes
    # dédiées aux unités de signal ont une histoire mouvementée selon les
    # versions (mieux vaut ne pas en dépendre).
    _attr_native_unit_of_measurement = "dBm"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator, entry, udid, SENSOR_RSSI)

    @property
    def native_value(self) -> int | None:
        dev = self._dev
        return dev.rssi if dev else None


class AiwitOnlineSensor(AiwitBaseSensor):
    _attr_name = "Connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:lan-connect"

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator, entry, udid, SENSOR_ONLINE)

    @property
    def native_value(self) -> str:
        dev = self._dev
        return "online" if (dev and dev.online) else "offline"


class AiwitSsidSensor(AiwitBaseSensor):
    _attr_name = "SSID"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:wifi"

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator, entry, udid, "ssid")

    @property
    def native_value(self) -> str | None:
        dev = self._dev
        return dev.ssid if dev else None


class AiwitPirLevelSensor(AiwitBaseSensor):
    """Raw PIR property value (read-only mirror of the select).

    Calibré empiriquement (2026-09-16) : 10=Faible, 5=Standard, 1=Haut.
    On garde la valeur numérique brute comme state (utile pour
    historique/graphiques), et on ajoute le libellé lisible en attribut.
    """

    _attr_name = "PIR level"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:motion-sensor"
    _attr_state_class = SensorStateClass.MEASUREMENT

    _LABELS = {10: "Faible", 5: "Standard", 1: "Haut"}

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator, entry, udid, "pir_level")

    @property
    def native_value(self) -> int | None:
        dev = self._dev
        return dev.pir_level if dev else None

    @property
    def extra_state_attributes(self) -> dict:
        dev = self._dev
        level = dev.pir_level if dev else None
        return {"label": self._LABELS.get(level, "inconnu")}


class AiwitHumanoidLevelSensor(AiwitBaseSensor):
    """Raw nn_sens property value (read-only mirror of the select).

    Curseur 15..50 ; zones : <27 Faible, 27-38 Standard, >=39 Haut (cf. const.py).
    """

    _attr_name = "Humanoid detection level"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:human-greeting-variant"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator, entry, udid, "nn_sens_level")

    @property
    def native_value(self) -> int | None:
        dev = self._dev
        return dev.nn_sens if dev else None

    @property
    def extra_state_attributes(self) -> dict:
        dev = self._dev
        level = dev.nn_sens if dev else None
        return {
            "label": nn_sens_label(level) or "inconnu",
            "human_detection": dev.nn_label if dev else None,
        }


class AiwitRingVolumeSensor(AiwitBaseSensor):
    _attr_name = "Ring volume"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:volume-high"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator, entry, udid, "ring_volume")

    @property
    def native_value(self) -> int | None:
        dev = self._dev
        return dev.ring_volume if dev else None


class AiwitFirmwareSensor(AiwitBaseSensor):
    _attr_name = "Firmware"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chip"

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator, entry, udid, "firmware")

    @property
    def native_value(self) -> str | None:
        dev = self._dev
        return dev.firmware if dev else None

    @property
    def extra_state_attributes(self) -> dict:
        dev = self._dev
        if not dev:
            return {}
        attrs = {}
        if dev.mcu_ver:
            attrs["mcu_ver"] = dev.mcu_ver
        if dev.mac:
            attrs["mac"] = dev.mac
        if dev.led_mode is not None:
            attrs["led_mode"] = dev.led_mode
        return attrs
