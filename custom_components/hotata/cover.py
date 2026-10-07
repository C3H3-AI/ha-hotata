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

from . import capabilities
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
    # Additional rails. The product-line TSL declares the A/B pole properties
    # on every model of the family, but only a device whose capability list
    # sets DOUBLE_POLE actually has a second rail — a D-3072S declares both
    # properties, never reports either, and the vendor app hides the controls
    # (its bit 24 is 0).
    if device.product_key in ADVANCED_AIRER_PRODUCT_KEYS:
        for identifier, name in (
            ("ApoleMotorControlMode", "A 杆"),
            ("BpoleMotorControlMode", "B 杆"),
        ):
            if has_property(device, identifier) and capabilities.supported(
                device, capabilities.CAP_DOUBLE_POLE
            ):
                yield HotataRailCover(coordinator, device, identifier, name)


class _DirectionMixin:
    """Client-side travel direction, shared by both rail entities.

    Directions, in device terms (issue #15, decided 2026-10-07):

        device 100 = rail raised (收起)      device 0 = rail lowered (放下)
        default    : open (展开/打开) lowers the rail -> 100 % = rail down
        行程反转   : open raises the rail            -> 100 % = rail up

    The entity uses ``device_class: awning`` on purpose: Home Assistant draws
    its cover buttons from the device class, and only the awning/curtain family
    gets 「展开 / 合拢」 arrows instead of a hard-wired ⬆️=打开 / ⬇️=关闭. That
    keeps the icon, the button label, the reported percentage and the physical
    movement consistent with each other (see the frontend's cover icon
    function), which the shade class cannot do.

    Internal coordinates stay in DEVICE terms and only the Home Assistant
    facing values and commands are translated, so the position simulation, the
    auto-stop timer and the descent-time number are untouched.
    """

    @property
    def _reversed(self) -> bool:
        """True when 行程反转 is on: opening raises the rail instead of lowering it."""
        return bool(self.coordinator.runtime(self.device.iot_id).invert_direction)

    def _ha_position(self, device_position: int) -> int:
        """Device coordinate -> the percentage Home Assistant should show.

        Device side 100 is "rail raised". Home Assistant side 100 is "open",
        which for an airer means the rail is lowered for drying, so the two are
        mirrored — unless the direction was reversed to 打开=收起.
        """
        return device_position if self._reversed else 100 - device_position

    def _device_position(self, ha_position: int) -> int:
        """Home Assistant percentage -> the device coordinate to aim for."""
        return ha_position if self._reversed else 100 - ha_position

    @property
    def _open_command(self) -> int:
        """Device command behind Home Assistant's "open" (展开/放下)."""
        return MOTOR_OPEN if self._reversed else MOTOR_CLOSE

    @property
    def _close_command(self) -> int:
        return MOTOR_CLOSE if self._reversed else MOTOR_OPEN


class HotataRailCover(_DirectionMixin, HotataEntity, CoverEntity):
    """Raise, lower, or stop one clothes-airer rail (no position)."""

    _attr_device_class = CoverDeviceClass.AWNING
    # The device class only drives the button glyphs (see _DirectionMixin);
    # the icon is what identifies the thing on a dashboard, and an airer is
    # not an awning.
    _attr_icon = "mdi:hanger"
    # These rails have no position feedback, so is_closed can only be unknown.
    # CoverEntity annotates _attr_is_closed without a default (unlike
    # _attr_is_closing / _attr_is_opening), so omitting it here makes the base
    # is_closed cached_property raise AttributeError on every state write.
    _attr_is_closed: bool | None = None
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
        return self.property_value(self.identifier) == self._open_command

    @property
    def is_closing(self) -> bool:
        return self.property_value(self.identifier) == self._close_command

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self.async_set_property(self.identifier, self._open_command)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.async_set_property(self.identifier, self._close_command)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self.async_set_property(self.identifier, MOTOR_STOP)


class HotataAirerCover(_DirectionMixin, HotataEntity, CoverEntity):
    """Main airer rail with time-based position simulation and auto-stop.

    按下降键 → 运行 descent_time 秒 → 自动停止在目标位置。
    上升键 → 一直升到顶。中途按停止 → 停在当前位置。
    设备没有真实位置传感器（Position 只是粗粒度枚举），位置由时间模拟。
    """

    _attr_device_class = CoverDeviceClass.AWNING
    # The device class only drives the button glyphs (see _DirectionMixin).
    # The identity — a clothes airer, not an awning — comes from this
    # translation key plus the integration's own icons.json, which is what lets
    # the icon differ per state (衣架 / 挂着衣服 / 正在升 / 正在降).
    # NOTE: no _attr_icon here on purpose — a hard-coded icon would win over
    # icons.json and freeze the icon.
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
        """Return current position, in the direction the user asked for."""
        if self._position is None:
            return None
        return self._ha_position(self._position)

    @property
    def is_opening(self) -> bool:
        return self.property_value("MotorControlMode") == self._open_command

    @property
    def is_closing(self) -> bool:
        return self.property_value("MotorControlMode") == self._close_command

    @property
    def is_closed(self) -> bool | None:
        """True when the cover is at the end of its travel that reads "closed".

        Device-side that is 0 (rail lowered); with the direction reversed it is
        100 (rail raised), which is what "closed" means to that installation.
        """
        return self._position == self._device_position(0)

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
        # Same `is not None` discipline as async_close_cover: an explicit
        # target wins, and a missing one falls back to the simulated position
        # rather than a bare literal. `target_position` is cleared by
        # _cancel_stop_timer, so a cancelled descent landing here must not
        # silently snap the rail to the bottom.
        if runtime.target_position is not None:
            self._position = runtime.target_position
        elif runtime.simulated_position is not None:
            self._position = runtime.simulated_position
        else:
            self._position = 0
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

    # ---- device primitives (device coordinates: 100 = rail at the top) ----

    async def _async_rise(self) -> None:
        """Raise the rail to the top (device command 上升)."""
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
        # Write only after both _position and runtime.simulated_position are
        # updated, so the next coordinator callback sees them in agreement.
        self.async_write_ha_state()

    async def _async_descend(self, target: int) -> None:
        """Lower the rail to ``target`` (device coordinate), auto-stopping."""
        self._cancel_stop_timer()
        try:
            await self.async_set_property("MotorControlMode", MOTOR_CLOSE)
        except HotataError as err:
            _LOGGER.error("Close cover failed: %s", err)
            return
        runtime = self.runtime
        runtime.target_position = target
        runtime.closing_start = time.time()
        current = self._position if self._position is not None else 100
        time_needed = max(1, (current - target) / 100 * runtime.descent_time)
        self._stop_timer = async_call_later(
            self.hass, time_needed, self._async_auto_stop_cover
        )
        _LOGGER.debug(
            "Cover descending to %d%%, auto-stop in %.1f seconds",
            target,
            time_needed,
        )
        # Timer armed and runtime fields written: make is_closing visible now.
        self.async_write_ha_state()

    # ---- Home Assistant surface (translated when the direction is reversed) --

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open (展开): lower the rail for drying, 上升 once reversed."""
        if self._reversed:
            await self._async_rise()
        else:
            await self._async_descend(0)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close (收起): raise the rail, 下降 once reversed."""
        if self._reversed:
            await self._async_descend(0)
        else:
            await self._async_rise()

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
        # _position and runtime.simulated_position are in sync here, so the
        # coordinator callback will not roll the estimate back.
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Set the cover to a specific position.

        The percentage arrives in Home Assistant terms and is translated once,
        here; the two branches below are device-coordinate moves, so they use
        the device commands rather than the (possibly reversed) HA ones.
        """
        target = self._device_position(int(kwargs.get(ATTR_POSITION, 100)))
        current = self._position if self._position is not None else 100
        if target == current:
            _LOGGER.debug(
                "Cover already at position %d%%, skipping command", target
            )
            return
        if target > current:
            await self._async_rise()
        else:
            await self._async_descend(target)


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
