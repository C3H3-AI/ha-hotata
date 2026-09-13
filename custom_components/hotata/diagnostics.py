"""Diagnostics support for Hotata Airer (per-account entry)."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import HotataCoordinator
from .hub import HotataAccount


def _mask_username(value: Any) -> str:
    """Mask a phone-number style username, keeping the outline."""
    s = str(value)
    if len(s) > 7:
        return f"{s[:3]}****{s[-4:]}"
    return "****"


def _redact_entry_data(data: dict[str, Any]) -> dict[str, Any]:
    """Redact credentials (tokens truncated, passwords/usernames masked).

    Matches by substring so ``backup_password`` and ``backup_username``
    cannot leak the way an exact-key check allowed.
    """
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        lowered = key.lower()
        if "password" in lowered:
            redacted[key] = "<redacted>"
        elif "username" in lowered:
            redacted[key] = _mask_username(value)
        elif "token" in lowered:
            redacted[key] = value if isinstance(value, str) and len(value) <= 40 else str(value)[:40] + "..."
        else:
            redacted[key] = value
    return redacted


def _account_runtime(account: HotataAccount) -> dict[str, Any]:
    """Collect failover/runtime diagnostics for the account."""
    now = time.time()
    return {
        "using_backup": account.using_backup,
        "primary_penalty_remaining_s": max(
            0.0, account._primary_penalty_until - now
        ),
        "backup_penalty_remaining_s": max(
            0.0, account._backup_penalty_until - now
        ),
        "active_issues": account.active_issues,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for the account config entry."""
    coordinator: HotataCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    devices: list[dict[str, Any]] = []
    if coordinator is not None:
        for device in (coordinator.data or {}).values():
            runtime = coordinator.runtimes.get(device.iot_id)
            devices.append(
                {
                    "iot_id": device.iot_id,
                    "name": device.name,
                    "product_key": device.product_key,
                    "device_name": device.device_name,
                    "parent_iot_id": device.parent_iot_id,
                    "online": device.online,
                    "properties": device.properties,
                    "descent_time": (
                        runtime.descent_time if runtime else None
                    ),
                    "thing_model_properties": [
                        p.get("identifier")
                        for p in device.thing_model.get("properties", [])
                        if isinstance(p, dict)
                    ],
                }
            )

    data = {
        "entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "domain": entry.domain,
            "title": entry.title,
            "data": _redact_entry_data(entry.data),
            "options": entry.options,
        },
        "account_runtime": (
            _account_runtime(coordinator.account)
            if coordinator is not None
            else None
        ),
        "devices": devices,
    }

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
        }
        for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    ]

    return data
