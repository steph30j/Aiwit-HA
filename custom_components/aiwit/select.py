"""Select entities — experimental PIR sensitivity control."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NN_SENS_LEVELS, nn_sens_label
from .coordinator import AiwitCoordinator

_LOGGER = logging.getLogger(__name__)

# Calibré empiriquement (2026-09-16) : l'app n'expose que 3 niveaux
# (Faible/Standard/Haut), avec un mapping INVERSÉ par rapport à l'intuition
# -- valeur haute = sensibilité FAIBLE.
PIR_OPTIONS = {
    "Faible": 10,
    "Standard": 5,
    "Haut": 1,
}
PIR_REVERSE = {v: k for k, v in PIR_OPTIONS.items()}

# Détection humaine : nn_sens est un curseur 15..50 dans l'app (défaut 25),
# découpé en 3 zones d'affichage (cf. const.py). On expose les 3 zones et on
# écrit une valeur représentative de chaque zone. L'ancien mapping 0/10/5/1
# (hérité du PIR) était faux : ces valeurs sont hors plage du curseur.
NN_SENS_OPTIONS = dict(NN_SENS_LEVELS)


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
            new.append(AiwitPirSelect(coordinator, entry, udid))
            new.append(AiwitHumanoidSelect(coordinator, entry, udid))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_add_for_devices))
    _add_for_devices()


class AiwitPirSelect(CoordinatorEntity[AiwitCoordinator], SelectEntity):
    """
    PIR sensitivity level (experimental write).

    Read value comes from device-properties. Write uses cmd "save-property"
    (nom de commande confirmé par décompilation, cf. s1/a.java) mais
    l'encodage exact de la valeur reste non calibré pour ce modèle précis
    -- voir PIR_OPTIONS plus haut.
    """

    _attr_has_entity_name = True
    _attr_name = "PIR sensitivity"
    _attr_icon = "mdi:motion-sensor"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(PIR_OPTIONS.keys())
    # Désactivée par défaut : l'écriture (cmd set-properties) ne fonctionne
    # qu'avec un compte "master" (admin) sur le device -- incompatible avec
    # le compte secondaire recommandé pour cette intégration (cf. README).
    # La valeur elle-même reste lisible via le capteur diagnostic dédié
    # (sensor.*_pir_sensitivity), lui activé par défaut.
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator)
        self._udid = udid
        self._attr_unique_id = f"{udid}_pir_sensitivity"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, udid)},
            name=f"Aiwit {udid[-8:]}",
            manufacturer="Aiwit",
            model="Doorbell",
        )

    @property
    def current_option(self) -> str | None:
        dev = self.coordinator.devices.get(self._udid)
        if not dev or dev.pir_level is None:
            return None
        return PIR_REVERSE.get(dev.pir_level, str(dev.pir_level))

    async def async_select_option(self, option: str) -> None:
        level = PIR_OPTIONS.get(option)
        if level is None:
            # allow raw numeric option labels
            try:
                level = int(option)
            except ValueError:
                return
        _LOGGER.warning(
            "Envoi de save-property (PIR=%s) sur %s -- l'encodage exact de "
            "cette valeur n'est pas confirmé pour ce modèle, et la commande "
            "n'a d'effet que si le compte utilisé est 'master' (admin) sur "
            "ce device. Avec un compte secondaire (recommandé), elle est "
            "silencieusement ignorée par le cloud.",
            level,
            self._udid,
        )
        await self.hass.async_add_executor_job(
            self.coordinator.set_pir_level, self._udid, level
        )


class AiwitHumanoidSelect(CoordinatorEntity[AiwitCoordinator], SelectEntity):
    """Précision de la détection humaine (nn_sens) : Faible / Standard / Haut.

    Faible  = plus de clips, batterie minimale ("Low Accuracy" dans l'app)
    Haut    = moins de clips, batterie maximale ("High Accuracy" dans l'app)
    L'activation on/off est portée par ``switch.*_human_detection``.
    """

    _attr_has_entity_name = True
    _attr_name = "Humanoid detection"
    _attr_icon = "mdi:human-greeting-variant"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(NN_SENS_OPTIONS.keys())
    # Les écritures exigent un compte "master" ; lecture OK avec un secondaire.
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: AiwitCoordinator, entry: ConfigEntry, udid: str
    ) -> None:
        super().__init__(coordinator)
        self._udid = udid
        self._attr_unique_id = f"{udid}_humanoid_detection"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, udid)},
            name=f"Aiwit {udid[-8:]}",
            manufacturer="Aiwit",
            model="Doorbell",
        )

    @property
    def current_option(self) -> str | None:
        dev = self.coordinator.devices.get(self._udid)
        if not dev:
            return None
        return nn_sens_label(dev.nn_sens)

    async def async_select_option(self, option: str) -> None:
        level = NN_SENS_OPTIONS.get(option)
        if level is None:
            return
        await self.hass.async_add_executor_job(
            self.coordinator.set_nn_sens, self._udid, level
        )
