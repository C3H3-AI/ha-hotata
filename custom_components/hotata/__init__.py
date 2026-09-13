"""Hotata Airer - Home Assistant integration for all Hotata product lines.

One config entry per Hotata account. Devices are discovered dynamically from
the Aliyun Link IoT gateway and exposed by capability (TSL model + reported
properties), covering airers, curtains, towel racks, locks, sensors, cameras,
music devices, sockets, wall switches and broadcasts. The account layer adds
403 rate-limit backoff and backup-account failover.
"""

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .const import (
    CONF_DESCENT_TIME,
    CONF_IOT_REFRESH_TOKEN,
    CONF_IOT_TOKEN,
    CONF_IDENTITY_ID,
    CONF_REGISTERED_ID,
    DOMAIN,
    PLATFORMS,
    READ_ONLY_QUERIES,
    SERVICE_INVOKE_SERVICE,
    SERVICE_QUERY,
    SERVICE_SET_PROPERTY,
)
from .coordinator import HotataCoordinator
from .exceptions import HotataAuthError, HotataError
from .hub import HotataAccount

_LOGGER = logging.getLogger(__name__)

_SET_PROPERTY_SCHEMA = vol.Schema(
    {
        vol.Required("iot_id"): cv.string,
        vol.Required("property"): cv.string,
        vol.Required("value"): vol.Any(str, int, float, bool, dict, list, None),
    }
)
_INVOKE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("iot_id"): cv.string,
        vol.Required("identifier"): cv.string,
        vol.Optional("args", default={}): dict,
    }
)
_QUERY_SCHEMA = vol.Schema(
    {
        vol.Required("query"): vol.In(READ_ONLY_QUERIES),
        vol.Optional("iot_id"): cv.string,
        vol.Optional("params", default={}): dict,
        vol.Optional("config_entry_id"): cv.string,
    }
)

# Keys of the v3 (keyoo app-api) entry schema that no longer apply in v4.
_V4_DROP_KEYS = {
    "access_token",
    "refresh_token",
    "userId",
    "token_expire_at",
    "devices",
}




async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Hotata account config entry and all of its devices."""
    account = HotataAccount(hass, entry)
    try:
        await account.async_setup()
    except HotataAuthError as err:
        _LOGGER.error("Account login failed: %s", err)
        raise ConfigEntryAuthFailed(str(err)) from err

    coordinator = HotataCoordinator(hass, entry, account)
    account.coordinator = coordinator
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_SET_PROPERTY):
        _register_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Upgrade legacy entries to the current schema version.

    v3 (keyoo app-api transport with static device records) → v4 (Aliyun
    gateway transport with dynamic discovery): old tokens and device records
    are dropped — the username/password re-login carries over, and per-device
    descent times are moved into the runtime Stores, so no user action is
    required.
    """
    if entry.version < 4:
        data = {k: v for k, v in entry.data.items() if k not in _V4_DROP_KEYS}
        # Preserve per-device descent times into the runtime Stores.
        for device in entry.data.get("devices", []):
            iot_id = device.get("iotId") or device.get("iotid")
            descent = device.get(CONF_DESCENT_TIME)
            if iot_id and descent is not None:
                from homeassistant.helpers.storage import Store

                store = Store(hass, 1, f"{DOMAIN}.descent_time.{iot_id}")
                hass.async_create_task(
                    store.async_save({"descent_time": descent})
                )
        hass.config_entries.async_update_entry(entry, data=data, version=4)
        _LOGGER.info("Migrated config entry %s to v4", entry.entry_id)
    return True


def _find_coordinator(
    hass: HomeAssistant,
    iot_id: str | None = None,
    config_entry_id: str | None = None,
) -> HotataCoordinator:
    coordinators = hass.data.get(DOMAIN, {})
    if config_entry_id:
        coordinator = coordinators.get(config_entry_id)
        if coordinator is None:
            raise vol.Invalid(
                f"Unknown Hotata config_entry_id: {config_entry_id}"
            )
        return coordinator
    if iot_id:
        for coordinator in coordinators.values():
            if iot_id in (coordinator.data or {}):
                return coordinator
        raise vol.Invalid(f"Unknown Hotata iot_id: {iot_id}")
    if len(coordinators) == 1:
        return next(iter(coordinators.values()))
    if not coordinators:
        raise vol.Invalid("No loaded Hotata account")
    raise vol.Invalid(
        "Multiple Hotata accounts are loaded; provide config_entry_id"
    )


def _register_services(hass: HomeAssistant) -> None:
    async def async_set_property(call: ServiceCall) -> None:
        coordinator = _find_coordinator(hass, iot_id=call.data["iot_id"])
        await coordinator.account.api.async_set_property(
            call.data["iot_id"], call.data["property"], call.data["value"]
        )
        coordinator.account.bump_active_window()
        await coordinator.async_request_refresh()

    async def async_invoke_service(call: ServiceCall) -> None:
        coordinator = _find_coordinator(hass, iot_id=call.data["iot_id"])
        await coordinator.account.api.async_invoke_service(
            call.data["iot_id"],
            call.data["identifier"],
            call.data["args"],
        )
        coordinator.account.bump_active_window()
        await coordinator.async_request_refresh()

    async def async_query(call: ServiceCall) -> dict:
        iot_id = call.data.get("iot_id")
        params = dict(call.data["params"])
        if iot_id:
            params.setdefault("iotId", iot_id)
        coordinator = _find_coordinator(
            hass,
            iot_id=iot_id,
            config_entry_id=call.data.get("config_entry_id"),
        )
        result = await coordinator.account.api.async_query(
            call.data["query"], params
        )
        path, api_version = READ_ONLY_QUERIES[call.data["query"]]
        return {
            "query": call.data["query"],
            "path": path,
            "api_version": api_version,
            "result": result,
        }

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_SET_PROPERTY,
        async_set_property,
        schema=_SET_PROPERTY_SCHEMA,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_INVOKE_SERVICE,
        async_invoke_service,
        schema=_INVOKE_SERVICE_SCHEMA,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_QUERY,
        async_query,
        schema=_QUERY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one account."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: HotataCoordinator | None = hass.data[DOMAIN].pop(
            entry.entry_id, None
        )
        if coordinator is not None:
            await coordinator.account.async_unload()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SET_PROPERTY)
            hass.services.async_remove(DOMAIN, SERVICE_INVOKE_SERVICE)
            hass.services.async_remove(DOMAIN, SERVICE_QUERY)
    return unloaded
