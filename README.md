# ⚡ Power Manager — Smart Home Load Management (Home Assistant + AppDaemon)

**AppDaemon app for Home Assistant** that helps prevent **meter trips** by automatically managing household loads based on **grid power**.  
Designed for the Italian market with native support for **E-Distribuzione Open Meter (GEMIS)** logic, optional **home battery forced-charge reduction** (e.g., **Huawei Luna2000**), and EV charging control.

![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-41BDF5?logo=home-assistant&logoColor=white)
![AppDaemon](https://img.shields.io/badge/AppDaemon-4.x-2ea44f?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## 📌 What it does

Power Manager monitors your **grid consumption (W)** and applies a priority chain:

- **P0 — 🔋 Battery (optional):** progressively reduces forced charging power (step-based)
- **P1..Pn — Controllable devices:** sheds (turns off) loads by priority
- **👀 Non-controllable loads:** monitoring-only + “manual action needed” notifications

It also performs a **safe sequential restore** when the situation is stable again.

<img width="1847" height="910" alt="Immagine 2026-02-14 170413" src="https://github.com/user-attachments/assets/970d44d2-fed7-467c-bcd6-5afc4ba05afd" />

---

## 🧾 Open Meter (GEMIS) thresholds (example: 3 kW contract)

| Zone | Power | Meter behavior | Time |
|------|------:|----------------|------|
| 🟢 **Green** | < 3.3 kW (110%) | No limit | Unlimited |
| 🟡 **Yellow** | 3.3–4.0 kW (110–133%) | Progressive warnings | ≥ 3 hours |
| 🔴 **Red** | > 4.0 kW (133%) | Trip imminent | ≤ 2 minutes |

**Yellow warnings (typical):**
- After 2 min → `RIDURRE CARICO SUPERO POTENZA`
- After 92 min → `RISCHIO DISTACCO SUPERO POTENZA`
- After ≥ 3 hours → Trip

Above **133%** timings become much shorter (first warning ~1s, “risk trip” at ~1 min).  
Power Manager anticipates these behaviors by shedding loads before the meter trips.

---

## ✅ Features (v6)

### 🔋 Home battery management (Priority 0, optional)
- Progressive reduction of forced charge power (`luna_power_step`)
- Skips instantly if forced charging is not active
- Dedicated logic to distinguish **grid charging vs PV charging**
- Adaptive restore: sets charging power to the real available margin

### 🟡🔴 Smart Shedding
- **Minimum active power** filter (default 100W) to ignore standby
- Single-step or progressive shedding based on measured excess
- **Inverted switches** support (e.g., EV wallbox relay logic)
- `climate` domain support via `set_hvac_mode`
- Notifies non-controllable loads for manual intervention

### 🔁 Smart Restore (safe & sequential)
- Restores only devices that fit the available margin
- Mid-interval power check after each restore step
- Automatic re-shed if a restore step triggers a new overload
- Progressive backoff (restore interval × number of shed cycles)
- Maximum shed timeout with forced restore (default 30 min)

### 📣 Notifications
- **Telegram** (direct API, no HA integration required)
- **Alexa** announcements (optional), with **two configurable DND windows**
  - Defaults are auto-initialized if missing:
    - DND1: 23:00–08:00
    - DND2: 14:00–16:00

### 🧪 Safe testing
- **Test Mode**: uses a simulated power helper (`input_number.pm_test_power`)
- **Dry Run**: logic runs without switching any devices
- Can be combined for full “safe simulation”

### 🧩 Dashboard + HA Package included
- Full Lovelace dashboard (`ha_dashboard.yaml`)
- HA package (`packages/power_manager.yaml`) with helpers:
  - Runtime settings (contract power, hysteresis, restore interval, etc.)
  - Device entity configuration (switch/power sensor) and enable toggles
  - DND windows for Alexa



https://github.com/user-attachments/assets/4ff4b376-e4f2-4d2a-ab7c-7aaa916512fc

---

## 📦 Repository structure

Recommended layout:
```text
.
├─ power_manager.py
├─ apps.yaml.example
├─ packages/
│  └─ power_manager.yaml
├─ ha_dashboard.yaml
├─ LICENSE
└─ README.md
```

---

## 🔧 Requirements

### Software
- Home Assistant **2024.1+**
- AppDaemon **4.x**
- HACS: **Mushroom Cards** + **card-mod** (for the provided dashboard)

### Hardware / entities
- A grid power sensor in **Watts** (positive = import/consumption)
- Controllable loads as `switch.*` and/or `climate.*`
- Optional: home battery forced-charge control entities

---

## 🚀 Installation

### 1) Home Assistant package
Copy `packages/power_manager.yaml` to:
```text
config/packages/power_manager.yaml
```

Enable packages in `configuration.yaml` (if not already):
```yaml
homeassistant:
  packages: !include_dir_named packages
```

Restart Home Assistant.

### 2) AppDaemon app
Copy `power_manager.py` to your AppDaemon apps folder:
```text
appdaemon/apps/power_manager.py
```

### 3) Configure AppDaemon
Copy the example:
```bash
cp apps.yaml.example apps.yaml
```

Edit `apps.yaml` and set **all your real entity_ids**.

> ⚠️ Never commit `apps.yaml` (it may contain secrets). This repo includes a `.gitignore` that excludes it.

### 4) Dashboard (optional)
Import/paste `ha_dashboard.yaml` into a Lovelace dashboard.

---

## ⚙️ AppDaemon configuration (apps.yaml)

Top-level keys used by the app (as in `apps.yaml.example`):

```yaml
power_manager:
  module: power_manager
  class: PowerManager

  power_sensor: "sensor.YOUR_GRID_POWER_SENSOR"

  contract_power: 4500
  hysteresis: 200

  alexa_notify_service: "notify/alexa_media"

  telegram_bot_token: "YOUR_BOT_TOKEN"
  telegram_chat_id: 0

  # Optional battery forced charge (remove if not used)
  luna_charge_switch: "input_boolean.forcible_charge_switch"
  luna_power_slider: "input_number.power_slider"
  luna_power_sensor: "sensor.battery_power_dashboard"
  luna_power_step: 100

  stable_minutes_before_restore: 5
  min_shed_duration: 300

  devices: []
  non_controllable: []
```

### Device fields (controllable)
Required fields per device:
- `name`, `entity_id`, `priority`, `estimated_power`, `power_sensor`, `dashboard_prefix`

Optional fields:
- `domain` (default `switch`)
- `inverted`
- `shed_in_yellow`, `shed_in_red`
- `auto_restore`
- `needs_manual_restart`
- `turn_off_service`, `turn_on_service`

### Non-controllable loads (monitoring-only)
- `name`, `estimated_power`, `power_sensor`

---

## 🎛️ Runtime helpers (HA package)

These helpers are created by `packages/power_manager.yaml` and are used by the dashboard/app.

### Numbers
- `input_number.pm_contract_power`
- `input_number.pm_test_power`
- `input_number.pm_restore_interval`
- `input_number.pm_min_active_power`
- `input_number.pm_max_shed_time`
- `input_number.pm_stable_minutes`
- `input_number.pm_min_shed_duration`

### Booleans
- `input_boolean.pm_test_mode`
- `input_boolean.pm_dry_run`
- `input_boolean.pm_show_entity_config`
- Per-device enable toggles (example): `input_boolean.pm_lavatrice_enabled`, etc.

### DND
- `input_datetime.pm_dnd1_start`, `input_datetime.pm_dnd1_end`
- `input_datetime.pm_dnd2_start`, `input_datetime.pm_dnd2_end`

### Per-device entity configuration (dashboard)
- `input_text.<prefix>_switch`
- `input_text.<prefix>_power`

---

## 🖼️ Screenshots

Create a `docs/` folder and add your screenshots:
- `docs/overview.png`
- `docs/devices.png`
- `docs/settings.png`

Then add them here, e.g.:
```md
![Overview](docs/overview.png)
```

---

## 📄 License
MIT — see `LICENSE`.
