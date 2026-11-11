"""Number entities for Tapo RV30."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CLEANING_INTERVAL,
    DEFAULT_CLEANING_INTERVAL,
    DOMAIN,
    EXCLUDE_FEATURE_IDS,
)
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
    entities: list[NumberEntity] = [
        TapoPositionIntervalNumber(coordinator, entry),
    ]
    for desc in coordinator.feature_descriptors:
        if desc["type"] == "Number" and desc["id"] not in EXCLUDE_FEATURE_IDS:
            entities.append(TapoFeatureNumber(coordinator, entry, desc))

    async_add_entities(entities)


class TapoPositionIntervalNumber(CoordinatorEntity[TapoCoordinator], NumberEntity):
    """Configures the vacuum position update frequency while cleaning."""

    _attr_has_entity_name = True
    _attr_name = "Vacuum Position Update Interval"
    _attr_icon = "mdi:map-marker-path"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = 2
    _attr_native_max_value = 120
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: TapoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_vacuum_position_update_interval"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name":        self.coordinator.device_name,
            "manufacturer":"TP-Link",
            "model":       self.coordinator.device_model,
        }

    @property
    def native_value(self) -> float:
        return float(self.coordinator.cleaning_interval)

    async def async_set_native_value(self, value: float) -> None:
        interval = int(round(value))
        self.coordinator.cleaning_interval = interval

        # If vacuum is actively cleaning or moving, update active polling interval immediately
        is_active = (
            self.coordinator.data is not None
            and self.coordinator.data.get("status_code") in (1, 2, 4)
        )
        if is_active:
            self.coordinator.update_interval = timedelta(seconds=interval)

        # Persist across restarts into config entry options
        new_options = {**self._entry.options, CONF_CLEANING_INTERVAL: interval}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        self.async_write_ha_state()


class TapoFeatureNumber(CoordinatorEntity[TapoCoordinator], NumberEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, desc: dict) -> None:
        super().__init__(coordinator)
        self._fid            = desc["id"]
        self._attr_name      = desc["name"]
        self._attr_unique_id = f"{entry.entry_id}_feat_{desc['id']}"
        if desc.get("icon"):
            self._attr_icon = desc["icon"]
        if desc.get("unit"):
            self._attr_native_unit_of_measurement = desc["unit"]
        self._attr_native_min_value = desc.get("min") if desc.get("min") is not None else 0
        self._attr_native_max_value = desc.get("max") if desc.get("max") is not None else 100
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
    def native_value(self) -> float | None:
        return self.coordinator.features.get(self._fid)

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.client.set_feature_value, self._fid, int(round(value))
        )
        await self.coordinator.async_request_refresh()
