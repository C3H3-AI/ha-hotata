"""Button entities: stateless device actions + airer position reset."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CAMERA_PRODUCT_KEYS,
    CLOTHES_CARE_PRODUCT_KEYS,
    DOMAIN,
    INDOOR_ALARM_PRODUCT_KEYS,
    TOWEL_RACK_PRODUCT_KEYS,
)
from .coordinator import HotataCoordinator
from .entity import HotataEntity, async_setup_dynamic_entities, entity_identity

_LOGGER = logging.getLogger(__name__)

PTZ_ACTIONS = (
    ("ptz_left", "云台向左", "mdi:arrow-left", 0),
    ("ptz_right", "云台向右", "mdi:arrow-right", 1),
    ("ptz_up", "云台向上", "mdi:arrow-up", 2),
    ("ptz_down", "云台向下", "mdi:arrow-down", 3),
)
TOWEL_MODES = (
    ("soft_dry", "柔烘", 1, 45),
    ("warm_dry", "暖烘", 2, 50),
    ("dry", "烘干", 3, 60),
    ("sterilize", "除菌", 4, 65),
    ("custom", "自定义模式", 5, 70),
)


def _entities(coordinator: HotataCoordinator, device) -> Iterable[ButtonEntity]:
    # Airer position calibration (this project).
    if has_motor(device):
        yield HotataResetPositionButton(coordinator, device)
    if device.product_key in CAMERA_PRODUCT_KEYS:
        for key, name, icon, action_type in PTZ_ACTIONS:
            yield HotataPtzButton(
                coordinator, device, key, name, icon, action_type
            )
    if device.product_key in INDOOR_ALARM_PRODUCT_KEYS:
        yield HotataServiceButton(
            coordinator,
            device,
            "silence_alarm",
            "关闭报警",
            "mdi:alarm-light-off-outline",
            "ActivateAlarm",
            {"AlarmMode": 0, "Twinkle": 0, "Duration": 1},
        )
    if device.product_key in TOWEL_RACK_PRODUCT_KEYS:
        for key, name, mode, temperature in TOWEL_MODES:
            yield HotataServiceButton(
                coordinator,
                device,
                key,
                name,
                "mdi:radiator",
                "FunctionControl",
                {
                    "Mode": mode,
                    "OperationType": 2,
                    "TargetTemperature": temperature,
                    "TargetRunningTime": 240,
                },
            )
        for key, name, mode in (
            ("pause", "暂停", 1),
            ("resume", "继续", 0),
        ):
            yield HotataServiceButton(
                coordinator,
                device,
                key,
                name,
                "mdi:pause-circle-outline" if mode else "mdi:play-circle-outline",
                "PauseControl",
                {"Mode": mode, "OperationType": 2},
            )
    if device.product_key in CLOTHES_CARE_PRODUCT_KEYS:
        for key, name, value in (
            ("start", "开始护理", 1),
            ("stop", "停止护理", 0),
        ):
            yield HotataPropertyButton(
                coordinator,
                device,
                key,
                name,
                "mdi:play" if value else "mdi:stop",
                "OperationControl",
                value,
            )


def has_motor(device) -> bool:
    """True for devices with an airer rail (position applies)."""
    from .entity import has_property

    return has_property(device, "MotorControlMode")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create and dynamically discover buttons."""
    coordinator: HotataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda device: _entities(coordinator, device),
    )


class HotataResetPositionButton(HotataEntity, ButtonEntity):
    """Reset the simulated position to 100 (fully up)."""

    _attr_translation_key = "reset_position"
    _attr_icon = "mdi:restore"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HotataCoordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = entity_identity(device, "reset_position")

    async def async_press(self) -> None:
        """Reset the simulated position."""
        runtime = self.coordinator.runtime(self.device.iot_id)
        runtime.simulated_position = 100
        runtime.closing_start = None
        runtime.target_position = None
        _LOGGER.info("Simulated position reset to 100 (fully up)")
        self.coordinator.async_update_listeners()


class HotataPtzButton(HotataEntity, ButtonEntity):
    def __init__(
        self, coordinator, device, key, name, icon, action_type
    ) -> None:
        super().__init__(coordinator, device)
        self._action_type = action_type
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = entity_identity(device, key)

    async def async_press(self) -> None:
        await self.async_invoke_service(
            "PTZActionControl",
            {"ActionType": self._action_type, "Step": 1},
        )


class HotataServiceButton(HotataEntity, ButtonEntity):
    def __init__(
        self, coordinator, device, key, name, icon, service, args
    ) -> None:
        super().__init__(coordinator, device)
        self._service = service
        self._args = args
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = entity_identity(device, key)

    async def async_press(self) -> None:
        await self.async_invoke_service(self._service, self._args)


class HotataPropertyButton(HotataEntity, ButtonEntity):
    def __init__(
        self, coordinator, device, key, name, icon, identifier, value
    ) -> None:
        super().__init__(coordinator, device)
        self._identifier = identifier
        self._value = value
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = entity_identity(device, key)

    async def async_press(self) -> None:
        await self.async_set_property(self._identifier, self._value)
