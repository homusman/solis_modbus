"""Experimental signed battery dispatch for Solis hybrid inverters.

Firmware-dependent RC registers: queued Modbus writes are NOT verified output.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .helpers import unique_id_generator

_LOGGER = logging.getLogger(__name__)

MODE = 43135
DISCHARGE = 43129
CHARGE = 43136
RC_TIMEOUT = 43282


class SolisDispatchCoordinator:
    """Translate signed battery power into serialized remote-control writes."""

    def __init__(self, hass, controller, entry_id):
        self.hass = hass
        self.controller = controller
        self.self_use = None  # Unknown until the hardware mode is read.
        self.target_w = 0
        self.initialized = False
        self._store = Store(hass, 1, f"solis_be_dispatch_{entry_id}")
        self.last_direction = 2  # Prefer discharge mode for an initial zero setpoint.
        self.last_error = None
        self.last_request = None
        self.refresh_interval = 30
        self._lock = asyncio.Lock()
        self._unsub = None
        self.entities = []

    async def start(self):
        """Recover last staged target and the inverter's actual RC mode."""
        saved = await self._store.async_load() or {}
        saved_target = saved.get("target_w", 0)
        if isinstance(saved_target, (int, float)) and -self.max_w <= saved_target <= self.max_w:
            self.target_w = int(round(saved_target / 10)) * 10
        try:
            modes = await self.controller.async_read_holding_register(MODE, 1)
            if not modes or modes[0] not in (0, 1, 2):
                raise RuntimeError(f"Cannot identify Solis RC mode: {modes}")
            mode = modes[0]
            self.self_use = mode == 0
            if mode in (1, 2):
                self.last_direction = mode
                reg = CHARGE if mode == 1 else DISCHARGE
                power = await self.controller.async_read_holding_register(reg, 1)
                if not power:
                    raise RuntimeError("Cannot read active Solis RC power setpoint")
                self.target_w = int(power[0]) * 10 * (1 if mode == 2 else -1)
            self.initialized = True
            self.last_error = None
            self.last_request = "Recovered inverter RC state; no dispatch write on startup"
        except Exception as exc:
            self.initialized = False
            self.self_use = None
            self.last_error = str(exc)
            self.last_request = "RC state not verified on startup"
            _LOGGER.warning("Solis dispatch startup recovery failed: %s", exc)
        self.notify()
        self._unsub = async_track_time_interval(
            self.hass, self._heartbeat, timedelta(seconds=self.refresh_interval)
        )

    async def _save(self):
        await self._store.async_save({"target_w": self.target_w})

    async def stop(self):
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def notify(self):
        for entity in self.entities:
            entity.async_write_ha_state()

    async def set_self_use(self, enabled: bool):
        async with self._lock:
            if enabled:
                await self._write_off()
                self.self_use = True
            else:
                await self._write_target(self.target_w)
                self.self_use = False
            self.initialized = True
            await self._save()
            self.notify()

    async def set_power(self, watts: float):
        if not (-self.max_w <= watts <= self.max_w):
            raise ValueError(f"Solis dispatch target must be within +/-{self.max_w} W")
        watts = int(round(watts / 10)) * 10
        async with self._lock:
            if self.self_use is None:
                raise RuntimeError('Solis remote-control mode not yet verified')
            if not self.self_use:
                await self._write_target(watts)
            if watts:
                self.last_direction = 2 if watts > 0 else 1
            self.target_w = watts
            self.initialized = True
            await self._save()
            self.notify()

    @property
    def max_w(self):
        return min(int(getattr(self.controller.inverter_config, "wattage_chosen", 5000)), 655350)

    async def _write_off(self):
        if not self.controller.connected():
            self.last_error = "Modbus disconnected; RC off not delivered"
            raise RuntimeError(self.last_error)
        await self.controller.async_write_holding_register(MODE, 0)
        self.last_error = None
        self.last_request = "RC off queued"

    async def _write_target(self, watts: int):
        if not self.controller.connected():
            self.last_error = "Modbus disconnected; dispatch not delivered"
            raise RuntimeError(self.last_error)
        # A zero target is still a remote-control request. Keep the previous
        # force direction and write zero to its setpoint instead of RC mode=0.
        # Whether S5 firmware physically holds 0 W must be verified.
        mode = 2 if watts > 0 else (1 if watts < 0 else self.last_direction)
        raw = abs(watts) // 10
        async with self.controller.write_batch():
            await self.controller.async_write_holding_register(MODE, mode)
            await self.controller.async_write_holding_register(DISCHARGE if watts > 0 else CHARGE, raw)
            await self.controller.async_write_holding_register(RC_TIMEOUT, 2)
        self.last_error = None
        self.last_request = (
            f"{watts} W RC {'discharge' if mode == 2 else 'charge'} command queued; "
            "actual output unverified"
        )

    async def _heartbeat(self, _now=None):
        if not self.initialized or self.self_use is not False or self._lock.locked():
            return
        async with self._lock:
            try:
                await self._write_target(self.target_w)
            except Exception as exc:
                self.last_error = str(exc)
                _LOGGER.warning("Solis RC refresh failed: %s", exc)
            self.notify()


class SolisDispatchPower(NumberEntity):
    _attr_has_entity_name = True
    _attr_name = "Forced battery power"
    _attr_native_unit_of_measurement = "W"
    _attr_mode = NumberMode.BOX
    _attr_native_step = 10
    _attr_should_poll = False

    def __init__(self, coord):
        self.coord = coord
        self._attr_unique_id = unique_id_generator(coord.controller, "battery_dispatch_power", "rc")
        self._attr_native_min_value = -coord.max_w
        self._attr_native_max_value = coord.max_w

    @property
    def device_info(self):
        return self.coord.controller.device_info

    @property
    def available(self):
        return self.coord.initialized

    @property
    def native_value(self):
        return self.coord.target_w

    @property
    def extra_state_attributes(self):
        return {
            "control_mode": "self_use" if self.coord.self_use else "forced_power",
            "command_status": self.coord.last_request,
            "error": self.coord.last_error,
            "note": "Requested watts only. Verify physical output using battery power telemetry.",
        }

    async def async_set_native_value(self, value):
        await self.coord.set_power(value)


class SolisDispatchSelfUse(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Self-Use Mode"
    _attr_should_poll = False

    def __init__(self, coord):
        self.coord = coord
        self._attr_unique_id = unique_id_generator(coord.controller, "battery_dispatch_self_use", "rc")

    @property
    def device_info(self):
        return self.coord.controller.device_info

    @property
    def available(self):
        return self.coord.initialized

    @property
    def is_on(self):
        return self.coord.self_use

    @property
    def extra_state_attributes(self):
        return {
            "mode": "self_use" if self.coord.self_use else "forced_power",
            "switch_on": "Self-use mode",
            "switch_off": "Forced-power mode",
            "command_status": self.coord.last_request,
            "error": self.coord.last_error,
            "firmware_support": "not verified",
        }

    async def async_turn_on(self, **kwargs):
        await self.coord.set_self_use(True)

    async def async_turn_off(self, **kwargs):
        await self.coord.set_self_use(False)
