"""
=============================================================================
  POWER MANAGER v6 - Gestione Intelligente Carichi Elettrici
  AppDaemon App per Home Assistant
=============================================================================

  Previene il distacco del contatore gestendo automaticamente
  i carichi domestici. Basato sulle specifiche E-Distribuzione
  Open Meter GEMIS.

  Configurazione: vedi apps.yaml.example
  Documentazione: vedi README.md

=============================================================================
"""

class PowerZone(Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class DeviceState(Enum):
    UNKNOWN = "unknown"
    ON_BY_USER = "on_by_user"
    OFF_BY_USER = "off_by_user"
    SHED = "shed"


class ManagedDevice:
    def __init__(self, entity_id, name, priority, estimated_power,
                 power_sensor=None, domain="switch",
                 turn_off_service=None, turn_on_service=None,
                 shed_in_yellow=True, shed_in_red=True,
                 auto_restore=True, needs_manual_restart=False,
                 inverted=False, controllable=True, enabled=True,
                 dashboard_prefix=None):
        self.entity_id = entity_id
        self.name = name
        self.priority = priority
        self.estimated_power = estimated_power
        self.power_sensor = power_sensor
        self.domain = domain
        self.turn_off_service = turn_off_service
        self.turn_on_service = turn_on_service
        self.shed_in_yellow = shed_in_yellow
        self.shed_in_red = shed_in_red
        self.auto_restore = auto_restore
        self.needs_manual_restart = needs_manual_restart
        self.inverted = inverted
        self.controllable = controllable
        self.enabled = enabled
        self.dashboard_prefix = dashboard_prefix
        self.state = DeviceState.UNKNOWN
        self.shed_time = None
        self.pre_shed_state = None
        self.last_known_power = 0.0  # v6: consumo reale pre-shed


class PowerManager(hass.Hass):

    def initialize(self):
        # =================================================================
        # CONFIGURAZIONE
        # =================================================================
        self.power_sensor = self.args.get(
            "power_sensor", "sensor.power_meter"
        )
        self.hysteresis = self.args.get("hysteresis", 200)
        self._recalculate_thresholds()

        # =================================================================
        # NOTIFICHE
        # =================================================================
        self.alexa_notify_service = self.args.get(
            "alexa_notify_service", "notify/alexa_media"
        )
        self.telegram_chat_id = self.args.get("telegram_chat_id", 0)
        self.telegram_bot_token = self.args.get("telegram_bot_token", "")

        # =================================================================
        # ACCUMULO DOMESTICO (es. Huawei Luna2000)
        # =================================================================
        self.luna_switch = self.args.get(
            "luna_charge_switch", self.luna_switch
        )
        self.luna_power_slider = self.args.get(
            "luna_power_slider", self.luna_power_slider
        )
        self.luna_power_sensor = self.args.get(
            "luna_power_sensor", self.luna_power_sensor
        )
        self.luna_power_step = self.args.get("luna_power_step", 100)

        # =================================================================
        # ANTI PING-PONG
        # =================================================================
        self.stable_minutes_before_restore = self.args.get(
            "stable_minutes_before_restore", 5
        )
        self.min_shed_duration = self.args.get("min_shed_duration", 300)

        # =================================================================
        # DISPOSITIVI
        # =================================================================
        self.devices = self._init_devices()
        self.non_controllable = self._init_non_controllable()

        # =================================================================
        # STATO INTERNO
        # =================================================================
        self.current_zone = PowerZone.GREEN
        self.zone_entry_time = None
        self.shed_active = False
        self.green_stable_since = None
        self.restore_in_progress = False
        self.restore_timer = None
        self.restore_check_timer = None
        self.current_check = None
        self.came_from_yellow = False

        # v6
        self.shed_cycle_count = 0
        self.restore_queue = []
        self.last_restored_device = None
        self.max_shed_timers = {}

        # v6: Luna2000 battery charging
        self.luna_was_charging = False
        self.luna_pre_shed_power = 0.0
        self.luna_reduced = False

        # Timer zona gialla
        self.yellow_check2_timer = None
        self.yellow_check3_timer = None
        self.yellow_check4_timer = None
        self.yellow_recheck_timer = None

        # Timer real-time
        self.realtime_timer = None

        # =================================================================
        # TEST MODE
        # =================================================================
        self.test_mode = False
        self.dry_run = False
        self._setup_test_mode()

        # =================================================================
        # LISTENER
        # =================================================================
        self.listen_state(self.on_power_change, self.power_sensor)

        if self.entity_exists("input_number.pm_contract_power"):
            self.listen_state(
                self._on_contract_power_change,
                "input_number.pm_contract_power"
            )
        self._setup_dashboard_listeners()

        # =================================================================
        # LOG
        # =================================================================
        min_active = self._get_min_active_power()
        restore_int = self._get_restore_interval()
        max_shed_t = self._get_max_shed_time()

        self.log("=" * 65)
        self.log("POWER MANAGER v6 INIZIALIZZATO")
        self.log(f"  Sensore:        {self.power_sensor}")
        self.log(f"  Contratto:      {self.contract_power:.0f} W")
        self.log(f"  Disponibile:    {self.available_power:.0f} W (110%)")
        self.log(f"  Soglia rossa:   {self.red_threshold:.0f} W (133%)")
        self.log(f"  Rientro verde:  {self.green_threshold:.0f} W "
                 f"(isteresi {self.hysteresis}W)")
        self.log(f"  Target shed:    {self.contract_power:.0f} W")
        self.log(f"  Soglia attivo:  {min_active:.0f} W")
        self.log(f"  Restore interv: {restore_int:.0f}s "
                 f"({restore_int / 60:.1f} min)")
        self.log(f"  Max shed time:  {max_shed_t:.0f}s "
                 f"({max_shed_t / 60:.0f} min)")
        self.log(f"  Telegram ID:    {self.telegram_chat_id}")
        tg_ok = "OK" if self.telegram_bot_token else "MANCA!"
        self.log(f"  Telegram Bot:   {tg_ok}")
        dnd = self._get_dnd_periods()
        self.log(f"  DND Alexa 1:    {dnd[0][0]}-{dnd[0][1]}")
        self.log(f"  DND Alexa 2:    {dnd[1][0]}-{dnd[1][1]}")
        luna_ok = "SI" if self.entity_exists(
            self.luna_switch) else "NO"
        self.log(f"  Luna2000:       {luna_ok}")
        self.log("  Priorita:")
        self.log("    P0: Luna2000 (riduzione/stop carica)")
        for d in sorted(self.devices, key=lambda x: x.priority):
            inv = " [INV]" if d.inverted else ""
            self.log(f"    P{d.priority}: {d.name} "
                     f"(~{d.estimated_power}W){inv}")
        self.log("=" * 65)

        self._sync_device_states()
        self._publish_state()

        # v6: stato iniziale per pm_elapsed_time (evita "unknown")
        self.set_state(
            "sensor.pm_elapsed_time",
            state="00:00",
            attributes={
                "friendly_name": "PM Tempo in zona",
                "icon": "mdi:timer-outline",
                "zone": "green",
                "elapsed_seconds": 0,
                "elapsed_minutes": 0.0,
            }
        )

        # v6: inizializza DND defaults se non impostati
        dnd_defaults = {
            "input_datetime.pm_dnd1_start": "23:00:00",
            "input_datetime.pm_dnd1_end": "08:00:00",
            "input_datetime.pm_dnd2_start": "14:00:00",
            "input_datetime.pm_dnd2_end": "16:00:00",
        }
        for entity_id, default_time in dnd_defaults.items():
            if self.entity_exists(entity_id):
                val = self.get_state(entity_id)
                if not val or val in ("unknown", "unavailable"):
                    self.call_service(
                        "input_datetime/set_datetime",
                        entity_id=entity_id,
                        time=default_time
                    )
                    self.log(f"  DND init: {entity_id} = {default_time}")

    # =====================================================================
    # SOGLIE DINAMICHE
    # =====================================================================

    def _recalculate_thresholds(self):
        if self.entity_exists("input_number.pm_contract_power"):
            try:
                self.contract_power = float(
                    self.get_state("input_number.pm_contract_power")
                )
            except (ValueError, TypeError):
                self.contract_power = self.args.get("contract_power", 4500)
        else:
            self.contract_power = self.args.get("contract_power", 4500)

        self.available_power = self.contract_power * 1.10
        self.red_threshold = self.contract_power * 1.33
        self.green_threshold = self.available_power - self.hysteresis
        self.shed_target = self.contract_power

    def _on_contract_power_change(self, entity, attribute, old, new, kwargs):
        try:
            float(new)
        except (ValueError, TypeError):
            return
        self._recalculate_thresholds()
        self.log(
            f"Contratto: {self.contract_power:.0f}W -> "
            f"gialla={self.available_power:.0f}W, "
            f"rossa={self.red_threshold:.0f}W"
        )
        self._publish_state()

    def _calc_excess_percent(self, power=None):
        if power is None:
            power = self._get_grid_power()
        if power <= self.contract_power:
            return 0.0
        return ((power - self.contract_power) / self.contract_power) * 100

    # =====================================================================
    # PARAMETRI DASHBOARD v6
    # =====================================================================

    def _get_min_active_power(self):
        if self.entity_exists("input_number.pm_min_active_power"):
            try:
                return float(
                    self.get_state("input_number.pm_min_active_power")
                )
            except (ValueError, TypeError):
                pass
        return 100.0

    def _get_restore_interval(self):
        if self.entity_exists("input_number.pm_restore_interval"):
            try:
                return float(
                    self.get_state("input_number.pm_restore_interval")
                )
            except (ValueError, TypeError):
                pass
        return 180.0

    def _get_max_shed_time(self):
        if self.entity_exists("input_number.pm_max_shed_time"):
            try:
                return float(
                    self.get_state("input_number.pm_max_shed_time")
                ) * 60  # dashboard in minuti -> secondi
            except (ValueError, TypeError):
                pass
        return 1800.0

    # =====================================================================
    # DISPOSITIVI (configurazione in apps.yaml → sezione "devices")
    # =====================================================================

    def _init_devices(self):
        """
        Carica la lista dispositivi da apps.yaml.
        Ogni device ha: name, entity_id, priority, estimated_power,
        power_sensor, dashboard_prefix e opzionali.
        """
        device_configs = self.args.get("devices", [])
        if not device_configs:
            self.log("ATTENZIONE: nessun device in apps.yaml!", level="WARNING")
            return []

        devices = []
        for cfg in device_configs:
            turn_off = None
            if "turn_off_service" in cfg:
                turn_off = cfg["turn_off_service"]
            elif cfg.get("domain") == "climate":
                turn_off = {
                    "service": "climate/set_hvac_mode",
                    "data": {
                        "entity_id": cfg["entity_id"],
                        "hvac_mode": "off",
                    },
                }

            devices.append(ManagedDevice(
                entity_id=cfg["entity_id"],
                name=cfg["name"],
                priority=cfg["priority"],
                estimated_power=cfg.get("estimated_power", 1000),
                power_sensor=cfg.get("power_sensor", ""),
                domain=cfg.get("domain", "switch"),
                turn_off_service=turn_off,
                turn_on_service=cfg.get("turn_on_service"),
                shed_in_yellow=cfg.get("shed_in_yellow", True),
                shed_in_red=cfg.get("shed_in_red", True),
                auto_restore=cfg.get("auto_restore", True),
                needs_manual_restart=cfg.get("needs_manual_restart", False),
                inverted=cfg.get("inverted", False),
                controllable=cfg.get("controllable", True),
                dashboard_prefix=cfg.get("dashboard_prefix", ""),
            ))
        return devices

    def _init_non_controllable(self):
        """Carica i dispositivi non controllabili da apps.yaml."""
        nc_configs = self.args.get("non_controllable", [])
        devices = []
        for cfg in nc_configs:
            devices.append(ManagedDevice(
                entity_id="",
                name=cfg["name"],
                priority=99,
                estimated_power=cfg.get("estimated_power", 1000),
                power_sensor=cfg.get("power_sensor", ""),
                controllable=False,
            ))
        return devices

    # =====================================================================
    # CORE
    # =====================================================================

    def on_power_change(self, entity, attribute, old, new, kwargs):
        try:
            raw_value = float(new)
        except (ValueError, TypeError):
            return

        power = max(raw_value, 0.0)

        if power <= self.green_threshold:
            if self.green_stable_since is None:
                self.green_stable_since = datetime.now()
        else:
            self.green_stable_since = None

        old_zone = self.current_zone
        new_zone = self._classify_zone(power)

        if new_zone != old_zone:
            pct = self._calc_excess_percent(power)
            self.log(
                f"ZONA: {old_zone.value} -> {new_zone.value} "
                f"(rete: {power:.0f}W, supero: {pct:.0f}%)"
            )

            if old_zone == PowerZone.YELLOW and new_zone == PowerZone.RED:
                self.came_from_yellow = True
            else:
                self.came_from_yellow = False

            self.current_zone = new_zone
            self.zone_entry_time = datetime.now()
            self._on_zone_change(old_zone, new_zone, power)

        if new_zone == PowerZone.RED and not self.shed_active:
            self._red_zone_shed(power)

        self._publish_state()

    def _classify_zone(self, power):
        if self.current_zone == PowerZone.GREEN:
            if power >= self.red_threshold:
                return PowerZone.RED
            elif power >= self.available_power:
                return PowerZone.YELLOW
            return PowerZone.GREEN
        elif self.current_zone == PowerZone.YELLOW:
            if power >= self.red_threshold:
                return PowerZone.RED
            elif power <= self.green_threshold:
                return PowerZone.GREEN
            return PowerZone.YELLOW
        elif self.current_zone == PowerZone.RED:
            if power <= self.green_threshold:
                return PowerZone.GREEN
            elif power < self.red_threshold:
                return PowerZone.YELLOW
            return PowerZone.RED

    def _on_zone_change(self, old_zone, new_zone, power):
        if new_zone == PowerZone.RED:
            self._cancel_yellow_timers()
            self._cancel_restore()
            self._stop_realtime_timer()
            self._red_zone_shed(power)
            self._start_ha_timer_red()
            self._start_realtime_timer()

        elif new_zone == PowerZone.YELLOW:
            self._cancel_restore()
            self._stop_realtime_timer()
            self._start_yellow_management(power)
            self._start_ha_timer_yellow()
            self._start_realtime_timer()

        elif new_zone == PowerZone.GREEN:
            self._cancel_yellow_timers()
            self._stop_ha_timer()
            self._stop_realtime_timer()
            self.current_check = None
            self.came_from_yellow = False
            if self.shed_active:
                self._schedule_restore()

    # =====================================================================
    # HA TIMER
    # =====================================================================

    def _start_ha_timer_yellow(self):
        try:
            self.call_service(
                "timer/start",
                entity_id="timer.pm_distacco_countdown",
                duration="03:02:00"
            )
        except Exception as e:
            self.log(f"Timer start: {e}", level="WARNING")

    def _start_ha_timer_red(self):
        if self.came_from_yellow:
            duration = "00:04:00"
            self.log("GIALLA->ROSSA: 4 minuti al distacco!")
        else:
            duration = "00:02:00"
            self.log("Ingresso diretto ROSSA: 2 minuti al distacco!")
        try:
            self.call_service(
                "timer/start",
                entity_id="timer.pm_distacco_countdown",
                duration=duration
            )
        except Exception as e:
            self.log(f"Timer start: {e}", level="WARNING")

    def _stop_ha_timer(self):
        try:
            self.call_service(
                "timer/cancel",
                entity_id="timer.pm_distacco_countdown"
            )
        except Exception:
            pass

    def _start_realtime_timer(self):
        self._stop_realtime_timer()
        self.realtime_timer = self.run_every(
            self._update_elapsed, "now", 10
        )

    def _stop_realtime_timer(self):
        if self.realtime_timer is not None:
            try:
                self.cancel_timer(self.realtime_timer)
            except Exception:
                pass
            self.realtime_timer = None

    def _update_elapsed(self, kwargs):
        if self.zone_entry_time is None:
            return
        elapsed = (datetime.now() - self.zone_entry_time).total_seconds()
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        self.set_state(
            "sensor.pm_elapsed_time",
            state=f"{minutes:02d}:{seconds:02d}",
            attributes={
                "friendly_name": "PM Tempo in zona",
                "icon": "mdi:timer-outline",
                "zone": self.current_zone.value,
                "elapsed_seconds": int(elapsed),
                "elapsed_minutes": round(elapsed / 60, 1),
            }
        )

    # =====================================================================
    # LETTURA POTENZA
    # =====================================================================

    def _get_grid_power(self):
        if self.test_mode and self.entity_exists("input_number.pm_test_power"):
            try:
                return float(self.get_state("input_number.pm_test_power"))
            except (ValueError, TypeError):
                pass
        try:
            raw = float(self.get_state(self.power_sensor))
        except (ValueError, TypeError):
            self.log("Sensore rete non leggibile!", level="WARNING")
            return 0.0
        return max(raw, 0.0)

    def _get_device_power(self, device):
        if device.power_sensor:
            try:
                raw = float(self.get_state(device.power_sensor))
                if raw < 0:
                    return 0.0
                return raw  # include 0.0 — NON cadere nel fallback!
            except (ValueError, TypeError):
                pass
        if device.controllable and self._is_device_on(device):
            return device.estimated_power
        return 0.0

    def _get_non_controllable_power(self):
        result = {}
        for d in self.non_controllable:
            pw = self._get_device_power(d)
            if pw > 50:
                result[d.name] = pw
        return result

    # =====================================================================
    # OPERAZIONI DISPOSITIVI
    # =====================================================================

    def _is_device_on(self, device):
        if not device.entity_id:
            return False
        state = self.get_state(device.entity_id)
        if state is None:
            return False
        s = state.lower()
        if device.inverted:
            return s in ("off",)
        return s in ("on", "heat", "cool", "auto", "heat_cool",
                      "fan_only", "dry", "performance", "eco", "electric")

    def _shed_device(self, device):
        if not device.enabled or not device.controllable:
            return
        # v6: salva consumo reale
        device.last_known_power = self._get_device_power(device)
        device.pre_shed_state = self.get_state(device.entity_id)
        device.shed_time = datetime.now()
        device.state = DeviceState.SHED

        # v6: avvia timer timeout massimo
        self._start_max_shed_timer(device)

        if self.dry_run:
            self.log(f"  DRY RUN: spegnerei {device.name} "
                     f"({device.last_known_power:.0f}W)")
            return

        if device.inverted:
            self.call_service(
                f"{device.domain}/turn_on", entity_id=device.entity_id
            )
        elif device.turn_off_service:
            svc = device.turn_off_service["service"]
            data = device.turn_off_service.get("data", {})
            self.call_service(svc, **data)
        else:
            self.call_service(
                f"{device.domain}/turn_off", entity_id=device.entity_id
            )
        self.log(f"  SPENTO: {device.name} ({device.last_known_power:.0f}W)")

    def _restore_device(self, device):
        self._cancel_max_shed_timer(device)

        if self.dry_run:
            self.log(f"  DRY RUN: riaccenderei {device.name}")
            device.state = DeviceState.ON_BY_USER
            device.shed_time = None
            return

        if device.inverted:
            self.call_service(
                f"{device.domain}/turn_off", entity_id=device.entity_id
            )
        elif device.domain == "climate":
            mode = self._get_climate_restore_mode()
            self.call_service(
                "climate/set_hvac_mode",
                entity_id=device.entity_id,
                hvac_mode=mode,
            )
        elif device.turn_on_service:
            svc = device.turn_on_service["service"]
            data = device.turn_on_service.get("data", {})
            self.call_service(svc, **data)
        else:
            self.call_service(
                f"{device.domain}/turn_on", entity_id=device.entity_id
            )
        device.state = DeviceState.ON_BY_USER
        device.shed_time = None
        self.log(f"  RIACCESO: {device.name}")

    def _get_climate_restore_mode(self):
        """Legge il modo di ripristino per dispositivi climate."""
        helper = "input_select.pm_altherma_restore_mode"
        if self.entity_exists(helper):
            mode = self.get_state(helper)
            if mode and mode not in ("unknown", "unavailable"):
                return mode
        month = datetime.now().month
        return "heat" if month in (11, 12, 1, 2, 3, 4) else "cool"

    def _sync_device_states(self):
        for d in self.devices:
            if self._is_device_on(d):
                d.state = DeviceState.ON_BY_USER
            else:
                d.state = DeviceState.OFF_BY_USER

    # =====================================================================
    # v6: TIMEOUT MASSIMO SHED
    # =====================================================================

    def _start_max_shed_timer(self, device):
        max_time = self._get_max_shed_time()
        timer_handle = self.run_in(
            self._on_max_shed_timeout, max_time,
            device_name=device.name
        )
        self.max_shed_timers[device.name] = timer_handle
        self.log(f"  Timeout shed {device.name}: {max_time / 60:.0f} min")

    def _cancel_max_shed_timer(self, device):
        timer = self.max_shed_timers.pop(device.name, None)
        if timer is not None:
            try:
                self.cancel_timer(timer)
            except Exception:
                pass

    def _cancel_all_max_shed_timers(self):
        for name, timer in list(self.max_shed_timers.items()):
            try:
                self.cancel_timer(timer)
            except Exception:
                pass
        self.max_shed_timers.clear()

    def _on_max_shed_timeout(self, kwargs):
        device_name = kwargs.get("device_name")
        device = None
        for d in self.devices:
            if d.name == device_name:
                device = d
                break
        if device is None or device.state != DeviceState.SHED:
            return

        max_min = self._get_max_shed_time() / 60
        self.log(f"TIMEOUT {max_min:.0f} min per {device.name}! "
                 f"Riaccensione forzata.")

        self._restore_device(device)
        self.max_shed_timers.pop(device.name, None)

        self._notify_telegram(
            f"*Power Manager:* ⏰ Timeout {max_min:.0f} min raggiunto!\n"
            f"Riacceso forzatamente: *{device.name}*\n"
            f"Controlla la situazione."
        )
        self._notify_alexa(
            f"Attenzione, {device.name} era spento da "
            f"{max_min:.0f} minuti. L'ho riacceso. "
            f"Controlla la situazione."
        )

        remaining_shed = [d for d in self.devices
                          if d.state == DeviceState.SHED]
        if not remaining_shed:
            self.shed_active = False
            self.restore_in_progress = False
        self._publish_state()

    # =====================================================================
    # LUNA2000: GESTIONE CARICA BATTERIA (PRIORITA 0)
    # =====================================================================
    # Logica: PRIMA di spegnere qualsiasi device,
    # ridurre/fermare la carica Luna2000 se attiva.
    # Entities:
    #   Configurabili in apps.yaml:
    #   luna_charge_switch, luna_power_slider,
    #   luna_power_sensor, luna_power_step
    
    # =====================================================================

    def _luna_is_charging(self):
        """Verifica se la carica forzata Luna2000 è attiva."""
        if not self.entity_exists(self.luna_switch):
            return False
        return self.get_state(
            self.luna_switch) == "on"

    def _luna_get_power(self):
        """
        Legge la potenza di carica REALE istantanea (W).
        Usa sensor.battery_power_dashboard: negativo = sta caricando.
        Ritorna il valore assoluto (positivo) se sta caricando, 0 altrimenti.
        """
        if not self.entity_exists(self.luna_power_sensor):
            return 0.0
        try:
            raw = float(self.get_state(self.luna_power_sensor))
            if raw < 0:
                return abs(raw)  # caricando: -1616 → 1616
            return 0.0  # scaricando o fermo: non conta
        except (ValueError, TypeError):
            return 0.0

    def _luna_get_configured_power(self):
        """
        Legge la potenza di carica CONFIGURATA dallo slider (W).
        Usata per save/restore del valore impostato dall'utente.
        """
        if not self.entity_exists(self.luna_power_slider):
            return 0.0
        try:
            return float(self.get_state(self.luna_power_slider))
        except (ValueError, TypeError):
            return 0.0

    def _luna_try_reduce(self, excess_watts):
        """
        Tenta di ridurre/fermare la carica Luna2000 per coprire
        l'eccesso. Ritorna i Watt effettivamente ridotti.

        Logica:
        1. Se carica non attiva (switch OFF) → return 0 (skip immediato)
        2. Leggi potenza reale da sensor.battery_power_dashboard
        3. Se potenza reale >= eccesso → riduci slider abbastanza
        4. Se potenza reale < eccesso → ferma tutto
        """
        if not self._luna_is_charging():
            return 0.0

        actual_power = self._luna_get_power()
        if actual_power <= 0:
            return 0.0

        # Salva stato pre-shed (solo la prima volta)
        if not self.luna_reduced:
            self.luna_was_charging = True
            self.luna_pre_shed_power = self._luna_get_configured_power()
            self.luna_reduced = True

        configured = self._luna_get_configured_power()

        self.log(f"  LUNA2000: carica reale {actual_power:.0f}W, "
                 f"configurata {configured:.0f}W, "
                 f"eccesso {excess_watts:.0f}W")

        if actual_power >= excess_watts:
            # Basta ridurre la potenza
            new_power = configured - excess_watts
            # Arrotonda a step di 100W (arrotonda per difetto = più sicuro)
            new_power = int(new_power / self.luna_power_step) * self.luna_power_step
            new_power = max(new_power, 0)
            reduced = actual_power if new_power == 0 else excess_watts

            if new_power > 0:
                self._luna_set_power(new_power)
                self.log(f"  LUNA2000: slider ridotto "
                         f"{configured:.0f}W → {new_power:.0f}W "
                         f"(liberati ~{reduced:.0f}W)")
                self._notify_telegram(
                    f"*Power Manager:* 🔋 Luna2000 carica ridotta\n"
                    f"Reale: {actual_power:.0f}W → "
                    f"slider: {configured:.0f}W → {new_power:.0f}W\n"
                    f"Liberati ~{reduced:.0f}W")
            else:
                # Riduzione a 0 = ferma
                self._luna_stop_charging()
                reduced = actual_power
                self.log(f"  LUNA2000: carica fermata "
                         f"(reale {actual_power:.0f}W → OFF)")
                self._notify_telegram(
                    f"*Power Manager:* 🔋 Luna2000 carica fermata\n"
                    f"Assorbiva {actual_power:.0f}W")
            return reduced
        else:
            # Non basta ridurre, ferma tutto
            self._luna_stop_charging()
            self.log(f"  LUNA2000: carica fermata "
                     f"(reale {actual_power:.0f}W → OFF, "
                     f"servivano {excess_watts:.0f}W)")
            self._notify_telegram(
                f"*Power Manager:* 🔋 Luna2000 carica fermata\n"
                f"Assorbiva {actual_power:.0f}W, "
                f"servono ancora {excess_watts - actual_power:.0f}W")
            return actual_power

    def _luna_set_power(self, watts):
        """Imposta la potenza di carica Luna2000."""
        if self.dry_run:
            self.log(f"  DRY RUN: imposterei Luna2000 a {watts:.0f}W")
            return
        try:
            self.call_service(
                "input_number/set_value",
                entity_id=self.luna_power_slider,
                value=watts
            )
        except Exception as e:
            self.log(f"Luna2000 set_power: {e}", level="WARNING")

    def _luna_stop_charging(self):
        """Ferma la carica forzata Luna2000."""
        if self.dry_run:
            self.log("  DRY RUN: fermerei carica Luna2000")
            return
        try:
            self.call_service(
                "input_boolean/turn_off",
                entity_id=self.luna_switch
            )
        except Exception as e:
            self.log(f"Luna2000 stop: {e}", level="WARNING")

    def _luna_restore(self):
        """
        Ripristina la carica Luna2000 alla fine del restore.
        NON ripristina il valore originale ma calcola la potenza
        disponibile al netto dei carichi già riaccesi, per evitare
        di causare un nuovo supero (ping-pong).

        Logica:
        1. Calcola margine = green_threshold - potenza_rete_attuale
        2. Se margine <= 200W → non riattivare (troppo poco)
        3. Se margine < valore_originale → imposta al margine
        4. Se margine >= valore_originale → ripristina originale
        Arrotonda per difetto a step 100W per sicurezza.
        """
        if not self.luna_reduced or not self.luna_was_charging:
            return

        current_power = self._get_grid_power()
        margin = self.green_threshold - current_power
        # Arrotonda per difetto a step 100W
        margin = int(margin / self.luna_power_step) * self.luna_power_step

        if self.dry_run:
            restore_pw = min(self.luna_pre_shed_power, margin)
            self.log(f"  DRY RUN: Luna2000 margine={margin:.0f}W, "
                     f"ripristinerei a {restore_pw:.0f}W "
                     f"(originale: {self.luna_pre_shed_power:.0f}W)")
            self.luna_reduced = False
            self.luna_was_charging = False
            return

        if margin <= 200:
            # Troppo poco margine, non riattivare
            self.log(f"  LUNA2000: margine solo {margin:.0f}W, "
                     f"carica NON ripristinata (min 200W)")
            self._notify_telegram(
                f"*Power Manager:* 🔋 Luna2000 carica NON ripristinata\n"
                f"Margine disponibile: {margin:.0f}W (troppo poco)\n"
                f"Riattiva manualmente quando possibile.")
            self.luna_reduced = False
            self.luna_was_charging = False
            self.luna_pre_shed_power = 0.0
            return

        # Calcola potenza di restore: minimo tra originale e margine
        restore_power = min(self.luna_pre_shed_power, margin)
        restore_power = max(int(restore_power / self.luna_power_step) * self.luna_power_step, self.luna_power_step)

        try:
            # Prima imposta la potenza (PRIMA di accendere lo switch)
            self.call_service(
                "input_number/set_value",
                entity_id=self.luna_power_slider,
                value=restore_power
            )
            # Poi riattiva la carica
            self.call_service(
                "input_boolean/turn_on",
                entity_id=self.luna_switch
            )

            if restore_power < self.luna_pre_shed_power:
                self.log(f"  LUNA2000: carica ripristinata a "
                         f"{restore_power:.0f}W (ridotta da "
                         f"{self.luna_pre_shed_power:.0f}W, "
                         f"margine {margin:.0f}W)")
                self._notify_telegram(
                    f"*Power Manager:* 🔋 Luna2000 carica ripristinata\n"
                    f"Potenza: {restore_power:.0f}W "
                    f"(era {self.luna_pre_shed_power:.0f}W)\n"
                    f"Ridotta per margine disponibile: {margin:.0f}W")
            else:
                self.log(f"  LUNA2000: carica ripristinata a "
                         f"{restore_power:.0f}W (originale)")
                self._notify_telegram(
                    f"*Power Manager:* 🔋 Luna2000 carica ripristinata\n"
                    f"Potenza: {restore_power:.0f}W")
        except Exception as e:
            self.log(f"Luna2000 restore: {e}", level="WARNING")

        self.luna_reduced = False
        self.luna_was_charging = False
        self.luna_pre_shed_power = 0.0

    # =====================================================================
    # SMART SHED v6
    # =====================================================================

    def _smart_shed(self, excess_watts, include_all=False):
        if excess_watts <= 0:
            return []

        # ─── PRIORITA 0: Luna2000 ───
        # Prima di toccare qualsiasi device, ridurre/fermare
        # la carica batteria se attiva
        luna_reduced = self._luna_try_reduce(excess_watts)
        excess_watts -= luna_reduced
        if excess_watts <= 0:
            self.shed_active = True
            self.shed_cycle_count += 1
            self.log(f"  Luna2000 sufficiente! "
                     f"Ridotti {luna_reduced:.0f}W "
                     f"[ciclo #{self.shed_cycle_count}]")
            return [f"Luna2000 (-{luna_reduced:.0f}W)"]

        # ─── PRIORITA 1-6: Device normali ───
        luna_name = (f"Luna2000 (-{luna_reduced:.0f}W)"
                     if luna_reduced > 0 else None)

        min_active = self._get_min_active_power()
        candidates = []

        for d in sorted(self.devices, key=lambda x: x.priority):
            if not d.enabled or d.state == DeviceState.SHED:
                continue
            if not include_all and not d.shed_in_yellow:
                continue
            if not self._is_device_on(d):
                d.state = DeviceState.OFF_BY_USER
                continue
            pw = self._get_device_power(d)
            # v6: skip se sotto soglia minima
            if pw < min_active:
                self.log(f"  Skip {d.name}: {pw:.0f}W "
                         f"< soglia {min_active:.0f}W")
                continue
            candidates.append((d, pw))

        if not candidates:
            self.log(f"  Nessun dispositivo attivo da spegnere "
                     f"(soglia {min_active:.0f}W)")
            self._notify_telegram(
                f"*Power Manager:* ⚠️ Nessun dispositivo attivo "
                f"sopra {min_active:.0f}W da spegnere!\n"
                f"Eccesso residuo: {excess_watts:.0f}W - "
                f"Intervento manuale necessario."
            )
            return [luna_name] if luna_name else []

        # Un solo device basta?
        for d, pw in candidates:
            if pw >= excess_watts:
                self._shed_device(d)
                self.shed_active = True
                self.shed_cycle_count += 1
                self.log(
                    f"  Basta {d.name} ({pw:.0f}W) "
                    f"per eccesso {excess_watts:.0f}W "
                    f"[ciclo #{self.shed_cycle_count}]"
                )
                names = [d.name]
                if luna_name:
                    names.insert(0, luna_name)
                return names

        # Shed progressivo
        reduced = 0.0
        shed_names = []
        if luna_name:
            shed_names.append(luna_name)
        for d, pw in candidates:
            if reduced >= excess_watts:
                break
            self._shed_device(d)
            reduced += pw
            shed_names.append(d.name)
            self.shed_active = True

        self.shed_cycle_count += 1
        self.log(
            f"  Spenti {len(shed_names)}: "
            f"ridotti ~{reduced:.0f}W (eccesso: {excess_watts:.0f}W) "
            f"[ciclo #{self.shed_cycle_count}]"
        )
        return shed_names

    def _force_shed_all(self, shed_names):
        # Ferma Luna2000 se ancora attiva
        if self._luna_is_charging():
            luna_pw = self._luna_get_power()  # reale dal sensore
            if luna_pw > 0:
                if not self.luna_reduced:
                    self.luna_was_charging = True
                    self.luna_pre_shed_power = self._luna_get_configured_power()
                    self.luna_reduced = True
                self._luna_stop_charging()
                if "Luna2000" not in str(shed_names):
                    shed_names.append(f"Luna2000 (-{luna_pw:.0f}W)")
                self.log(f"  FORCE: Luna2000 fermata "
                         f"(reale {luna_pw:.0f}W)")

        min_active = self._get_min_active_power()
        for d in sorted(self.devices, key=lambda x: x.priority):
            if d.enabled and d.state != DeviceState.SHED:
                if self._is_device_on(d):
                    pw = self._get_device_power(d)
                    if pw < min_active:
                        self.log(f"  Skip force {d.name}: "
                                 f"{pw:.0f}W < {min_active:.0f}W")
                        continue
                    self._shed_device(d)
                    if d.name not in shed_names:
                        shed_names.append(d.name)
                    self.shed_active = True
        return shed_names

    # =====================================================================
    # ZONA GIALLA
    # =====================================================================

    def _start_yellow_management(self, power):
        pct = self._calc_excess_percent(power)
        self.current_check = "1"
        self.log(f"1 CHECK - Rete: {power:.0f}W, supero: {pct:.0f}%. "
                 f"Nessun intervento.")

        self.yellow_check2_timer = self.run_in(
            self._yellow_check2_callback, 180)
        self.yellow_check3_timer = self.run_in(
            self._yellow_check3_callback, 3600)
        self.yellow_check4_timer = self.run_in(
            self._yellow_check4_callback, 9000)
        self._publish_state()

    def _yellow_check2_callback(self, kwargs):
        if self.current_zone != PowerZone.YELLOW:
            return
        power = self._get_grid_power()
        pct = self._calc_excess_percent(power)
        self.current_check = "2"
        self.log(f"2 CHECK - Rete: {power:.0f}W, supero: {pct:.0f}%. "
                 f"Nessun intervento.")
        self._publish_state()

    def _yellow_check3_callback(self, kwargs):
        if self.current_zone != PowerZone.YELLOW:
            return
        power = self._get_grid_power()
        pct = self._calc_excess_percent(power)
        self.current_check = "3"
        self.log(f"3 CHECK - Rete: {power:.0f}W, supero: {pct:.0f}%")

        if power <= self.shed_target:
            self.log("  Gia sotto target, nessun shed.")
            self._publish_state()
            return

        excess = power - self.shed_target
        shed_names = self._smart_shed(excess, include_all=False)
        nc_active = self._get_non_controllable_power()

        if shed_names:
            lista = ", ".join(shed_names)
            self._notify_alexa(
                f"Attenzione, supero potenza del {pct:.0f} percento "
                f"da un'ora. Ho spento: {lista}."
                + (f" Valuta di spegnere anche "
                   f"{', '.join(nc_active.keys())}!"
                   if nc_active else "")
            )
        self._send_check_telegram(
            "3", "Rischio distacco", pct, power, shed_names, nc_active)

        self._schedule_yellow_recheck()
        self._publish_state()

    def _yellow_recheck_callback(self, kwargs):
        if self.current_zone != PowerZone.YELLOW:
            self.yellow_recheck_timer = None
            return

        power = self._get_grid_power()
        excess = power - self.shed_target
        if excess <= 0:
            self.log("  Recheck: rientrato!")
            self.yellow_recheck_timer = None
            return

        pct = self._calc_excess_percent(power)
        self.log(f"  Recheck: eccesso {excess:.0f}W")

        shed_names = self._smart_shed(excess, include_all=False)
        if shed_names:
            lista = ", ".join(shed_names)
            self._notify_alexa(
                f"Ancora in supero potenza. Ho spento anche: {lista}.")
            self._notify_telegram(
                f"*Power Manager:* 🟡 Recheck Zona Gialla\n"
                f"Supero: {pct:.0f}% - Ho spento: {lista}")
        self._schedule_yellow_recheck()

    def _schedule_yellow_recheck(self):
        if self.yellow_recheck_timer is not None:
            try:
                self.cancel_timer(self.yellow_recheck_timer)
            except Exception:
                pass
        self.yellow_recheck_timer = self.run_in(
            self._yellow_recheck_callback, 300)

    def _yellow_check4_callback(self, kwargs):
        if self.current_zone != PowerZone.YELLOW:
            return
        power = self._get_grid_power()
        pct = self._calc_excess_percent(power)
        self.current_check = "4"

        if power <= self.shed_target:
            self.log("  4 check: gia sotto target.")
            self._publish_state()
            return

        self.log(f"4 CHECK SAFETY NET! Rete: {power:.0f}W")

        excess = power - self.shed_target
        shed_names = self._smart_shed(excess, include_all=True)
        if self._get_grid_power() > self.shed_target:
            shed_names = self._force_shed_all(shed_names)

        nc_active = self._get_non_controllable_power()

        msg = (f"Attenzione critica! Distacco tra 30 minuti! "
               f"Supero del {pct:.0f} percento. ")
        if shed_names:
            msg += f"Ho spento: {', '.join(shed_names)}. "
        if nc_active:
            msg += f"Spegni subito {', '.join(nc_active.keys())}!"
        self._notify_alexa(msg)

        self._send_check_telegram(
            "4", "DISTACCO IMMINENTE", pct, power, shed_names, nc_active)
        self._publish_state()

    # =====================================================================
    # ZONA ROSSA
    # =====================================================================

    def _red_zone_shed(self, power):
        pct = self._calc_excess_percent(power)
        self.current_check = "ROSSO"

        if self.came_from_yellow:
            time_str = "4 minuti (da gialla)"
        else:
            time_str = "2 minuti"

        self.log(f"ZONA ROSSA! Rete: {power:.0f}W, supero: {pct:.0f}%. "
                 f"Distacco in {time_str}!")

        excess = power - self.shed_target
        shed_names = self._smart_shed(excess, include_all=True)

        if self._get_grid_power() > self.shed_target:
            shed_names = self._force_shed_all(shed_names)

        nc_active = self._get_non_controllable_power()

        msg = (f"Attenzione! Emergenza supero potenza del {pct:.0f} percento! "
               f"Distacco tra {time_str}! ")
        if shed_names:
            msg += f"Ho spento: {', '.join(shed_names)}. "
        if nc_active:
            msg += f"Spegni subito {', '.join(nc_active.keys())}!"
        self._notify_alexa(msg)

        header = f"Distacco in {time_str}!"
        self._send_check_telegram(
            "ROSSO", header, pct, power, shed_names, nc_active)
        self._publish_state()

    # =====================================================================
    # RESTORE v6: SMART SEQUENZIALE CON VERIFICA
    # =====================================================================

    def _schedule_restore(self):
        if self.restore_in_progress:
            return
        delay = self.stable_minutes_before_restore * 60
        self.log(f"Verde. Attendo {self.stable_minutes_before_restore} min "
                 f"stabili prima del restore.")
        self.restore_timer = self.run_in(
            self._check_stability_then_restore, delay)
        self.restore_in_progress = True

    def _check_stability_then_restore(self, kwargs):
        if self.current_zone != PowerZone.GREEN:
            self.restore_in_progress = False
            return

        if self.green_stable_since is None:
            self.restore_timer = self.run_in(
                self._check_stability_then_restore, 60)
            return

        stable = (datetime.now() - self.green_stable_since).total_seconds()
        required = self.stable_minutes_before_restore * 60

        if stable < required:
            self.restore_timer = self.run_in(
                self._check_stability_then_restore,
                min(required - stable + 5, 60))
            return

        self.log(f"Stabile da {stable:.0f}s. Avvio smart restore.")
        self._build_restore_queue()
        self._restore_next_in_queue({})

    def _build_restore_queue(self):
        """
        v6: Coda di restore intelligente.
        Priorita inversa (P6->P1), ma PRIMA quelli che
        rientrano nel margine disponibile.
        """
        shed_devices = [
            d for d in sorted(self.devices, key=lambda x: -x.priority)
            if d.state == DeviceState.SHED and d.auto_restore and d.enabled
        ]

        current_power = self._get_grid_power()
        margin = self.green_threshold - current_power

        fits = []
        exceeds = []
        for d in shed_devices:
            if d.last_known_power <= margin:
                fits.append(d)
                margin -= d.last_known_power
            else:
                exceeds.append(d)

        self.restore_queue = fits + exceeds

        if self.restore_queue:
            names_fits = [f"{d.name}({d.last_known_power:.0f}W)"
                          for d in fits]
            names_exc = [f"{d.name}({d.last_known_power:.0f}W)"
                         for d in exceeds]
            self.log(f"  Coda restore: "
                     f"rientrano=[{', '.join(names_fits)}] "
                     f"eccedono=[{', '.join(names_exc)}]")

    def _restore_next_in_queue(self, kwargs):
        """v6: Riaccendi prossimo device con verifica potenza."""
        if self.current_zone != PowerZone.GREEN:
            self.log("  Restore interrotto: non piu in zona verde.")
            self.restore_in_progress = False
            self.restore_queue = []
            return

        if not self.restore_queue:
            self._on_restore_complete()
            return

        device = self.restore_queue[0]

        # Rispetta durata minima shed
        if device.shed_time:
            shed_secs = (datetime.now() - device.shed_time).total_seconds()
            if shed_secs < self.min_shed_duration:
                wait = self.min_shed_duration - shed_secs + 5
                self.log(f"  {device.name}: shed da {shed_secs:.0f}s, "
                         f"min={self.min_shed_duration}s. "
                         f"Attendo {wait:.0f}s.")
                self.restore_timer = self.run_in(
                    self._restore_next_in_queue, wait)
                return

        # Verifica margine PRIMA di riaccendere
        current_power = self._get_grid_power()
        projected = current_power + device.last_known_power

        if projected >= self.available_power:
            restore_int = self._get_restore_interval()
            backoff = restore_int * max(self.shed_cycle_count, 1)
            self.log(f"  {device.name}: proiezione {projected:.0f}W "
                     f">= {self.available_power:.0f}W. "
                     f"Riprovo tra {backoff:.0f}s "
                     f"(backoff x{max(self.shed_cycle_count, 1)})")
            self.restore_timer = self.run_in(
                self._restore_next_in_queue, backoff)
            return

        # Riaccendi
        self.last_restored_device = device
        self.restore_queue.pop(0)
        self._restore_device(device)

        if device.needs_manual_restart:
            self._notify_alexa(
                f"Ho riacceso la presa di {device.name} ma ricordati "
                f"di riavviare il programma manualmente.")

        self._notify_telegram(
            f"*Power Manager:* 🔺 Riacceso *{device.name}*\n"
            f"Rete: {current_power:.0f}W -> "
            f"proiezione ~{projected:.0f}W\n"
            f"Rimangono spenti: {len(self.restore_queue)}")

        # Programma verifica a meta intervallo
        restore_int = self._get_restore_interval()
        half_interval = restore_int / 2
        self.log(f"  Verifica tra {half_interval:.0f}s")
        self.restore_check_timer = self.run_in(
            self._on_restore_verify, half_interval)

    def _on_restore_verify(self, kwargs):
        """v6: Verifica potenza dopo riaccensione."""
        self.restore_check_timer = None
        power = self._get_grid_power()
        zone = self._classify_zone(power)
        device = self.last_restored_device

        self.log(f"  Verifica post-restore: rete={power:.0f}W, "
                 f"zona={zone.value}")

        if zone == PowerZone.GREEN and power <= self.green_threshold:
            # OK! Aspetta fine intervallo poi prossimo
            restore_int = self._get_restore_interval()
            remaining = restore_int / 2
            if self.restore_queue:
                self.log(f"  OK. Prossimo restore tra {remaining:.0f}s")
                self.restore_timer = self.run_in(
                    self._restore_next_in_queue, remaining)
            else:
                self._on_restore_complete()

        elif zone == PowerZone.RED:
            # CRITICO: ri-spegni l'ultimo riacceso
            if device and device.state == DeviceState.ON_BY_USER:
                self.log(f"  ROSSA! Ri-spengo {device.name}")
                self._shed_device(device)
                self._notify_telegram(
                    f"*Power Manager:* 🔴 Risupero dopo restore!\n"
                    f"Ri-spento: *{device.name}*\n"
                    f"Rete: {power:.0f}W")
            self.restore_in_progress = False
            self.restore_queue = []

        elif zone == PowerZone.YELLOW:
            # Ri-spegni l'ultimo, logica gialla parte da sola
            if device and device.state == DeviceState.ON_BY_USER:
                self.log(f"  GIALLA! Ri-spengo {device.name}")
                self._shed_device(device)
                self._notify_telegram(
                    f"*Power Manager:* 🟡 Risupero dopo restore!\n"
                    f"Ri-spento: *{device.name}*\n"
                    f"Rete: {power:.0f}W - Avvio check gialli.")
            self.restore_in_progress = False
            self.restore_queue = []

        else:
            # Tra verde e gialla: STOP, troppo rischioso
            remaining_names = ', '.join(d.name for d in self.restore_queue)
            restore_int = self._get_restore_interval()
            backoff = restore_int * max(self.shed_cycle_count, 1)
            self.log(f"  Rete {power:.0f}W tra verde e gialla. "
                     f"STOP restore, riprovo tra {backoff:.0f}s.")
            self._notify_telegram(
                f"*Power Manager:* ⚠️ Restore in pausa\n"
                f"Rete: {power:.0f}W - troppo vicino alla soglia.\n"
                f"Rimangono spenti: {remaining_names}\n"
                f"Riprovo tra {backoff:.0f}s")
            self.restore_timer = self.run_in(
                self._restore_next_in_queue, backoff)

    def _on_restore_complete(self):
        self.shed_active = False
        self.restore_in_progress = False
        self.current_check = None
        self.restore_queue = []
        self.shed_cycle_count = 0
        self._cancel_all_max_shed_timers()

        # Ripristina Luna2000 per ultima (dopo tutti i device)
        self._luna_restore()

        manual = [d for d in self.devices
                  if d.state == DeviceState.SHED
                  and (d.needs_manual_restart or not d.auto_restore)]

        if manual:
            names = ", ".join(d.name for d in manual)
            self._notify_alexa(
                f"Tutti riaccesi. Riavvia manualmente: {names}.")
            self._notify_telegram(
                f"*Power Manager:* ✅ Restore completato\n"
                f"Riavvia: {names}")
        else:
            self._notify_alexa(
                "Ho riacceso tutti gli elettrodomestici.")
            self._notify_telegram(
                "*Power Manager:* ✅ Restore completato - Tutti riaccesi")

        for d in self.devices:
            if d.state == DeviceState.SHED:
                d.state = DeviceState.UNKNOWN

        self.log("Restore completato!")
        self._publish_state()

    # =====================================================================
    # NOTIFICHE
    # =====================================================================

    def _notify_alexa(self, message):
        """Alexa: DND SEMPRE rispettato. Solo Telegram bypassa DND."""
        if self._is_dnd_active():
            self.log(f"  DND: {message[:60]}...")
            return
        try:
            self.call_service(
                self.alexa_notify_service,
                message=message,
                data={"type": "announce"},
            )
        except Exception as e:
            self.log(f"Alexa: {e}", level="WARNING")

    def _notify_telegram(self, message):
        if not self.telegram_bot_token:
            self.log("Telegram: bot token non configurato!", level="WARNING")
            return
        try:
            url = (f"https://api.telegram.org/"
                   f"bot{self.telegram_bot_token}/sendMessage")
            payload = json_module.dumps({
                "chat_id": self.telegram_chat_id,
                "text": message,
                "parse_mode": "Markdown",
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"})
            response = urllib.request.urlopen(req, timeout=10)
            if response.getcode() == 200:
                self.log(f"  TG: {message[:60]}...")
            else:
                self.log(f"TG HTTP {response.getcode()}", level="WARNING")
        except Exception as e:
            self.log(f"TG errore: {e}", level="WARNING")

    def _send_check_telegram(self, check_num, header, pct, power,
                              shed_names, nc_active):
        zone = ("🔴 ZONA ROSSA" if check_num == "ROSSO"
                else "🟡 Zona Gialla")
        msg = (f"*Power Manager:* {zone}\n"
               f"*{check_num} check - {header}*\n"
               f"Supero potenza: *{pct:.0f}%*\n"
               f"Rete: {power:.0f}W / "
               f"Contratto: {self.contract_power:.0f}W\n")
        if shed_names:
            msg += f"🔻 Spenti: {', '.join(shed_names)}\n"
        if nc_active:
            for name, pw in nc_active.items():
                msg += f"⚠️ {name}: {pw:.0f}W (non controllabile)\n"
        self._notify_telegram(msg)

    def _is_dnd_active(self):
        """
        DND letto da dashboard (input_datetime).
        Fallback su valori apps.yaml se helper non esistono.
        DND si applica SOLO ad Alexa, MAI a Telegram.
        """
        now = datetime.now().time()
        periods = self._get_dnd_periods()
        for start_str, end_str in periods:
            try:
                start = datetime.strptime(start_str, "%H:%M").time()
                end = datetime.strptime(end_str, "%H:%M").time()
            except (ValueError, TypeError):
                continue
            if start <= end:
                if start <= now <= end:
                    return True
            else:
                # Attraversa mezzanotte (es. 23:00 → 08:00)
                if now >= start or now <= end:
                    return True
        return False

    def _get_dnd_periods(self):
        """Legge i 2 periodi DND da dashboard, fallback su apps.yaml."""
        periods = []
        # Periodo 1
        s1 = self._get_dnd_helper("input_datetime.pm_dnd1_start", "23:00")
        e1 = self._get_dnd_helper("input_datetime.pm_dnd1_end", "08:00")
        periods.append((s1, e1))
        # Periodo 2
        s2 = self._get_dnd_helper("input_datetime.pm_dnd2_start", "14:00")
        e2 = self._get_dnd_helper("input_datetime.pm_dnd2_end", "16:00")
        periods.append((s2, e2))
        return periods

    def _get_dnd_helper(self, entity_id, fallback):
        """Legge un input_datetime come HH:MM, con fallback."""
        if self.entity_exists(entity_id):
            val = self.get_state(entity_id)
            if val and val not in ("unknown", "unavailable"):
                # input_datetime con has_time restituisce "HH:MM:SS"
                return val[:5]  # prendi solo HH:MM
        return fallback

    # =====================================================================
    # TIMER
    # =====================================================================

    def _cancel_yellow_timers(self):
        for attr in ("yellow_check2_timer", "yellow_check3_timer",
                      "yellow_check4_timer", "yellow_recheck_timer"):
            timer = getattr(self, attr, None)
            if timer is not None:
                try:
                    self.cancel_timer(timer)
                except Exception:
                    pass
                setattr(self, attr, None)

    def _cancel_restore(self):
        if self.restore_timer is not None:
            try:
                self.cancel_timer(self.restore_timer)
            except Exception:
                pass
            self.restore_timer = None
        if self.restore_check_timer is not None:
            try:
                self.cancel_timer(self.restore_check_timer)
            except Exception:
                pass
            self.restore_check_timer = None
        self.restore_in_progress = False
        self.restore_queue = []

    # =====================================================================
    # TEST MODE
    # =====================================================================

    def _setup_test_mode(self):
        for helper, attr in [
            ("input_boolean.pm_test_mode", "test_mode"),
            ("input_boolean.pm_dry_run", "dry_run"),
        ]:
            if self.entity_exists(helper):
                self.listen_state(
                    self._on_test_toggle, helper, attr_name=attr)
                setattr(self, attr, self.get_state(helper) == "on")

        if self.test_mode and self.entity_exists("input_number.pm_test_power"):
            self.listen_state(
                self._on_test_power_change, "input_number.pm_test_power")

        if self.test_mode:
            self.log("TEST MODE ATTIVA")
        if self.dry_run:
            self.log("DRY RUN ATTIVO")

    def _on_test_toggle(self, entity, attribute, old, new, kwargs):
        attr = kwargs.get("attr_name")
        setattr(self, attr, new == "on")
        self.log(f"{attr}: {'ON' if new == 'on' else 'OFF'}")
        if attr == "test_mode" and new == "on":
            self.listen_state(
                self._on_test_power_change, "input_number.pm_test_power")

    def _on_test_power_change(self, entity, attribute, old, new, kwargs):
        if not self.test_mode:
            return
        try:
            test_val = float(new)
        except (ValueError, TypeError):
            return
        self.on_power_change(entity, "state", old, str(test_val), {})

    # =====================================================================
    # DASHBOARD LISTENERS
    # =====================================================================

    def _setup_dashboard_listeners(self):
        for device in self.devices:
            prefix = device.dashboard_prefix
            if not prefix:
                continue
            for suffix, field in [("_switch", "entity_id"),
                                  ("_power", "power_sensor")]:
                helper = f"input_text.{prefix}{suffix}"
                if self.entity_exists(helper):
                    self.listen_state(
                        self._on_dashboard_change, helper,
                        device_name=device.name, field=field)
                    val = self.get_state(helper)
                    if val and val not in ("unknown", "unavailable", ""):
                        setattr(device, field, val)

            enabled_helper = f"input_boolean.{prefix}_enabled"
            if self.entity_exists(enabled_helper):
                self.listen_state(
                    self._on_dashboard_enable_change, enabled_helper,
                    device_name=device.name)
                val = self.get_state(enabled_helper)
                device.enabled = (val == "on")

    def _on_dashboard_change(self, entity, attribute, old, new, kwargs):
        if not new or new in ("unknown", "unavailable", ""):
            return
        name = kwargs.get("device_name")
        field = kwargs.get("field")
        for d in self.devices:
            if d.name == name:
                setattr(d, field, new)
                self.log(f"{name}.{field} = {new}")
                break

    def _on_dashboard_enable_change(self, entity, attribute, old, new, kwargs):
        name = kwargs.get("device_name")
        for d in self.devices:
            if d.name == name:
                d.enabled = (new == "on")
                break

    # =====================================================================
    # PUBBLICA STATO
    # =====================================================================

    def _publish_state(self):
        shed_list = [d.name for d in self.devices
                     if d.state == DeviceState.SHED]
        nc_active = self._get_non_controllable_power()
        grid_power = self._get_grid_power()
        pct = self._calc_excess_percent(grid_power)

        zone_dur = None
        if self.zone_entry_time:
            zone_dur = (
                datetime.now() - self.zone_entry_time
            ).total_seconds() / 60

        stable_min = None
        if self.green_stable_since:
            stable_min = (
                datetime.now() - self.green_stable_since
            ).total_seconds() / 60

        device_powers = {}
        for d in self.devices:
            pw = self._get_device_power(d)
            device_powers[d.name] = {
                "power": round(pw, 1),
                "state": d.state.value,
                "enabled": d.enabled,
                "priority": d.priority,
                "last_known_power": round(d.last_known_power, 1),
            }

        restore_queue_names = [d.name for d in self.restore_queue]

        self.set_state(
            "sensor.power_manager_zone",
            state=self.current_zone.value,
            attributes={
                "friendly_name": "Power Manager - Zona",
                "icon": {
                    "green": "mdi:check-circle",
                    "yellow": "mdi:alert",
                    "red": "mdi:alert-octagon",
                }.get(self.current_zone.value, "mdi:flash"),
                "grid_power": grid_power,
                "contract_power": self.contract_power,
                "excess_percent": round(pct, 1),
                "current_check": self.current_check,
                "came_from_yellow": self.came_from_yellow,
                "available_power": self.available_power,
                "red_threshold": self.red_threshold,
                "green_threshold": self.green_threshold,
                "shed_active": self.shed_active,
                "shed_devices": shed_list,
                "non_controllable_active": nc_active,
                "zone_duration_min": (
                    round(zone_dur, 1) if zone_dur else None),
                "stable_in_green_min": (
                    round(stable_min, 1) if stable_min else None),
                "restore_in_progress": self.restore_in_progress,
                "restore_queue": restore_queue_names,
                "shed_cycle_count": self.shed_cycle_count,
                "test_mode": self.test_mode,
                "dry_run": self.dry_run,
                "device_details": device_powers,
                "luna2000_charging": self._luna_is_charging(),
                "luna2000_actual_power": self._luna_get_power(),
                "luna2000_configured_power": self._luna_get_configured_power(),
                "luna2000_reduced": self.luna_reduced,
                "luna2000_pre_shed_power": self.luna_pre_shed_power,
            },
        )
