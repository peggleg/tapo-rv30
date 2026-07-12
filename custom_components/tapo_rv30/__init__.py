"""Tapo RV30 Robot Vacuum integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall

from .aes_client import AesVacuumClient
from .const import CONF_TRANSPORT, DEFAULT_PORT, DOMAIN, TRANSPORT_AES, TRANSPORT_TPAP
from .coordinator import TapoCoordinator
from .tpap import TapoVacuumClient

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [
    Platform.VACUUM, Platform.SENSOR, Platform.CAMERA, Platform.SELECT,
    Platform.BINARY_SENSOR, Platform.SWITCH, Platform.NUMBER, Platform.BUTTON,
]


def _build_client(entry: ConfigEntry):
    """Instantiate the right transport client for this entry.

    Existing config entries created before transport auto-detection was
    added won't have CONF_TRANSPORT set — default those to TPAP so
    upgrading the integration doesn't break already-working setups.
    """
    transport = entry.data.get(CONF_TRANSPORT, TRANSPORT_TPAP)
    kwargs = dict(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        port=DEFAULT_PORT,
    )
    if transport == TRANSPORT_AES:
        return AesVacuumClient(**kwargs)
    return TapoVacuumClient(**kwargs)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = _build_client(entry)
    coordinator = TapoCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def handle_clean_rooms(call: ServiceCall) -> None:
        """Service: tapo_rv30.clean_rooms."""
        entity_ids: list[str] = call.data.get("entity_id", [])
        rooms_raw = call.data.get("rooms", [])
        map_name: str | None = call.data.get("map")

        # Normalise rooms to a list — HA templates can produce a string when only
        # one room is selected, and iterating a string gives individual characters.
        if isinstance(rooms_raw, str):
            rooms: list[str] = [rooms_raw]
        else:
            rooms = list(rooms_raw)

        if not rooms:
            _LOGGER.error("clean_rooms: 'rooms' field is required")
            return

        # Find the coordinator for the target entity
        coord: TapoCoordinator | None = None
        for eid in entity_ids:
            state = hass.states.get(eid)
            if state and state.attributes.get("integration") == DOMAIN:
                coord = coordinator
                break
        if coord is None:
            coord = coordinator   # fallback to first/only

        try:
            # Fetch rooms live from the device so we always use the correct map_id
            # and support the optional map_name filter.
            room_ids, map_id = await hass.async_add_executor_job(
                coord.resolve_rooms_live, rooms, map_name
            )
            await hass.async_add_executor_job(coord.client.clean_rooms, room_ids, map_id)
            # Trigger a map refresh so the in-progress path shows promptly
            await coordinator.async_request_refresh()
        except ValueError as exc:
            _LOGGER.error("clean_rooms: %s", exc)

    hass.services.async_register(DOMAIN, "clean_rooms", handle_clean_rooms)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.services.async_remove(DOMAIN, "clean_rooms")
    return ok
