"""Sensor entities for Tapo RV30."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONSUMABLE_LABELS,
    CONSUMABLE_LIMITS_H,
    DOMAIN,
    ERROR_CODES,
    EXCLUDE_FEATURE_IDS,
    VACUUM_STATES,
    WATER_INT_TO_NAME,
    FAN_INT_TO_NAME,
)
from .coordinator import TapoCoordinator, _b64name

_LOGGER = logging.getLogger(__name__)

_FEATURE_CATEGORY_MAP = {
    "Config": EntityCategory.CONFIG,
    "Debug":  EntityCategory.DIAGNOSTIC,
    "Info":   EntityCategory.DIAGNOSTIC,
}


@dataclass(frozen=True)
class TapoSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict], Any] = lambda d: None


_STATUS_SENSOR = TapoSensorDescription(
    key="status",
    name="Status",
    icon="mdi:robot-vacuum",
    value_fn=lambda d: VACUUM_STATES.get(d.get("status_code", 0), "idle").replace("_", " ").title(),
)

_BATTERY_SENSOR = TapoSensorDescription(
    key="battery",
    name="Battery",
    native_unit_of_measurement=PERCENTAGE,
    device_class=SensorDeviceClass.BATTERY,
    state_class=SensorStateClass.MEASUREMENT,
    value_fn=lambda d: d.get("battery"),
)

_ERROR_SENSOR = TapoSensorDescription(
    key="error",
    name="Error",
    icon="mdi:alert-circle",
    value_fn=lambda d: ERROR_CODES.get(
        (d.get("error_codes") or [0])[0], f"Code({(d.get('error_codes') or [0])[0]})"
    ),
)

_AREA_SENSOR = TapoSensorDescription(
    key="clean_area",
    name="Last Clean Area",
    icon="mdi:texture-box",
    native_unit_of_measurement="m²",
    state_class=SensorStateClass.MEASUREMENT,
    value_fn=lambda d: d.get("clean_area"),
)


def _consumable_descriptions() -> list[TapoSensorDescription]:
    descs = []
    icons = {
        "roll_brush_time":     "mdi:brush",
        "edge_brush_time":     "mdi:rotate-right",
        "filter_time":         "mdi:air-filter",
        "sensor_time":         "mdi:eye",
        "charge_contact_time": "mdi:lightning-bolt",
    }
    for key, label in CONSUMABLE_LABELS.items():
        descs.append(TapoSensorDescription(
            key=f"consumable_{key}",
            name=f"{label} Remaining",
            icon=icons.get(key, "mdi:wrench"),
            native_unit_of_measurement=UnitOfTime.HOURS,
            state_class=SensorStateClass.MEASUREMENT,
            # value_fn filled in below via closure
        ))
    return descs


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TapoCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        TapoStatusSensor(coordinator, entry, _STATUS_SENSOR),
        TapoStatusSensor(coordinator, entry, _BATTERY_SENSOR),
        TapoStatusSensor(coordinator, entry, _ERROR_SENSOR),
        TapoStatusSensor(coordinator, entry, _AREA_SENSOR),
        TapoRoomsSensor(coordinator, entry),
        TapoSchedulesSensor(coordinator, entry),
    ]

    for ckey, clabel in CONSUMABLE_LABELS.items():
        entities.append(
            TapoConsumableSensor(coordinator, entry, ckey, clabel)
        )

    for desc in coordinator.feature_descriptors:
        if desc["type"] == "Sensor" and desc["id"] not in EXCLUDE_FEATURE_IDS:
            entities.append(TapoFeatureSensor(coordinator, entry, desc))

    async_add_entities(entities)


class TapoStatusSensor(CoordinatorEntity[TapoCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, desc: TapoSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = desc
        self._attr_unique_id    = f"{entry.entry_id}_{desc.key}"
        self._entry             = entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name":        self.coordinator.device_name,
            "manufacturer":"TP-Link",
            "model":       self.coordinator.device_model,
        }

    @property
    def native_value(self) -> Any:
        d = self.coordinator.data
        if d is None:
            return None
        return self.entity_description.value_fn(d)


class TapoRoomsSensor(CoordinatorEntity[TapoCoordinator], SensorEntity):
    """Lists the rooms found on the current map.

    State is a comma-separated list of room names (what you actually
    want to see at a glance in the entity list); extra_state_attributes
    carries the same data structured for templates/scripts — a plain
    list, and an id→name mapping to help construct clean_rooms calls.
    """
    _attr_has_entity_name = True
    _attr_name             = "Rooms"
    _attr_icon             = "mdi:floor-plan"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_rooms"
        self._entry           = entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name":        self.coordinator.device_name,
            "manufacturer":"TP-Link",
            "model":       self.coordinator.device_model,
        }

    @property
    def native_value(self) -> str:
        rooms = sorted(self.coordinator.rooms, key=lambda r: r.get("id", 0))
        names = [_b64name(r.get("name", "")) for r in rooms]
        state = ", ".join(names)
        # HA logs a warning and truncates display for states over 255 chars —
        # keep well under that even with many/long room names.
        return state[:252] + "…" if len(state) > 255 else state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rooms = sorted(self.coordinator.rooms, key=lambda r: r.get("id", 0))
        names = [_b64name(r.get("name", "")) for r in rooms]
        return {
            "room_names": names,
            "room_count": len(rooms),
            "rooms": {r.get("id"): _b64name(r.get("name", "")) for r in rooms},
            "map_id": self.coordinator.map_id,
        }


class TapoFeatureSensor(CoordinatorEntity[TapoCoordinator], SensorEntity):
    """Generic sensor for any python-kasa Feature of type Sensor that
    isn't already covered by a hand-written entity — this is how new
    diagnostic values (consumable 'used' hours, cleaning progress/time,
    wifi signal, device time, etc) show up automatically without needing
    a bespoke class per field. AES transport only; empty on TPAP.
    """
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, desc: dict) -> None:
        super().__init__(coordinator)
        self._fid              = desc["id"]
        self._attr_name        = desc["name"]
        self._attr_unique_id   = f"{entry.entry_id}_feat_{desc['id']}"
        if desc.get("icon"):
            self._attr_icon = desc["icon"]
        if desc.get("unit"):
            self._attr_native_unit_of_measurement = desc["unit"]
        self._attr_entity_category = _FEATURE_CATEGORY_MAP.get(desc.get("category"))
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
    def native_value(self) -> Any:
        return self.coordinator.features.get(self._fid)


class TapoSchedulesSensor(CoordinatorEntity[TapoCoordinator], SensorEntity):
    """Shows the vacuum's saved schedules (as configured in the Tapo app)
    decoded into something readable — time, repeat days, room order, and
    settings — pulled straight from the device via get_schedule_rules.
    """
    _attr_has_entity_name = True
    _attr_name             = "Schedules"
    _attr_icon             = "mdi:calendar-clock"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_schedules"
        self._entry           = entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name":        self.coordinator.device_name,
            "manufacturer":"TP-Link",
            "model":       self.coordinator.device_model,
        }

    @property
    def native_value(self) -> int:
        return len(self.coordinator.schedules)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"schedules": self.coordinator.schedules}


class TapoConsumableSensor(CoordinatorEntity[TapoCoordinator], SensorEntity):
    """Sensor showing hours remaining on a consumable part."""
    _attr_has_entity_name            = True
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class                = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry, ckey: str, label: str) -> None:
        super().__init__(coordinator)
        self._ckey             = ckey
        self._limit_h          = CONSUMABLE_LIMITS_H[ckey]
        self._attr_name        = f"{label} Remaining"
        self._attr_unique_id   = f"{entry.entry_id}_consumable_{ckey}"
        self._attr_icon        = "mdi:wrench"
        self._entry            = entry

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
        d = self.coordinator.data
        if not d:
            return None
        raw = d.get("consumables", {}).get(self._ckey)
        if raw is None:
            return None
        return round(max(0.0, self._limit_h - raw / 60), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        if not d:
            return {}
        raw = d.get("consumables", {}).get(self._ckey)
        if raw is None:
            return {}
        used_h = raw / 60
        return {
            "used_hours":   round(used_h, 1),
            "limit_hours":  self._limit_h,
            "percent_used": min(100, round(used_h / self._limit_h * 100, 1)),
        }
