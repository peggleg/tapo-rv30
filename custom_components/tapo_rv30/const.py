"""Constants for Tapo RV30 integration."""
DOMAIN = "tapo_rv30"

CONF_HOST     = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_PORT     = "port"
DEFAULT_PORT  = 4433
CONF_TRANSPORT = "transport"
TRANSPORT_TPAP = "tpap"
TRANSPORT_AES  = "aes"

# Feature ids already covered by hand-written entities elsewhere (vacuum.py,
# select.py, sensor.py's fixed descriptions) — excluded from the generic
# Feature-registry sweep in sensor.py/binary_sensor.py/switch.py/number.py/
# button.py so we don't create a duplicate entity for the same data.
EXCLUDE_FEATURE_IDS = {
    "battery_level", "vacuum_status", "vacuum_error", "clean_area",
    "clean_count", "vacuum_fan_speed", "mop_waterlevel",
    "vacuum_start", "vacuum_pause", "vacuum_return_home",
    "main_brush_remaining", "side_brush_remaining", "filter_remaining",
    "sensor_remaining", "charging_contacts_remaining",
}


FAST_INTERVAL = 30   # seconds — status / battery / attrs
MAP_INTERVAL  = 300  # seconds — map image re-render

CONF_CLEANING_INTERVAL = "cleaning_interval"
CONF_IDLE_INTERVAL     = "idle_interval"

DEFAULT_CLEANING_INTERVAL = 10  # seconds — position update interval while cleaning
DEFAULT_IDLE_INTERVAL     = 30  # seconds — status update interval while idle

VACUUM_STATES = {
    0:   "idle",
    1:   "cleaning",
    2:   "cleaning",   # mapping counts as cleaning
    4:   "returning",
    5:   "docked",
    6:   "docked",
    7:   "paused",
    8:   "idle",
    100: "error",
}

FAN_SPEED_LIST = ["Quiet", "Standard", "Turbo", "Max", "Ultra"]
FAN_NAME_TO_INT = {n.lower(): i + 1 for i, n in enumerate(FAN_SPEED_LIST)}
FAN_INT_TO_NAME = {v: k.capitalize() for k, v in FAN_NAME_TO_INT.items()}

WATER_NAME_TO_INT = {"off": 0, "low": 1, "medium": 2, "high": 3}
WATER_INT_TO_NAME = {v: k for k, v in WATER_NAME_TO_INT.items()}

ERROR_CODES = {
    0:  "Ok",
    2:  "Side Brush Stuck",
    3:  "Main Brush Stuck",
    4:  "Wheel Blocked",
    6:  "Trapped",
    7:  "Trapped (Cliff)",
    14: "Dust Bin Removed",
    15: "Unable To Move",
    16: "Lidar Blocked",
    21: "Unable To Find Dock",
    22: "Battery Low",
}

CONSUMABLE_LIMITS_H = {
    "roll_brush_time":     400,
    "edge_brush_time":     200,
    "filter_time":         200,
    "sensor_time":          30,
    "charge_contact_time":  30,
}
CONSUMABLE_LABELS = {
    "roll_brush_time":     "Main Brush",
    "edge_brush_time":     "Side Brush",
    "filter_time":         "Filter",
    "sensor_time":         "Sensor",
    "charge_contact_time": "Charge Contacts",
}

# Modern, refined room colours (R, G, B) — high contrast, clean app aesthetic
ROOM_PALETTE = [
    (180, 215, 248),  # soft sky blue
    (188, 228, 202),  # sage green
    (252, 238, 178),  # warm butter yellow
    (218, 198, 245),  # soft lavender
    (252, 206, 188),  # soft peach
    (178, 232, 232),  # soft teal
    (250, 192, 204),  # soft blush rose
    (210, 230, 214),  # pale mint
]
WALL_COLOR    = (44,  48,  56)     # crisp architectural slate wall
UNKNOWN_COLOR = (236, 239, 243)    # clean off-white / light canvas
FLOOR_COLOR   = (250, 252, 255)    # bright clean floor
