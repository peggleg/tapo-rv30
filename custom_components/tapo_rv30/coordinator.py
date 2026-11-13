"""DataUpdateCoordinator for Tapo RV30."""
from __future__ import annotations

import base64
import io
import logging
import math
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .const import (
    CONF_CLEANING_INTERVAL,
    CONF_IDLE_INTERVAL,
    DEFAULT_CLEANING_INTERVAL,
    DEFAULT_IDLE_INTERVAL,
    DOMAIN,
    FAST_INTERVAL,
    MAP_INTERVAL,
    ROOM_PALETTE,
    WALL_COLOR,
    UNKNOWN_COLOR,
    FLOOR_COLOR,
)
from .tpap import TapoVacuumClient

_LOGGER = logging.getLogger(__name__)

MAP_SCALE   = 6   # px per vacuum grid cell in the final image
SUPERSAMPLE = 3   # render at MAP_SCALE*SUPERSAMPLE then downscale for anti-aliasing
FONT_SIZE   = 20
BLUR_RADIUS = 2.4  # smooths grid-cell boundary into soft curves

FONT_PATH = Path(__file__).parent / "fonts" / "DejaVuSans-Bold.ttf"


def _lz4_block_decompress(data: bytes, uncompressed_size: int) -> bytes:
    """Pure-Python LZ4 block decompressor — no C extension needed."""
    out = bytearray(uncompressed_size)
    src = 0
    dst = 0
    n = len(data)
    while src < n:
        token = data[src]; src += 1
        # Literal run
        lit_len = token >> 4
        if lit_len == 15:
            while src < n:
                extra = data[src]; src += 1
                lit_len += extra
                if extra != 255:
                    break
        out[dst:dst + lit_len] = data[src:src + lit_len]
        src += lit_len
        dst += lit_len
        if src >= n:
            break
        # Match copy
        offset = data[src] | (data[src + 1] << 8); src += 2
        match_len = (token & 0xF) + 4
        if match_len == 19:  # 4 + 15
            while src < n:
                extra = data[src]; src += 1
                match_len += extra
                if extra != 255:
                    break
        match_pos = dst - offset
        for i in range(match_len):
            out[dst + i] = out[match_pos + i]
        dst += match_len
    return bytes(out)


def _b64name(s: str) -> str:
    try:
        return base64.b64decode(s).decode(errors="replace").strip()
    except Exception:
        return s


# Confirmed against a real device: a test schedule set for Mon/Wed/Fri
# came back with week_day=42, and 2(Mon)+8(Wed)+32(Fri)=42 exactly under
# this Sun=1..Sat=64 convention — not a guess, an exact numeric match.
_WEEKDAY_BITS = [
    (1,  "Sun"), (2,  "Mon"), (4,  "Tue"), (8,  "Wed"),
    (16, "Thu"), (32, "Fri"), (64, "Sat"),
]


def _decode_weekdays(mask: int) -> list[str]:
    return [name for bit, name in _WEEKDAY_BITS if mask & bit]


def _decode_schedule(rule: dict, room_names: dict[int, str]) -> dict:
    """Turn one raw schedule rule into a human-readable summary."""
    attr = rule.get("clean_attr", {})
    room_ids = attr.get("room_list", [])
    s_min = rule.get("s_min", 0)
    return {
        "id": rule.get("id"),
        "enabled": rule.get("enable", False),
        "time": f"{s_min // 60:02d}:{s_min % 60:02d}",
        "days": _decode_weekdays(rule.get("week_day", 0)),
        "repeat": rule.get("mode") == "repeat",
        "rooms": [room_names.get(rid, f"Room {rid}") for rid in room_ids],
        "room_ids": room_ids,
        "clean_order": attr.get("clean_order", False),
        "suction": attr.get("suction"),
        "water_level": attr.get("cistern"),
        "clean_passes": attr.get("clean_number"),
    }


def _render_base_map_image(map_data: dict) -> tuple[Image.Image, int, int]:
    """Decode LZ4 pixel data and render a clean, high-contrast base map
    (soft blurred room fills, solid architectural walls, TrueType room labels,
    and charging dock glyph) downsampled with Lanczos anti-aliasing.

    Returns (base_image, width, height).
    """
    width   = map_data["width"]
    height  = map_data["height"]
    pix_len = map_data["pix_len"]

    raw     = base64.b64decode(map_data["map_data"])
    pixels  = _lz4_block_decompress(raw, uncompressed_size=pix_len)

    rooms = [a for a in map_data.get("area_list", []) if a.get("type") == "room"]
    sorted_ids  = sorted(r["id"] for r in rooms)
    room_colors = {rid: ROOM_PALETTE[i % len(ROOM_PALETTE)]
                   for i, rid in enumerate(sorted_ids)}

    # Build colour lookup table (0-255).
    # In Pass 1 (floor/room blur pass), walls are filled with FLOOR_COLOR so
    # the room boundaries smooth naturally without bleeding dark wall pixels into the rooms.
    lut_fill: list[tuple[int, int, int]] = [UNKNOWN_COLOR] * 256
    lut_fill[0]   = FLOOR_COLOR
    lut_fill[127] = UNKNOWN_COLOR
    lut_fill[255] = FLOOR_COLOR
    for rid, color in room_colors.items():
        if 0 <= rid <= 255:
            lut_fill[rid] = color

    render_scale = MAP_SCALE * SUPERSAMPLE

    # --- Pass 1: soft room and floor fills ---
    base = Image.new("RGB", (width * render_scale, height * render_scale), UNKNOWN_COLOR)
    bdraw = ImageDraw.Draw(base)
    for row in range(height - 1, -1, -1):
        for col in range(width):
            pv = pixels[row * width + col]
            if pv == 127:
                continue  # leave background untouched
            color = lut_fill[pv] if pv < 256 else UNKNOWN_COLOR
            screen_row = (height - 1 - row) * render_scale
            screen_col = col * render_scale
            bdraw.rectangle(
                [screen_col, screen_row,
                 screen_col + render_scale - 1, screen_row + render_scale - 1],
                fill=color,
            )
    base = base.filter(ImageFilter.GaussianBlur(radius=BLUR_RADIUS * SUPERSAMPLE))

    # --- Pass 2: sharp, solid architectural walls on top of blurred fills ---
    wdraw = ImageDraw.Draw(base)
    for row in range(height - 1, -1, -1):
        for col in range(width):
            pv = pixels[row * width + col]
            if pv == 0:  # wall / obstacle
                screen_row = (height - 1 - row) * render_scale
                screen_col = col * render_scale
                wdraw.rectangle(
                    [screen_col, screen_row,
                     screen_col + render_scale - 1, screen_row + render_scale - 1],
                    fill=WALL_COLOR,
                )

    # --- Pass 3: crisp room badges using bundled TrueType font ---
    try:
        if FONT_PATH.exists():
            font = ImageFont.truetype(str(FONT_PATH), FONT_SIZE * SUPERSAMPLE)
        else:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                FONT_SIZE * SUPERSAMPLE,
            )
    except Exception:
        font = ImageFont.load_default()

    for room in rooms:
        rid = room["id"]
        if rid not in room_colors:
            continue
        name = _b64name(room.get("name", ""))
        if not name:
            continue
        # Centroid of room pixels
        xs, ys = [], []
        for row in range(height):
            for col in range(width):
                if pixels[row * width + col] == rid:
                    xs.append(col)
                    ys.append(row)
        if not xs:
            continue
        cx = int(sum(xs) / len(xs)) * render_scale + render_scale // 2
        cy = int((height - 1 - (sum(ys) / len(ys)))) * render_scale + render_scale // 2

        # High-contrast modern badge pill
        bbox = wdraw.textbbox((cx, cy), name, font=font, anchor="mm")
        pad = 8 * SUPERSAMPLE
        wdraw.rounded_rectangle(
            [bbox[0] - pad, bbox[1] - pad // 2, bbox[2] + pad, bbox[3] + pad // 2],
            radius=6 * SUPERSAMPLE,
            fill=(36, 40, 48),
            outline=(85, 92, 105),
            width=SUPERSAMPLE,
        )
        wdraw.text((cx, cy), name, fill=(255, 255, 255), font=font, anchor="mm")

    # --- Pass 4: charging station dock glyph ---
    charge = map_data.get("charge_coor")
    if charge:
        gx, gy = charge[0], charge[1]
        sx = gx * render_scale + render_scale // 2
        sy = (height - 1 - gy) * render_scale + render_scale // 2
        r = 8 * SUPERSAMPLE
        # Dock station base
        wdraw.rounded_rectangle(
            [sx - r, sy - r, sx + r, sy + r],
            radius=3 * SUPERSAMPLE,
            fill=(42, 46, 54),
            outline=(255, 255, 255),
            width=2 * SUPERSAMPLE,
        )
        # Metallic charging contacts
        pin_h = 4 * SUPERSAMPLE
        wdraw.rectangle(
            [sx - 4 * SUPERSAMPLE, sy - pin_h, sx - 2 * SUPERSAMPLE, sy + pin_h],
            fill=(255, 190, 0),
        )
        wdraw.rectangle(
            [sx + 2 * SUPERSAMPLE, sy - pin_h, sx + 4 * SUPERSAMPLE, sy + pin_h],
            fill=(255, 190, 0),
        )

    # Downsample from supersampled render with Lanczos for final anti-aliasing
    final_size = (width * MAP_SCALE, height * MAP_SCALE)
    final_base = base.resize(final_size, Image.LANCZOS)
    return final_base, width, height


def _composite_vacuum_map(
    base_img: Image.Image,
    vac_coor: list[int] | tuple[int, int] | None,
    height: int,
    cleaning_path: list[tuple[int, int]] | None = None,
) -> bytes:
    """Draw cleaning trail and vector robot marker with heading onto base map."""
    img = base_img.copy().convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")

    # 1. Cleaning trail line
    if cleaning_path and len(cleaning_path) >= 2:
        pts = [
            (
                x * MAP_SCALE + MAP_SCALE // 2,
                (height - 1 - y) * MAP_SCALE + MAP_SCALE // 2,
            )
            for x, y in cleaning_path
        ]
        draw.line(pts, fill=(0, 160, 240, 180), width=2, joint="curve")

    # 2. Modern robot vacuum puck
    if vac_coor:
        gx, gy = vac_coor[0], vac_coor[1]
        sx = gx * MAP_SCALE + MAP_SCALE // 2
        sy = (height - 1 - gy) * MAP_SCALE + MAP_SCALE // 2

        # Drop shadow
        draw.ellipse([sx - 10, sy - 9, sx + 10, sy + 11], fill=(0, 0, 0, 70))
        # Outer robot body
        draw.ellipse(
            [sx - 9, sy - 9, sx + 9, sy + 9],
            fill=(255, 255, 255, 255),
            outline=(45, 50, 60, 255),
            width=2,
        )
        # Center LiDAR turret dome
        draw.ellipse(
            [sx - 4, sy - 4, sx + 4, sy + 4],
            fill=(0, 170, 255, 255),
        )

        # Directional heading wedge / arrow
        if cleaning_path and len(cleaning_path) >= 2:
            px, py = cleaning_path[-2]
            dx = gx - px
            dy = -(gy - py)  # Inverted Y screen coords
            if dx != 0 or dy != 0:
                angle = math.atan2(dy, dx)
                tip = (sx + int(12 * math.cos(angle)), sy + int(12 * math.sin(angle)))
                l_wing = (
                    sx + int(7 * math.cos(angle + 2.5)),
                    sy + int(7 * math.sin(angle + 2.5)),
                )
                r_wing = (
                    sx + int(7 * math.cos(angle - 2.5)),
                    sy + int(7 * math.sin(angle - 2.5)),
                )
                draw.polygon([tip, l_wing, r_wing], fill=(0, 170, 255, 255))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _render_map_image(map_data: dict) -> bytes:
    """Backward-compatible one-shot render of base map + vacuum marker."""
    base_img, _, height = _render_base_map_image(map_data)
    return _composite_vacuum_map(base_img, map_data.get("vac_coor"), height, [])


class TapoCoordinator(DataUpdateCoordinator):
    """Polls vacuum for status + periodically re-renders map."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: TapoVacuumClient,
        entry: ConfigEntry | None = None,
    ) -> None:
        self.entry = entry
        self.cleaning_interval = (
            entry.options.get(CONF_CLEANING_INTERVAL, DEFAULT_CLEANING_INTERVAL)
            if entry else DEFAULT_CLEANING_INTERVAL
        )
        self.idle_interval = (
            entry.options.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)
            if entry else DEFAULT_IDLE_INTERVAL
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.idle_interval),
        )
        self.client          = client
        self._map_tick       = 0      # counts update cycles; refresh map every N
        self._map_cycles     = max(1, MAP_INTERVAL // self.idle_interval)
        self.map_image_bytes: bytes | None = None
        self.rooms:  list[dict] = []   # current rooms (area_list, type==room)
        self.map_id: int | None = None # current map_id
        self.device_name:  str = "Tapo RV30"
        self.device_model: str = "Tapo RV30"
        self._name_fetched = False

        # Live coordinates & cached base map
        self.vacuum_coordinates: list[int] | None = None
        self.dock_coordinates: list[int] | None = None
        self.cleaning_path: list[tuple[int, int]] = []
        self._last_clean_time: int = 0
        self._cached_base_img: Image.Image | None = None
        self._cached_base_key: tuple | None = None
        self._cached_height: int = 0
        self._was_active: bool = False

        # Generic Feature-registry data (AES transport only — stays empty,
        # harmlessly, for TPAP-transport clients that don't implement it).
        self.feature_descriptors: list[dict] = []
        self.features: dict[str, Any] = {}
        self.schedules: list[dict] = []

    async def _async_update_data(self) -> dict[str, Any]:
        if not self._name_fetched:
            try:
                self.device_name = await self.hass.async_add_executor_job(
                    self.client.get_nickname
                )
                self.device_model = await self.hass.async_add_executor_job(
                    self.client.get_model
                )
            except Exception:
                pass
            if hasattr(self.client, "list_feature_descriptors"):
                try:
                    self.feature_descriptors = await self.hass.async_add_executor_job(
                        self.client.list_feature_descriptors
                    )
                except Exception as exc:
                    _LOGGER.debug("Feature descriptor fetch failed: %s", exc)
            self._name_fetched = True

        try:
            data = await self.hass.async_add_executor_job(self.client.get_status)
        except Exception as exc:
            raise UpdateFailed(f"Failed to fetch vacuum status: {exc}") from exc

        try:
            data["consumables"] = await self.hass.async_add_executor_job(
                self.client.get_consumables
            )
        except Exception as exc:
            _LOGGER.debug("Consumables fetch failed: %s", exc)
            data["consumables"] = {}

        if self.feature_descriptors and hasattr(self.client, "refresh_features"):
            try:
                self.features = await self.hass.async_add_executor_job(
                    self.client.refresh_features
                )
            except Exception as exc:
                _LOGGER.debug("Feature value refresh failed: %s", exc)

        is_active = data.get("status_code") in (1, 2, 4)

        # Reset cleaning path trail when a new cleaning run starts
        clean_time = data.get("clean_time", 0)
        if is_active and (clean_time < self._last_clean_time or (not self._was_active and is_active)):
            self.cleaning_path.clear()
        self._last_clean_time = clean_time

        # Dynamic update interval based on activity:
        # High frequency while cleaning/moving (10s by default),
        # standard frequency while idle/docked (30s by default).
        target_seconds = self.cleaning_interval if is_active else self.idle_interval
        target_interval = timedelta(seconds=target_seconds)
        if self.update_interval != target_interval:
            self.update_interval = target_interval
            self._map_cycles = max(1, MAP_INTERVAL // target_seconds)
            _LOGGER.debug(
                "Switched polling interval to %ds (cleaning=%s)",
                target_seconds, is_active,
            )

        self._map_tick += 1
        needs_full_map = self._map_tick >= self._map_cycles
        was_active = self._was_active
        self._was_active = is_active

        # Refresh map:
        # - on first load
        # - on EVERY cycle while active/cleaning so position stays accurate
        # - when transitioning from active -> idle (docked or stopped)
        # - periodically when idle (every MAP_INTERVAL seconds)
        if (
            self.map_image_bytes is None
            or is_active
            or (was_active and not is_active)
            or needs_full_map
        ):
            force_base = needs_full_map or (self.map_image_bytes is None)
            if force_base:
                self._map_tick = 0
            try:
                await self.hass.async_add_executor_job(self._refresh_map, force_base)
            except Exception as exc:
                _LOGGER.warning("Map refresh failed: %s", exc)

            if hasattr(self.client, "get_schedules") and (needs_full_map or not self.schedules):
                try:
                    await self.hass.async_add_executor_job(self._refresh_schedules)
                except Exception as exc:
                    _LOGGER.debug("Schedule refresh failed: %s", exc)

        return data

    def _refresh_map(self, force_base_rerender: bool = False) -> None:
        current_id, _ = self.client.get_map_info()
        map_data       = self.client.get_map_data(current_id)
        self.map_id    = current_id
        self.rooms     = [a for a in map_data.get("area_list", [])
                          if a.get("type") == "room"]
        self.dock_coordinates   = map_data.get("charge_coor")
        self.vacuum_coordinates = map_data.get("vac_coor")

        # Cache base map (room fills, walls, labels, dock) so that frequent
        # position updates only need to draw the single vacuum marker (<2ms)
        # instead of repeating full LZ4 decompression + 18x Gaussian blur.
        base_key = (
            current_id,
            tuple(self.dock_coordinates) if self.dock_coordinates else None,
            map_data.get("pix_len"),
            len(map_data.get("map_data", "")),
        )
        if (
            force_base_rerender
            or self._cached_base_img is None
            or self._cached_base_key != base_key
        ):
            base_img, _, height = _render_base_map_image(map_data)
            self._cached_base_img = base_img
            self._cached_base_key = base_key
            self._cached_height = height

        # Track cleaning path points while active
        is_active = bool(self.data and self.data.get("status_code") in (1, 2, 4))
        if self.vacuum_coordinates and (is_active or self._was_active):
            pt = (self.vacuum_coordinates[0], self.vacuum_coordinates[1])
            if not self.cleaning_path or self.cleaning_path[-1] != pt:
                self.cleaning_path.append(pt)
                if len(self.cleaning_path) > 10000:
                    self.cleaning_path.pop(0)

        self.map_image_bytes = _composite_vacuum_map(
            self._cached_base_img,
            self.vacuum_coordinates,
            self._cached_height,
            self.cleaning_path,
        )
        _LOGGER.debug(
            "Map updated: %d bytes, vac=%s, path_len=%d",
            len(self.map_image_bytes), self.vacuum_coordinates, len(self.cleaning_path),
        )

    def _refresh_schedules(self) -> None:
        room_names = {r["id"]: _b64name(r.get("name", "")) for r in self.rooms}
        raw_rules = self.client.get_schedules()
        self.schedules = [_decode_schedule(r, room_names) for r in raw_rules]
        _LOGGER.debug("Fetched %d schedule(s)", len(self.schedules))

    def resolve_rooms_live(
        self, name_patterns: list[str], map_name: str | None = None
    ) -> tuple[list[int], int]:
        """Fetch rooms live from device, resolve names → (room_ids, map_id).

        Uses map_name (partial match) if given, otherwise current map.
        Raises ValueError if map or any room is not found.
        """
        current_map_id, map_list = self.client.get_map_info()

        if map_name:
            target_id = next(
                (m["map_id"] for m in map_list
                 if map_name.lower() in _b64name(m.get("map_name", "")).lower()),
                None,
            )
            if target_id is None:
                available = [_b64name(m.get("map_name", "")) for m in map_list]
                raise ValueError(f"Map '{map_name}' not found. Available: {available}")
        else:
            target_id = current_map_id

        map_data = self.client.get_map_data(target_id)
        rooms = [a for a in map_data.get("area_list", []) if a.get("type") == "room"]

        matched: list[int] = []
        seen: set[int] = set()
        for pat in name_patterns:
            decoded = [_b64name(r.get("name", "")) for r in rooms]
            exact = [r for r, n in zip(rooms, decoded) if n.lower() == pat.lower()]
            hits = exact or [r for r, n in zip(rooms, decoded) if pat.lower() in n.lower()]
            if not hits:
                available = [_b64name(r.get("name", "")) for r in rooms]
                raise ValueError(f"No room matching '{pat}'. Available: {available}")
            for r in hits:
                if r["id"] not in seen:
                    seen.add(r["id"]); matched.append(r["id"])

        return matched, target_id
