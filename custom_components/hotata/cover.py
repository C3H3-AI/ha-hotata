"""Cover entities: airer rails (with position simulation) and curtains."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import (
    ADVANCED_AIRER_PRODUCT_KEYS,
    CURTAIN_V1_PRODUCT_KEYS,
    CURTAIN_V2_PRODUCT_KEYS,
    DOMAIN,
)
from .exceptions import HotataError
from .coordinator import HotataCoordinator
from .entity import (
    HotataEntity,
    async_setup_dynamic_entities,
    entity_identity,
    has_property,
)

_LOGGER = logging.getLogger(__name__)

_AUTO_STOP_RETRY_SECONDS = 15

MOTOR_STOP = 0
MOTOR_OPEN = 1
MOTOR_CLOSE = 2


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create and dynamically discover cover entities."""
    coordinator: HotataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda device: _covers_for_device(coordinator, device),
    )


def _covers_for_device(
    coordinator: HotataCoordinator, device
) -> Iterable[CoverEntity]:
    if device.product_key in CURTAIN_V1_PRODUCT_KEYS and has_property(
        device, "CurtainPosition"
    ):
        yield HotataCurtainV1(coordinator, device)
        return
    if device.product_key in CURTAIN_V2_PRODUCT_KEYS and has_property(
        device, "OpeningPercentage"
    ):
        yield HotataCurtainV2(coordinator, device)
        return
    # Main rail: this project's cover with time-based position simulation.
    if has_property(device, "MotorControlMode"):
        yield HotataAirerCover(coordinator, device)
    # Additional rails on feature-rich airers (A/B pole models).
    if device.product_key in ADVANCED_AIRER_PRODUCT_KEYS:
        for identifier, name in (
            ("ApoleMotorControlMode", "A 杆"),
            ("BpoleMotorControlMode", "B 杆"),
        ):
            if has_property(device, identifier):
                yield HotataRailCover(coordinator, device, identifier, name)


class HotataRailCover(HotataEntity, CoverEntity):
    """Raise, lower, or stop one clothes-airer rail (no position)."""

    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
    )

    def __init__(
        self, coordinator: HotataCoordinator, device, identifier: str, name: str
    ) -> None:
        super().__init__(coordinator, device)
        self.identifier = identifier
        self._attr_name = name
        self._attr_unique_id = entity_identity(device, identifier)

    @property
    def is_opening(self) -> bool:
        return self.property_value(self.identifier) == MOTOR_OPEN

    @property
    def is_closing(self) -> bool:
        return self.property_value(self.identifier) == MOTOR_CLOSE

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self.async_set_property(self.identifier, MOTOR_OPEN)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.async_set_property(self.identifier, MOTOR_CLOSE)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self.async_set_property(self.identifier, MOTOR_STOP)


class HotataAirerCover(HotataEntity, CoverEntity):
    """Main airer rail with time-based position simulation and auto-stop.

    按下降键 → 运行 descent_time 秒 → 自动停止在目标位置。
    上升键 → 一直升到顶。中途按停止 → 停在当前位置。
    设备没有真实位置传感器（Position 只是粗粒度枚举），位置由时间模拟。
    """

    _attr_device_class = CoverDeviceClass.SHADE
    _attr_translation_key = "cover"
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: HotataCoordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = entity_identity(device, "airer")
        self._position: int | None = 100
        self._stop_timer = None
        self._last_motor_mode: int | None = None

    @property
    def runtime(self):
        """Per-device runtime (descent time + simulated position)."""
        return self.coordinator.runtime(self.device.iot_id)

    @property
    def assumed_state(self) -> bool:
        """API 没有真实位置传感器时标记为假设状态。"""
        position = self.property_value("Position")
        return position not in (1, 2, 3)

    @property
    def current_cover_position(self) -> int | None:
        """Return current position."""
        return self._position

    @property
    def is_opening(self) -> bool:
        return self.property_value("MotorControlMode") == MOTOR_OPEN

    @property
    def is_closing(self) -> bool:
        return self.property_value("MotorControlMode") == MOTOR_CLOSE

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is at the lowest position."""
        return self._position == 0

    def _cancel_stop_timer(self) -> None:
        self.runtime.target_position = None
        if self._stop_timer is not None:
            self._stop_timer()
            self._stop_timer = None

    async def _async_auto_stop_cover(self, _now: Any) -> None:
        """Auto-stop callback: stops the motor after the simulated time."""
        self._stop_timer = None
        try:
            await self.async_set_property("MotorControlMode", MOTOR_STOP)
        except HotataError:
            # Stop command failed (cloud hiccup / penalty) — retry shortly
            # instead of letting the motor run to its limit silently.
            _LOGGER.warning(
                "Auto-stop command failed, retrying in %ds",
                _AUTO_STOP_RETRY_SECONDS,
            )
            self._stop_timer = async_call_later(
                self.hass, _AUTO_STOP_RETRY_SECONDS, self._async_auto_stop_cover
            )
            return
        runtime = self.runtime
        self._position = (
            runtime.target_position if runtime.target_position is not None else 0
        )
        runtime.simulated_position = self._position
        runtime.closing_start = None
        runtime.target_position = None
        self.async_write_ha_state()
        _LOGGER.debug("Cover auto-stopped at position %d", self._position)

    def _handle_coordinator_update(self) -> None:
        """Sync the simulated position with motor transitions."""
        runtime = self.runtime
        mode = self.property_value("MotorControlMode")
        # Motor stopped after moving: snap the simulated position to the end.
        if (
            self._last_motor_mode is not None
            and self._last_motor_mode != MOTOR_STOP
            and mode == MOTOR_STOP
            and runtime.closing_start is None
        ):
            if self._last_motor_mode == MOTOR_OPEN:
                runtime.simulated_position = 100
            elif self._last_motor_mode == MOTOR_CLOSE:
                runtime.simulated_position = 0
        if mode is not None:
            self._last_motor_mode = mode
        # While a simulated descent runs, advance the estimate.
        if runtime.closing_start is not None:
            elapsed = time.time() - runtime.closing_start
            ratio = min(elapsed / max(runtime.descent_time, 1), 1.0)
            estimated = max(0, 100 - int(ratio * 100))
            self._position = estimated
            runtime.simulated_position = estimated
        elif self._position != runtime.simulated_position:
            self._position = runtime.simulated_position
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover (上升/收起)."""
        self._cancel_stop_timer()
        try:
            await self.async_set_property("MotorControlMode", MOTOR_OPEN)
        except HotataError as err:
            _LOGGER.error("Open cover failed: %s", err)
            return
        runtime = self.runtime
        runtime.simulated_position = 100
        runtime.closing_start = None
        self._position = 100

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover (下降/展开), auto-stopping at the bottom."""
        self._cancel_stop_timer()
        try:
            await self.async_set_property("MotorControlMode", MOTOR_CLOSE)
        except HotataError as err:
            _LOGGER.error("Close cover failed: %s", err)
            return
        runtime = self.runtime
        runtime.target_position = 0
        runtime.closing_start = time.time()
        current = self._position or 100
        time_needed = max(1, current / 100 * runtime.descent_time)
        self._stop_timer = async_call_later(
            self.hass, time_needed, self._async_auto_stop_cover
        )
        _LOGGER.debug(
            "Cover descending from %d%%, auto-stop in %.1f seconds",
            current,
            time_needed,
        )

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover (中途停止)."""
        self._cancel_stop_timer()
        try:
            await self.async_set_property("MotorControlMode", MOTOR_STOP)
        except HotataError as err:
            _LOGGER.error("Stop cover failed: %s", err)
            return
        runtime = self.runtime
        if runtime.closing_start is not None:
            elapsed = time.time() - runtime.closing_start
            ratio = min(elapsed / max(runtime.descent_time, 1), 1.0)
            self._position = max(0, 100 - int(ratio * 100))
            runtime.simulated_position = self._position
        runtime.closing_start = None

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Set the cover to a specific position."""
        target = kwargs.get(ATTR_POSITION, 100)
        current = self._position or 100
        if target == current:
            return
        self._cancel_stop_timer()
        if target > current:
            await self.async_open_cover()
            return
        try:
            await self.async_set_property("MotorControlMode", MOTOR_CLOSE)
        except HotataError as err:
            _LOGGER.error("Set cover position failed: %s", err)
            return
        runtime = self.runtime
        runtime.target_position = target
        runtime.closing_start = time.time()
        time_needed = max(
            1, (current - target) / 100 * runtime.descent_time
        )
        self._stop_timer = async_call_later(
            self.hass, time_needed, self._async_auto_stop_cover
        )
        _LOGGER.debug(
            "Cover descending to %d%%, auto-stop in %.1f seconds",
            target,
            time_needed,
        )


# ---- curtain machines ----


class _HotataCurtainBase(HotataEntity, CoverEntity):
    _attr_name = "窗帘"
    _attr_device_class = CoverDeviceClass.CURTAIN
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = entity_identity(device, "curtain")

    @property
    def is_closed(self) -> bool | None:
        position = self.current_cover_position
        return position == 0 if position is not None else None


class HotataCurtainV1(_HotataCurtainBase):
    """First-generation curtain controlled through writable properties."""

    @property
    def current_cover_position(self) -> int | None:
        value = self.property_value("CurtainPosition")
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return None

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self.async_set_property("CurtainPosition", 100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.async_set_property("CurtainPosition", 0)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self.async_set_property("CurtainOperation", 2)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self.async_set_property("CurtainPosition", kwargs[ATTR_POSITION])


class HotataCurtainV2(_HotataCurtainBase):
    """Second-generation curtain controlled through thing services."""

    @property
    def current_cover_position(self) -> int | None:
        value = self.property_value("OpeningPercentage")
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return None

    @property
    def is_opening(self) -> bool:
        return self.property_value("MotorStatus") == MOTOR_OPEN

    @property
    def is_closing(self) -> bool:
        return self.property_value("MotorStatus") == MOTOR_CLOSE

    async def _async_motor(self, mode: int) -> None:
        await self.async_invoke_service("MotorControl", {"Mode": mode})

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_motor(MOTOR_OPEN)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_motor(MOTOR_CLOSE)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._async_motor(MOTOR_STOP)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self.async_invoke_service(
            "OpeningPercentageControl",
            {"Percentage": kwargs[ATTR_POSITION], "Channel": 0},
        )
