"""Binary sensors for Hotata devices (all product lines)."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONTACT_SENSOR_PRODUCT_KEYS,
    DOMAIN,
    GAS_SENSOR_PRODUCT_KEYS,
    INDOOR_ALARM_PRODUCT_KEYS,
    MOTION_SENSOR_PRODUCT_KEYS,
    SMOKE_SENSOR_PRODUCT_KEYS,
    WATER_SENSOR_PRODUCT_KEYS,
)
from .coordinator import HotataCoordinator
from .entity import (
    HotataEntity,
    async_setup_dynamic_entities,
    entity_identity,
    has_property,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HotataBinaryDescription(BinarySensorEntityDescription):
    """Describe a boolean state property."""

    active_values: frozenset = frozenset({1, "1", True})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create and dynamically discover binary sensors."""
    coordinator: HotataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda device: _binary_sensors_for_device(coordinator, device),
    )


def _binary_sensors_for_device(
    coordinator: HotataCoordinator, device
) -> Iterable[BinarySensorEntity]:
    yield HotataConnectivitySensor(coordinator, device)
    descriptions = []
    if device.product_key in MOTION_SENSOR_PRODUCT_KEYS:
        descriptions.append(
            HotataBinaryDescription(
                key="MotionAlarmState",
                name="移动",
                device_class=BinarySensorDeviceClass.MOTION,
            )
        )
    if device.product_key in CONTACT_SENSOR_PRODUCT_KEYS:
        descriptions.append(
            HotataBinaryDescription(
                key="ContactState",
                name="开合",
                device_class=BinarySensorDeviceClass.OPENING,
            )
        )
    if device.product_key in SMOKE_SENSOR_PRODUCT_KEYS:
        descriptions.append(
            HotataBinaryDescription(
                key="SmokeSensorState",
                name="烟雾",
                device_class=BinarySensorDeviceClass.SMOKE,
            )
        )
    if device.product_key in GAS_SENSOR_PRODUCT_KEYS:
        descriptions.append(
            HotataBinaryDescription(
                key="GasSensorState",
                name="燃气",
                device_class=BinarySensorDeviceClass.GAS,
            )
        )
    if device.product_key in WATER_SENSOR_PRODUCT_KEYS:
        descriptions.append(
            HotataBinaryDescription(
                key="WaterSensorState",
                name="水浸",
                device_class=BinarySensorDeviceClass.MOISTURE,
            )
        )
    if device.product_key in INDOOR_ALARM_PRODUCT_KEYS:
        descriptions.append(
            HotataBinaryDescription(
                key="AlarmState",
                name="报警",
                device_class=BinarySensorDeviceClass.SAFETY,
            )
        )
    descriptions.append(
        HotataBinaryDescription(
            key="TamperAlarm",
            name="防拆",
            device_class=BinarySensorDeviceClass.TAMPER,
        )
    )
    for description in descriptions:
        if has_property(device, description.key):
            yield HotataPropertyBinarySensor(
                coordinator, device, description
            )


class HotataConnectivitySensor(HotataEntity, BinarySensorEntity):
    """Report whether the cloud device is online."""

    _attr_translation_key = "online_status"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HotataCoordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = entity_identity(device, "connectivity")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool | None:
        return self.current_device.online


class HotataPropertyBinarySensor(HotataEntity, BinarySensorEntity):
    """Expose one reported boolean/alarm property."""

    entity_description: HotataBinaryDescription

    def __init__(
        self, coordinator: HotataCoordinator, device, description
    ) -> None:
        super().__init__(coordinator, device)
        self.entity_description = description
        self._attr_unique_id = entity_identity(device, description.key)

    @property
    def is_on(self) -> bool:
        return (
            self.property_value(self.entity_description.key)
            in self.entity_description.active_values
        )
