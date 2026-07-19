# Tapo RV30 Robot Vacuum — Home Assistant Integration

Local-only Home Assistant integration for the **TP-Link Tapo RV30 Max Plus** robot vacuum.

Implements the **TPAP / SPAKE2+** authentication protocol reverse-engineered from [python-kasa PR #1592](https://github.com/python-kasa/python-kasa/pull/1592).
No cloud dependency — communicates directly with the vacuum over your LAN.

With help from Claude the RV30C Mop is now supported using the **AES** authentication protocol.

## Features

- Full vacuum control — start, pause, stop, dock
- **Room-by-room cleaning** via `tapo_rv30.clean_rooms` service
- Fan speed selection (Quiet / Standard / Turbo / Max / Ultra)
- Water level select (Off / Low / Medium / High)
- Clean passes select (1 / 2 / 3)
- Battery sensor
- Error state sensor (e.g. "Ok", "Dust Bin Removed", "Trapped")
- Consumable wear sensors (main brush, side brush, filter, sensor, charge contacts)
- Config flow UI — set up from Settings → Devices & Services
- Schedules imported from Tapo App
- Check for firmware uptes
- Reset consumables (main brush, side brush, filter, sensor, charge contacts)

## Requirements

- Home Assistant 2024.1+
- [HACS](https://hacs.xyz) installed
- Tapo RV30 or RV20 on firmware **1.2.x+** (AES / TPAP protocol)
- Python packages (installed automatically by HACS): `requests`, `ecdsa`, `Pillow`

## Installation

1. https://github.com/peggleg/tapo-rv30
2. Copy the custom_components/tapo-rv30 folder into your Home Assistant config/custom_components/ directory, so you end up with config/custom_components/tapo-rv30/.
3. Restart Home Assistant.

## Supported Models

- **RV30 Max Plus (EU)** firmware 1.3.2
- **RV20 Max Plus (EU)** firmware 1.2.0
- **RV20C Mop** firmware 1.2.2

Should work on any Tapo RobovAC using AES / TPAP.

## Sample
**Script**
```
alias: Bonnie - Clean All Rooms
sequence:
  - action: tapo_rv30.clean_rooms
    target:
      entity_id: vacuum.bonnie
    data:
      rooms:
        - Lounge
        - Utility Room
        - Kitchen
        - Dining Room
        - Bathroom
        - Entrance
mode: single
description: Triggers the vacuum to clean all areas downstairs.
```

**Dashboard Button**
```
type: vertical-stack
cards:
  - square: false
    type: grid
    cards:
      - type: custom:mushroom-template-card
        primary: Bonnie
        icon: mdi:robot-vacuum
        tap_action:
          action: perform-action
          perform_action: script.clean_bathroom
          target: {}
        hold_action:
          action: none
        double_tap_action:
          action: none
        entity: script.clean_bathroom
        color: green
        features_position: bottom
        multiline_secondary: true
        secondary: All Rooms
        vertical: true
```

## Credits

- SPAKE2+ protocol implementation based on reverse engineering by the [python-kasa](https://github.com/python-kasa/python-kasa) project.
- https://github.com/epg-pers/tapo-rv30-ha for getting this one off the ground
