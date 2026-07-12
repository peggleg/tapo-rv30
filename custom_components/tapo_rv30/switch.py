"""Switch entities for Tapo RV30 — generic bridge for any python-kasa
Feature of type Switch (e.g. Carpet boost, Child lock).

AES transport only — see binary_sensor.py for the same note.
"""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EXCLUDE_FEATURE_IDS
from .coordinator import TapoCoordinator

_LOGGER = logging.getLogger(__name__)

_CATEGORY_MAP = {
    "Config": EntityCategory.CONFIG,
    "Debug":  EntityCategory.DIAGNOSTIC,
    "Info":   EntityCategory.DIAGNOSTIC,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TapoCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        TapoFeatureSwitch(coordinator, entry, desc)
        for desc in coordinator.feature_descriptors
        if desc["type"] == "Switch" and desc["id"] not in EXCLUDE_FEATURE_IDS
    ]
    async_add_entities(entities)


class TapoFeatureSwitch(CoordinatorEntity[TapoCoordinator], SwitchEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, desc: dict) -> None:
        super().__init__(coordinator)
        self._fid            = desc["id"]
        self._attr_name      = desc["name"]
        self._attr_unique_id = f"{entry.entry_id}_feat_{desc['id']}"
        if desc.get("icon"):
            self._attr_icon = desc["icon"]
        self._attr_entity_category = _CATEGORY_MAP.get(desc.get("category"))
        self._entry = entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name":        self.coordinator.device_name,
            "manufacturer":"TP-Link",
            "model":       self.coordinator.device_model,
        }

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.features.get(self._fid)

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.client.set_feature_value, self._fid, True
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.client.set_feature_value, self._fid, False
        )
        await self.coordinator.async_request_refresh()
