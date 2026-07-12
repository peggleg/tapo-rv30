"""DataUpdateCoordinator for Tapo RV30."""
from __future__ import annotations

import base64
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .const import (
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
FONT_SIZE   = 22
BLUR_RADIUS = 2.6  # applied at render_scale, before downsampling — smooths the
                    # grid-cell "staircase" on room boundaries into soft curves


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


def _render_map_image(map_data: dict) -> bytes:
    """Decode LZ4 pixel data and produce a lossless PNG image as bytes.

    Two-pass render:
      1. Room/wall/floor fills are drawn on their own layer at
         MAP_SCALE * SUPERSAMPLE resolution, then Gaussian-blurred. This
         is what actually smooths the blocky "staircase" look — the
         staircase comes from the source grid's own resolution, not
         from pixel-scaling artifacts, so scaling up or resizing alone
         (the old approach) can't fix it; only blurring the boundary
         can, since it's a real low-res boundary, not an aliasing
         artifact.
      2. Room labels and dock/vacuum markers are drawn crisp on top of
         the blurred base, then the whole composite is downsampled with
         LANCZOS for final anti-aliasing.
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

    # Build colour lookup table (0-255)
    lut: list[tuple[int, int, int]] = [UNKNOWN_COLOR] * 256
    lut[0]   = WALL_COLOR
    lut[127] = UNKNOWN_COLOR
    lut[255] = FLOOR_COLOR
    for rid, color in room_colors.items():
        if 0 <= rid <= 255:
            lut[rid] = color

    render_scale = MAP_SCALE * SUPERSAMPLE

    # --- Pass 1: base fill layer, blurred to smooth cell-boundary edges ---
    base = Image.new("RGB", (width * render_scale, height * render_scale))
    bdraw = ImageDraw.Draw(base)
    for row in range(height - 1, -1, -1):
        for col in range(width):
            pv    = pixels[row * width + col]
            color = lut[pv] if pv < 256 else UNKNOWN_COLOR
            screen_row = (height - 1 - row) * render_scale
            screen_col = col * render_scale
            bdraw.rectangle(
                [screen_col, screen_row,
                 screen_col + render_scale - 1, screen_row + render_scale - 1],
                fill=color,
            )
    base = base.filter(ImageFilter.GaussianBlur(radius=BLUR_RADIUS * SUPERSAMPLE))

    # --- Pass 2: crisp labels + markers on top of the blurred base ---
    img  = base
    draw = ImageDraw.Draw(img)

    try:
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
        # Find centroid of all pixels belonging to this room
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

        # Solid contrast backdrop behind the label — pastel room fills make
        # plain white/shadow text hard to read at a glance, especially once
        # downsampled, so this guarantees legibility regardless of room color.
        bbox = draw.textbbox((cx, cy), name, font=font, anchor="mm")
        pad = 6 * SUPERSAMPLE
        draw.rounded_rectangle(
            [bbox[0] - pad, bbox[1] - pad // 2, bbox[2] + pad, bbox[3] + pad // 2],
            radius=6 * SUPERSAMPLE,
            fill=(30, 30, 30),
        )
        draw.text((cx, cy), name, fill=(255, 255, 255), font=font, anchor="mm")

    # Charger and vacuum markers
    charge = map_data.get("charge_coor")
    vac    = map_data.get("vac_coor")

    def _dot(gx, gy, color, radius=7 * SUPERSAMPLE):
        sx = gx * render_scale + render_scale // 2
        sy = (height - 1 - gy) * render_scale + render_scale // 2
        draw.ellipse([sx - radius, sy - radius, sx + radius, sy + radius],
                     fill=color, outline=(255, 255, 255), width=2 * SUPERSAMPLE)

    if charge:
        _dot(charge[0], charge[1], (255, 200, 0))   # amber = dock
    if vac:
        _dot(vac[0], vac[1], (0, 180, 255))          # cyan = vacuum

    # Downsample from the supersampled render for final anti-aliasing
    final_size = (width * MAP_SCALE, height * MAP_SCALE)
    img = img.resize(final_size, Image.LANCZOS)

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class TapoCoordinator(DataUpdateCoordinator):
    """Polls Jarvis for status + periodically re-renders map."""

    def __init__(self, hass: HomeAssistant, client: TapoVacuumClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=FAST_INTERVAL),
        )
        self.client          = client
        self._map_tick       = 0      # counts update cycles; refresh map every N
        self._map_cycles     = MAP_INTERVAL // FAST_INTERVAL
        self.map_image_bytes: bytes | None = None
        self.rooms:  list[dict] = []   # current rooms (area_list, type==room)
        self.map_id: int | None = None # current map_id
        self.device_name:  str = "Tapo RV30"
        self.device_model: str = "Tapo RV30"
        self._name_fetched = False

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

        # Refresh map on first load and every MAP_INTERVAL seconds
        self._map_tick += 1
        if self.map_image_bytes is None or self._map_tick >= self._map_cycles:
            self._map_tick = 0
            try:
                await self.hass.async_add_executor_job(self._refresh_map)
            except Exception as exc:
                _LOGGER.warning("Map refresh failed: %s", exc)
            if hasattr(self.client, "get_schedules"):
                try:
                    await self.hass.async_add_executor_job(self._refresh_schedules)
                except Exception as exc:
                    _LOGGER.debug("Schedule refresh failed: %s", exc)

        return data

    def _refresh_map(self) -> None:
        current_id, _ = self.client.get_map_info()
        map_data       = self.client.get_map_data(current_id)
        self.map_id    = current_id
        self.rooms     = [a for a in map_data.get("area_list", [])
                          if a.get("type") == "room"]
        self.map_image_bytes = _render_map_image(map_data)
        _LOGGER.debug("Map rendered: %d bytes, %d rooms",
                      len(self.map_image_bytes), len(self.rooms))

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
