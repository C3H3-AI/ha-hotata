"""Sensor platform for Hotata Airer."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .hub import HotataAccount, HotataHub

_LOGGER = logging.getLogger(__name__)

REMAINING_TIME_SENSORS = {
    "light_remaining_time": "light_time",
    "disinfection_remaining_time": "disinfection_time",
    "drying_remaining_time": "drying_time",
    "air_drying_remaining_time": "air_drying_time",
    "ions_remaining_time": "ions_time",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    account: HotataAccount = hass.data["hotata_airer"][entry.entry_id]
    _LOGGER.debug(
        "Setting up sensor platform for %s, devices=%d",
        entry.entry_id, len(account.device_hubs),
    )
    entities = []
    for hub in account.device_hubs.values():
        entities.extend([
            PositionSensor(hub),
            *[RemainingTimeSensor(hub, trans_key, old_suffix)
              for trans_key, old_suffix in REMAINING_TIME_SENSORS.items()],
            MotorControlModeSensor(hub),
            ErrorStateSensor(hub),
        ])
    _LOGGER.debug("Adding %d sensor entities", len(entities))
    async_add_entities(entities)


class PositionSensor(SensorEntity):
    """Position sensor (0=up, value increases as lowered)."""

    _attr_native_unit_of_measurement = "%"
    _attr_state_class = "measurement"
    _attr_has_entity_name = True
    _attr_translation_key = "position"

    def __init__(self, hub: HotataHub) -> None:
        """Initialize the sensor."""
        self._hub = hub
        self._attr_unique_id = f"{hub.iot_id}_position"
        self._attr_device_info = hub.device_info

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        self.async_write_ha_state()
        self._hub.add_listener(self._handle_update)

    async def _handle_update(self) -> None:
        """Handle state update."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        """Return the simulated position value."""
        return self._hub.state.simulated_position

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return not self._hub.token_expired


class RemainingTimeSensor(SensorEntity):
    """Generic remaining time sensor for all timed functions."""

    _attr_native_unit_of_measurement = "min"
    _attr_state_class = "measurement"
    _attr_has_entity_name = True

    def __init__(self, hub: HotataHub, translation_key: str, old_suffix: str) -> None:
        """Initialize the sensor."""
        self._hub = hub
        self._attr_translation_key = translation_key
        self._state_attr = translation_key
        self._attr_unique_id = f"{hub.iot_id}_{old_suffix}"
        self._attr_device_info = hub.device_info

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        self.async_write_ha_state()
        self._hub.add_listener(self._handle_update)

    async def _handle_update(self) -> None:
        """Handle state update."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        """Return the remaining time value."""
        val = getattr(self._hub.state, self._state_attr, None)
        return int(val) if val is not None else None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return not self._hub.token_expired


class MotorControlModeSensor(SensorEntity):
    """Motor control mode sensor (0=stop, 1=up, 2=down)."""

    _attr_has_entity_name = True
    _attr_translation_key = "motor_control_mode"

    def __init__(self, hub: HotataHub) -> None:
        """Initialize the sensor."""
        self._hub = hub
        self._attr_unique_id = f"{hub.iot_id}_motor_mode"
        self._attr_device_info = hub.device_info

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        self.async_write_ha_state()
        self._hub.add_listener(self._handle_update)

    async def _handle_update(self) -> None:
        """Handle state update."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        """Return the motor mode as raw value (translated by HA)."""
        return self._hub.state.motor_control_mode

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return not self._hub.token_expired


class ErrorStateSensor(SensorEntity):
    """Error state sensor — shows error description when token is invalid."""

    _attr_has_entity_name = True
    _attr_translation_key = "error_state"
    _attr_icon = "mdi:alert-circle"

    def __init__(self, hub: HotataHub) -> None:
        """Initialize the sensor."""
        self._hub = hub
        self._attr_unique_id = f"{hub.iot_id}_error_state"
        self._attr_device_info = hub.device_info

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        self.async_write_ha_state()
        self._hub.add_listener(self._handle_update)

    async def _handle_update(self) -> None:
        """Handle state update."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Return the machine-readable error key (translated by HA)."""
        issues = self._hub.account.active_issues
        if "rate_limited" in issues:
            return "rate_limited"
        if "connection_error" in issues:
            return "connection_error"
        return self._hub.last_error or "normal"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose active issues and raw server error text for diagnostics."""
        attrs: dict[str, Any] = {}
        detail = self._hub.last_error_detail
        if detail:
            attrs["detail"] = detail
        issues = self._hub.account.active_issues
        if issues:
            attrs["active_issues"] = issues
        return attrs

    @property
    def available(self) -> bool:
        """Always available so user can see error state."""
        return True