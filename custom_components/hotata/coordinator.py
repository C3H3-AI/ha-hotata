"""Polling coordinator for Hotata cloud devices.

Polls with a dynamic interval: fast (5s) while a motor runs or a command was
just sent, slow (30s) otherwise. Any offline device pins the cadence at 30s
and only its connectivity is probed — property and TSL reads are skipped for
it, because an offline device rejects them and the attempts only feed the
cloud's throttle. The first poll that sees the device back online restores the
normal fast/slow choice.

A cloud 403 (操作过于频繁) penalizes the active identity through the account's
failover state machine instead of being retried, so throttling never amplifies
itself.

Devices are discovered recursively: gateways are expanded into their
sub-devices, and each device's thing model is fetched to decide which
capabilities it really exposes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DESCENT_TIME,
    DEFAULT_DESCENT_TIME,
    DOMAIN,
    EVENT_BUTTON_PRODUCT_KEYS,
    GATEWAY_PRODUCT_KEYS,
    POLL_INTERVAL_FAST,
    POLL_INTERVAL_OFFLINE,
    POLL_INTERVAL_SLOW,
)
from .exceptions import HotataAuthError, HotataError, HotataRateLimited
from .hub import HotataAccount
from .models import HotataDevice

_LOGGER = logging.getLogger(__name__)


#: Bumped when the meaning of ``invert_direction`` changes, so stored choices
#: can be migrated instead of silently flipping the user's direction.
DIRECTION_SEMANTICS_VERSION = 2


@dataclass
class DeviceRuntime:
    """Per-device client-side state: config + cover position simulation."""

    hass: HomeAssistant
    iot_id: str
    store: Store = None  # type: ignore[assignment]
    descent_time: int = DEFAULT_DESCENT_TIME
    #: Local-only preference (issue #15). Off means the 4.0.14 default — open
    #: (展开) lowers the rail; on means the reverse, open raises it. Nothing is
    #: ever sent to the cloud.
    invert_direction: bool = False
    # Shared position-simulation state (written by the cover, the reset
    # button and the descent-time number).
    simulated_position: int = 100
    closing_start: float | None = None
    target_position: int | None = None

    async def async_load(self) -> None:
        """Load the persisted descent time."""
        self.store = Store(self.hass, 1, f"{DOMAIN}.descent_time.{self.iot_id}")
        data = await self.store.async_load() or {}
        if "descent_time" in data:
            self.descent_time = int(data["descent_time"])
        if "invert_direction" in data:
            self.invert_direction = bool(data["invert_direction"])
            if data.get("direction_semantics") != DIRECTION_SEMANTICS_VERSION:
                # 4.0.14 made "off" mean 打开=放下. Whoever had the switch on
                # asked for exactly that, and whoever left it alone was on the
                # old default — so both migrate to the new default: off. Write
                # it back so the stored value stops disagreeing with the live
                # one (and so the migration cannot run twice).
                self.invert_direction = False
                await self._async_save()

    async def async_set_descent_time(self, value: int) -> None:
        """Persist a new descent time."""
        self.descent_time = value
        await self._async_save()

    async def async_set_invert_direction(self, value: bool) -> None:
        """Persist the direction preference (local only, no cloud call)."""
        self.invert_direction = bool(value)
        await self._async_save()

    async def _async_save(self) -> None:
        """Write every per-device preference, so one never drops another."""
        await self.store.async_save(
            {
                "descent_time": self.descent_time,
                "invert_direction": self.invert_direction,
                "direction_semantics": DIRECTION_SEMANTICS_VERSION,
            }
        )


class HotataCoordinator(DataUpdateCoordinator[dict[str, HotataDevice]]):
    """Keep device metadata, online state and properties fresh."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        account: HotataAccount,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL_SLOW),
        )
        self.entry = entry
        self.account = account
        self.thing_models: dict[str, dict] = {}
        self.runtimes: dict[str, DeviceRuntime] = {}

    def runtime(self, iot_id: str) -> DeviceRuntime:
        """Return (and lazily create) the runtime state for one device."""
        runtime = self.runtimes.get(iot_id)
        if runtime is None:
            runtime = DeviceRuntime(
                hass=self.hass,
                iot_id=iot_id,
                descent_time=int(
                    self.entry.data.get(CONF_DESCENT_TIME, DEFAULT_DESCENT_TIME)
                ),
            )
            self.runtimes[iot_id] = runtime
            self.hass.async_create_task(runtime.async_load())
        return runtime

    @property
    def first_device_id(self) -> str | None:
        """The first discovered device id, for probing and status display."""
        data = self.data or {}
        return next(iter(data), None)

    async def _async_discover_devices(self) -> list[HotataDevice]:
        """Discover direct bindings and recursively exposed gateway children."""
        devices = await self.account.api.async_list_devices()
        by_id = {device.iot_id: device for device in devices}
        queue = [
            device
            for device in devices
            if device.product_key in GATEWAY_PRODUCT_KEYS
        ]
        visited: set[str] = set()
        while queue:
            gateway = queue.pop(0)
            if gateway.iot_id in visited:
                continue
            visited.add(gateway.iot_id)
            try:
                children = await self.account.api.async_list_subdevices(
                    gateway.iot_id
                )
            except HotataError as err:
                _LOGGER.debug(
                    "Subdevice discovery failed for %s: %s",
                    gateway.iot_id,
                    err,
                )
                continue
            for child in children:
                child.parent_iot_id = gateway.iot_id
                if child.iot_id not in by_id:
                    by_id[child.iot_id] = child
                elif not by_id[child.iot_id].parent_iot_id:
                    by_id[child.iot_id].parent_iot_id = gateway.iot_id
                if child.product_key in GATEWAY_PRODUCT_KEYS:
                    queue.append(child)
        return list(by_id.values())

    async def _async_update_data(self) -> dict[str, HotataDevice]:
        if self.account.rate_limited:
            # Silence window: issue zero requests, keep last known data.
            _LOGGER.debug("Rate limited, skipping poll entirely")
            return self.data or {}
        try:
            devices = await self._async_discover_devices()
            for device in devices:
                # Connectivity first: a device that is down should not be
                # polled hard, and the cadence drops to the offline interval
                # below.
                try:
                    device.online = await self.account.api.async_get_online(
                        device.iot_id
                    )
                except HotataError as err:
                    _LOGGER.debug(
                        "Status update failed for %s: %s",
                        device.iot_id,
                        err,
                    )
                # Warm-up: the first poll has to establish what the device *is*
                # — its thing model and its reported capabilities — even while
                # it is offline. Measured on a device the cloud reports as
                # offline (status 3): /thing/properties/get still answers with
                # the full shadow (DeviceModelType included) and
                # /thing/tsl/get with the whole declaration. Skipping both
                # leaves has_property() with nothing to stand on, so none of
                # that device's entities are ever created and automations lose
                # their entity ids on every restart. Only a device that is both
                # offline *and* already known is skipped.
                cache_key = device.product_key or device.iot_id
                previous = (self.data or {}).get(device.iot_id)
                warm = (
                    cache_key in self.thing_models
                    and previous is not None
                    and bool(previous.properties)
                )
                if device.online is False and warm:
                    # Keep the last known properties so entities stay put
                    # (they go unavailable via HotataEntity.available) and we
                    # can tell the moment it comes back. Device objects are
                    # rebuilt from the device list on every poll, so this has
                    # to be carried over explicitly.
                    device.properties = previous.properties
                    device.thing_model = self.thing_models[cache_key]
                    _LOGGER.debug(
                        "Device %s offline, skipping property poll",
                        device.iot_id,
                    )
                    continue
                try:
                    device.properties = await self.account.api.async_get_properties(
                        device.iot_id
                    )
                except HotataError as err:
                    _LOGGER.debug(
                        "Property update failed for %s: %s",
                        device.iot_id,
                        err,
                    )
                if device.product_key in EVENT_BUTTON_PRODUCT_KEYS:
                    try:
                        event = await self.account.api.async_get_latest_event(
                            device.iot_id, "KeyEvent"
                        )
                        if event is not None:
                            device.properties["KeyEvent"] = {"value": event}
                    except HotataError as err:
                        _LOGGER.debug(
                            "Event update failed for %s: %s",
                            device.iot_id,
                            err,
                        )
                try:
                    if cache_key not in self.thing_models:
                        self.thing_models[cache_key] = (
                            await self.account.api.async_get_thing_model(
                                device.iot_id
                            )
                        )
                    device.thing_model = self.thing_models[cache_key]
                except HotataError as err:
                    _LOGGER.debug(
                        "Thing-model update failed for %s: %s",
                        device.iot_id,
                        err,
                    )
            self.account.note_cloud_success()
            self.account.maybe_persist_primary_tokens()
            self._adjust_poll_interval(devices)
            return {device.iot_id: device for device in devices}
        except HotataRateLimited as err:
            self.account.report_rate_limited()
            raise UpdateFailed(f"Cloud rate limit: {err}") from err
        except HotataAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HotataError as err:
            self.account.note_cloud_failure(err)
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            self.account.note_cloud_failure(err)
            raise UpdateFailed(str(err)) from err

    def _adjust_poll_interval(self, devices: list[HotataDevice]) -> None:
        """Pick the poll cadence from device state and recent activity.

        - any device offline -> offline interval (connectivity probe only,
          see _async_update_data), so we notice recovery without hammering
          a device that cannot answer;
        - a motor moving or a command just sent -> fast;
        - otherwise -> slow.
        """
        offline = any(device.online is False for device in devices)
        moving = any(
            _is_motor_running(device.properties) for device in devices
        )
        fast = timedelta(seconds=POLL_INTERVAL_FAST)
        slow = timedelta(seconds=POLL_INTERVAL_SLOW)
        offline_interval = timedelta(seconds=POLL_INTERVAL_OFFLINE)
        if offline:
            # Offline wins over "active": a probe cadence is all that is
            # useful, and the next successful poll re-enters the normal
            # fast/slow choice because device.online flips back to True.
            wanted = offline_interval
        else:
            active = moving or self.account.poll_active
            wanted = fast if active else slow
        if self.update_interval != wanted:
            self.update_interval = wanted
            _LOGGER.debug(
                "Poll interval -> %ss (offline=%s moving=%s)",
                wanted.total_seconds(),
                offline,
                moving,
            )


def _is_motor_running(properties: dict[str, Any]) -> bool:
    """True while any rail motor is reported as opening/closing."""
    for identifier in (
        "MotorControlMode",
        "ApoleMotorControlMode",
        "BpoleMotorControlMode",
    ):
        if properties.get(identifier) in (1, 2):
            return True
    return False
