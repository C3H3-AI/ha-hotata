"""Diagnostics support for Hotata Airer (single account entry, device list)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .hub import HotataHub

_TRUNCATE_MAX = 40


def _truncate_token(value: str, max_len: int = _TRUNCATE_MAX) -> str:
    """Truncate a token value for safe diagnostic display."""
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + "..."
    return value


def _redact_entry_data(data: dict[str, Any]) -> dict[str, Any]:
    """Redact credentials (tokens truncated, password masked) for diagnostics."""
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        lowered = key.lower()
        if lowered == "password":
            redacted[key] = "<redacted>"
        elif "token" in lowered:
            redacted[key] = _truncate_token(value)
        else:
            redacted[key] = value
    return redacted


def _device_state(hub: HotataHub) -> dict[str, Any]:
    """Collect diagnostic data for a single device hub."""
    s = hub.state
    return {
        "online": s.online,
        "power_on": s.power_on,
        "light_on": s.light_on,
        "light_brightness": s.light_brightness,
        "drying_on": s.drying_on,
        "air_drying_on": s.air_drying_on,
        "disinfection_on": s.disinfection_on,
        "ions_on": s.ions_on,
        "position": s.position,
        "motor_control_mode": s.motor_control_mode,
        "light_remaining_time": s.light_remaining_time,
        "drying_remaining_time": s.drying_remaining_time,
        "air_drying_remaining_time": s.air_drying_remaining_time,
        "ions_remaining_time": s.ions_remaining_time,
        "disinfection_remaining_time": s.disinfection_remaining_time,
    }


def _runtime(hub: HotataHub) -> dict[str, Any]:
    """Collect account/device runtime diagnostics for a single device hub."""
    return {
        "descent_time": hub.descent_time,
        "token_expired": hub.token_expired,
        "token_permanently_invalid": hub.token_permanently_invalid,
        "last_error": hub.last_error,
        "user_id": hub.user_id,
        "iot_id": hub.iot_id,
    }


def _entry_hubs(hass: HomeAssistant, entry: ConfigEntry) -> list[HotataHub]:
    """Return the device hubs belonging to an account config entry."""
    account = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if account is None:
        return []
    return list(account.device_hubs.values())


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics aggregated for the account config entry."""
    data = {
        "entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "domain": entry.domain,
            "title": entry.title,
            "data": _redact_entry_data(entry.data),
            "options": entry.options,
        },
        "devices": [],
    }

    for hub in _entry_hubs(hass, entry):
        data["devices"].append(
            {
                "iot_id": hub.iot_id,
                "name": hub.name,
                "device_state": _device_state(hub),
                "runtime": _runtime(hub),
            }
        )

    ent_reg = er.async_get(hass)
    data["entities"] = [
        {
            "entity_id": e.entity_id,
            "unique_id": e.unique_id,
            "platform": e.platform,
            "disabled_by": str(e.disabled_by),
            "hidden_by": str(e.hidden_by),
        }
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    ]

    dev_reg = dr.async_get(hass)
    data["devices_registry"] = [
        {
            "id": d.id,
            "name": d.name,
            "model": d.model,
            "manufacturer": d.manufacturer,
            "sw_version": d.sw_version,
        }
        for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    ]

    return data
