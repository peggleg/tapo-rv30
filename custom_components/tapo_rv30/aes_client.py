"""AES-transport client for Tapo RV-family vacuums.

Some RV30/RV20/RV30C units report `encrypt_type: AES` during discovery
rather than the newer TPAP/SPAKE2+ scheme handled by tpap.py. These
devices are already fully supported by the upstream `python-kasa`
library (its SmartProtocol/AesTransport implementation), so rather than
re-implementing that handshake, this module wraps python-kasa for
connection/auth and layers the same vacuum-specific `setSwitchClean`
room-cleaning payload documented in tpap.py on top of it.

This class exposes the exact same synchronous public method surface as
tpap.TapoVacuumClient, so it is a drop-in replacement selected
automatically by config_flow.py based on what the device reports.
"""
from __future__ import annotations

import asyncio
import base64
import enum
import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from kasa import Discover
from kasa.exceptions import AuthenticationError

_LOGGER = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when AES login fails (wrong credentials)."""


def _coerce_feature_value(val: Any) -> Any:
    """Convert a python-kasa Feature value into something HA entities can
    hold directly (str/int/float/bool) — Feature values can be timedelta
    (consumable used/remaining), datetime (device_time), or Enum members
    (e.g. status codes), none of which are safe to hand straight to a
    generic sensor's native_value without knowing its device_class.
    """
    if isinstance(val, timedelta):
        return round(val.total_seconds() / 3600, 2)  # hours, matches
        # the existing hand-written consumable sensors' units
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, enum.Enum):
        return val.name
    return val


class _LoopThread:
    """Owns a single background asyncio event loop for the lifetime of a
    client instance, so we reuse one authenticated connection across
    polls instead of paying a full reconnect+handshake cost every call.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=30)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)


class AesVacuumClient:
    """Synchronous facade around python-kasa's SmartDevice for AES-scheme
    Tapo RV vacuums. All public methods are blocking — call them via
    hass.async_add_executor_job, exactly like tpap.TapoVacuumClient.
    """

    def __init__(self, host: str, username: str, password: str, port: int = 4433) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self._loopthread = _LoopThread()
        self._device = None
        # Fully independent connection used ONLY by the feature/diagnostics
        # bridge below — deliberately never shared with self._device (used
        # by get_status/get_consumables/clean_rooms/etc). A failure calling
        # dev.update() for the feature bridge must be structurally incapable
        # of affecting the core polling path that every basic sensor
        # depends on; sharing one connection object between both was
        # exactly what caused a feature-bridge hiccup to cascade into every
        # sensor going unavailable.
        self._feature_device = None

    # ---- connection ------------------------------------------------------
    async def _connect(self):
        if self._device is not None:
            return self._device
        try:
            dev = await Discover.discover_single(
                self.host, username=self.username, password=self.password
            )
            await dev.update()
        except AuthenticationError as exc:
            raise AuthError(str(exc)) from exc
        self._device = dev
        return dev

    def authenticate(self) -> None:
        self._loopthread.run(self._connect())

    async def _connect_features(self):
        """Independent connection for the feature/diagnostics bridge —
        see the note on self._feature_device in __init__."""
        if self._feature_device is not None:
            return self._feature_device
        try:
            dev = await Discover.discover_single(
                self.host, username=self.username, password=self.password
            )
            await dev.update()
        except AuthenticationError as exc:
            raise AuthError(str(exc)) from exc
        self._feature_device = dev
        return dev

    async def _query(self, method: str, params: dict | None = None) -> Any:
        dev = await self._connect()
        try:
            resp = await dev._raw_query({method: params or {}})
        except AuthenticationError as exc:
            self._device = None
            raise AuthError(str(exc)) from exc
        except Exception as exc:
            # Whatever went wrong (expired session, device reboot, a stray
            # error from an unrelated call sharing this connection) — never
            # keep polling against a connection we know is bad. Reconnect
            # once and retry; if that also fails, let it propagate so the
            # coordinator can surface it instead of silently going stale.
            _LOGGER.debug("Query %s failed (%s), reconnecting once", method, exc)
            self._device = None
            dev = await self._connect()
            resp = await dev._raw_query({method: params or {}})
        return resp.get(method)

    def send(self, method: str, params: dict | None = None) -> Any:
        """Send a raw vacuum method and return its unwrapped result dict
        (matches the shape tpap.TapoVacuumClient callers already expect
        after their own ["result"] indexing — see note in each method
        below, since python-kasa unwraps `result` for us already)."""
        return self._loopthread.run(self._query(method, params))

    def close(self) -> None:
        self._loopthread.close()

    # ---- generic Feature-registry bridge ----------------------------------
    # python-kasa auto-registers a Feature object for most things a device
    # exposes (consumables, carpet_boost, child_lock, overheated, clean
    # progress/time, reboot, wifi signal, device time, ...) based on which
    # components the device actually negotiated. Rather than hand-writing
    # an entity per field, we expose the whole registry generically here —
    # const.EXCLUDE_FEATURE_IDS filters out the handful already covered by
    # the hand-written entities above (battery, status, error, clean_area,
    # clean_count, fan_speed, start/pause/return_home, *_remaining).
    async def _list_feature_descriptors(self) -> list[dict]:
        dev = await self._connect_features()
        try:
            await dev.update()
        except AuthenticationError as exc:
            self._feature_device = None
            raise AuthError(str(exc)) from exc
        except Exception:
            # Isolated to self._feature_device only — cannot touch the
            # separate connection get_status()/get_consumables() use.
            self._feature_device = None
            raise
        out = []
        for fid, feat in dev.features.items():
            out.append({
                "id": fid,
                "name": feat.name,
                "type": feat.type.name,
                "category": feat.category.name,
                "icon": feat.icon,
                "unit": feat.unit,
                "choices": feat.choices,
                "min": feat.minimum_value if feat.type == feat.Type.Number else None,
                "max": feat.maximum_value if feat.type == feat.Type.Number else None,
            })
        return out

    def list_feature_descriptors(self) -> list[dict]:
        """One-time metadata fetch (name/type/unit/etc per feature) — call
        once at setup, not on every poll."""
        return self._loopthread.run(self._list_feature_descriptors())

    async def _refresh_features(self) -> dict:
        dev = await self._connect_features()
        try:
            await dev.update()
        except AuthenticationError as exc:
            self._feature_device = None
            raise AuthError(str(exc)) from exc
        except Exception:
            self._feature_device = None
            raise
        values: dict[str, Any] = {}
        for fid, feat in dev.features.items():
            if feat.type == feat.Type.Action:
                continue  # buttons have no value to poll
            try:
                values[fid] = _coerce_feature_value(feat.value)
            except Exception as exc:
                _LOGGER.debug("Skipping feature %s (read failed): %s", fid, exc)
        return values

    def refresh_features(self) -> dict:
        """Bulk value snapshot for every feature — call every poll cycle."""
        return self._loopthread.run(self._refresh_features())

    async def _set_feature_value(self, feature_id: str, value: Any) -> None:
        dev = await self._connect_features()
        try:
            feat = dev.features[feature_id]
            await feat.set_value(value)
        except AuthenticationError as exc:
            self._feature_device = None
            raise AuthError(str(exc)) from exc
        except Exception:
            self._feature_device = None
            raise

    def set_feature_value(self, feature_id: str, value: Any = None) -> None:
        """Generic setter — covers switches, numbers, and actions (buttons
        pass value=None; Feature.set_value ignores it for Action type)."""
        self._loopthread.run(self._set_feature_value(feature_id, value))

    # ---- high-level API — mirrors tpap.TapoVacuumClient exactly ----------
    def get_status(self) -> dict:
        vac  = self.send("getVacStatus") or {}
        batt = self.send("getBatteryInfo") or {}
        info = self.send("getCleanInfo") or {}
        attr = self.send("getCleanAttr", {"type": "global"}) or {}
        mop  = self.send("getMopState") or {}
        return {
            "status_code":   vac.get("status", 0),
            "error_codes":   vac.get("err_status") or [0],
            "battery":       batt.get("battery_percentage", 0),
            "suction":       attr.get("suction", 4),
            "cistern":       attr.get("cistern", 0),
            "clean_number":  attr.get("clean_number", 1),
            "mop_attached":  mop.get("mop_state", False),
            "clean_area":    info.get("clean_area", 0),
            "clean_time":    info.get("clean_time", 0),
            "clean_percent": info.get("clean_percent", 0),
        }

    def get_nickname(self) -> str:
        info = self.send("get_device_info") or {}
        raw = info.get("nickname", "")
        try:
            return base64.b64decode(raw).decode(errors="replace").strip() or "Tapo RV30"
        except Exception:
            return raw or "Tapo RV30"

    def get_model(self) -> str:
        info = self.send("get_device_info") or {}
        model = info.get("model", "")
        return f"Tapo {model}".strip() if model else "Tapo RV30"

    def get_consumables(self) -> dict:
        return self.send("getConsumablesInfo") or {}

    def get_map_info(self) -> tuple[int, list[dict]]:
        r = self.send("getMapInfo") or {}
        return r["current_map_id"], r.get("map_list", [])

    def get_map_data(self, map_id: int) -> dict:
        return self.send("getMapData", {"map_id": map_id}) or {}

    def get_schedules(self) -> list[dict]:
        """Return the vacuum's saved schedule rules exactly as configured
        in the Tapo app (time, repeat days, room order, suction/water/
        passes) — confirmed via get_schedule_rules, which needs an
        explicit start_index param or the device returns PARAMS_ERROR."""
        r = self.send("get_schedule_rules", {"start_index": 0}) or {}
        return r.get("rule_list", [])

    def start(self) -> None:
        self.send("setSwitchClean", {
            "clean_mode": 0, "clean_on": True,
            "clean_order": True, "force_clean": False,
        })

    def clean_rooms(self, room_ids: list[int], map_id: int) -> None:
        self.send("setSwitchClean", {
            "clean_mode":  3,
            "clean_on":    True,
            "clean_order": True,
            "force_clean": False,
            "map_id":      map_id,
            "room_list":   list(room_ids),
            "start_type":  1,
        })

    def pause(self) -> None:
        status = (self.send("getVacStatus") or {}).get("status")
        if status == 4:
            self.send("setSwitchCharge", {"switch_charge": False})
        else:
            self.send("setRobotPause", {"pause": True})

    def resume(self) -> None:
        self.send("setRobotPause", {"pause": False})

    def dock(self) -> None:
        self.send("setSwitchCharge", {"switch_charge": True})

    def stop(self) -> None:
        self.pause()

    def set_fan_speed(self, value: int) -> None:
        self.send("setCleanAttr", {"suction": value, "type": "global"})

    def set_passes(self, value: int) -> None:
        self.send("setCleanAttr", {"clean_number": value, "type": "global"})

    def set_water(self, value: int) -> None:
        cur = self.send("getCleanAttr", {"type": "global"}) or {}
        cur["cistern"] = value
        cur["type"] = "global"
        self.send("setCleanAttr", cur)
