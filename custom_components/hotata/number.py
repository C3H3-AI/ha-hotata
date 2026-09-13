"""Number entities: broadcast volumes + airer descent time."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BROADCAST_PRODUCT_KEYS, DOMAIN
from .coordinator import HotataCoordinator
from .entity import (
    HotataEntity,
    async_setup_dynamic_entities,
    entity_identity,
    has_property,
)
from .tsl import is_writable_property

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HotataNumberDescription(NumberEntityDescription):
    minimum: float
    maximum: float
    step: float = 1


BROADCAST_NUMBERS: tuple[HotataNumberDescription, ...] = tuple(
    HotataNumberDescription(
        key=key,
        name=name,
        minimum=0,
        maximum=100,
        native_unit_of_measurement=PERCENTAGE,
    )
    for key, name in (
        ("AlarmSoundVolume", "报警音量"),
        ("DoorBellSoundVolume", "门铃音量"),
        ("VocieMessageVolume", "语音留言音量"),
    )
)


def _entities(coordinator: HotataCoordinator, device) -> Iterable[Any]:
    # Airer descent-time configuration (this project).
    if has_property(device, "MotorControlMode"):
        yield DescentTimeNumber(coordinator, device)
    for description in BROADCAST_NUMBERS:
        if (
            device.product_key in BROADCAST_PRODUCT_KEYS
            and has_property(device, description.key)
            and is_writable_property(device, description.key)
        ):
            yield HotataPropertyNumber(coordinator, device, description)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create and dynamically discover number entities."""
    coordinator: HotataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda device: _entities(coordinator, device),
    )


class DescentTimeNumber(HotataEntity, NumberEntity):
    """Configure the airer cover's full descent duration (seconds)."""

    _attr_translation_key = "descent_time"
    _attr_native_min_value = 0
    _attr_native_max_value = 20
    _attr_native_step = 1
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HotataCoordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = entity_identity(device, "descent_time")

    @property
    def available(self) -> bool:
        """Config parameter is always available."""
        return True

    @property
    def native_value(self) -> float:
        """Return the current descent time."""
        return float(self.coordinator.runtime(self.device.iot_id).descent_time)

    async def async_set_native_value(self, value: float) -> None:
        """Update descent time at runtime without reloading."""
        new_value = int(value)
        _LOGGER.info(
            "Setting descent time to %d seconds for %s",
            new_value,
            self.device.name,
        )
        await self.coordinator.runtime(
            self.device.iot_id
        ).async_set_descent_time(new_value)
        self.async_write_ha_state()


class HotataPropertyNumber(HotataEntity, NumberEntity):
    """Control one numeric device property."""

    entity_description: HotataNumberDescription

    def __init__(
        self, coordinator: HotataCoordinator, device, description
    ) -> None:
        super().__init__(coordinator, device)
        self.entity_description = description
        self._attr_unique_id = entity_identity(device, description.key)
        self._attr_native_min_value = description.minimum
        self._attr_native_max_value = description.maximum
        self._attr_native_step = description.step

    @property
    def native_value(self) -> float | None:
        value = self.property_value(self.entity_description.key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        wire_value = int(value) if value.is_integer() else value
        await self.async_set_property(self.entity_description.key, wire_value)
