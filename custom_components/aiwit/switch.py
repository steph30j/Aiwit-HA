"""Switch entity — motion detection on/off (property confirmed: motion_enable)."""

from __future__ import annotations

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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
            new.append(AiwitMotionEnableSwitch(coordinator, entry, udid))
            new.append(AiwitHumanDetectionSwitch(coordinator, entry, udid))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_add_for_devices))
    _add_for_devices()


class AiwitMotionEnableSwitch(CoordinatorEntity[AiwitCoordinator], SwitchEntity):
    """
    Active/désactive la détection de mouvement (property `motion_enable`,
    nom de clé confirmé par décompilation -- cf. w1/v5.java, p2/e.java).

    ⚠️ Comme le contrôle PIR sensitivity, l'écriture nécessite probablement
    un compte "master" (admin) -- silencieusement ignorée avec le compte
    secondaire recommandé. Désactivée par défaut pour la même raison.
    """

    _attr_has_entity_name = True
    _attr_name = "Motion detection"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator)
        self._udid = udid
        self._attr_unique_id = f"{udid}_motion_enable"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, udid)},
            name=f"Aiwit {udid[-8:]}",
            manufacturer="Aiwit",
            model="Doorbell",
        )

    @property
    def is_on(self) -> bool | None:
        dev = self.coordinator.devices.get(self._udid)
        return dev.motion_enable if dev else None

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.set_motion_enable, self._udid, True
        )

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.set_motion_enable, self._udid, False
        )


class AiwitHumanDetectionSwitch(CoordinatorEntity[AiwitCoordinator], SwitchEntity):
    """Détection humaine on/off (propriété ``nn_label``, 0/1).

    Clé confirmée par désassemblage de l'app (écran « Human Detection ») ;
    l'association précise du libellé reste à valider par un test réel.
    La précision (3 niveaux) est dans ``select.*_humanoid_detection``.
    """

    _attr_has_entity_name = True
    _attr_name = "Human detection"
    _attr_icon = "mdi:account-eye"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = True

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator)
        self._udid = udid
        self._attr_unique_id = f"{udid}_human_detection"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, udid)},
            name=f"Aiwit {udid[-8:]}",
            manufacturer="Aiwit",
            model="Doorbell",
        )

    @property
    def is_on(self) -> bool | None:
        dev = self.coordinator.devices.get(self._udid)
        return dev.nn_label if dev else None

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.set_nn_label, self._udid, True
        )

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.set_nn_label, self._udid, False
        )
