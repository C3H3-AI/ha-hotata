"""Event entities for reported Hotata key and lock events."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_BUTTON_PRODUCT_KEYS, LOCK_PRODUCT_KEYS
from .coordinator import HotataCoordinator
from .entity import HotataEntity, async_setup_dynamic_entities, entity_identity

_LOGGER = logging.getLogger(__name__)

LOCK_EVENT_IDENTIFIERS = (
    "DoorOpenNotification",
    "DoorOpenNotification2",
    "RingDoorbellAlarm",
    "TamperAlarm",
    "TamperAlarm2",
    "LowElectricityAlarm",
    "HijackingAlarm",
    "ForbiddenAlarm",
    "ActiveDefenseAlarm",
    "AlarmEvent",
)


def _entities(coordinator: HotataCoordinator, device) -> Iterable[EventEntity]:
    if device.product_key in EVENT_BUTTON_PRODUCT_KEYS:
        yield HotataReportedEvent(coordinator, device, ("KeyEvent",), "按键事件")
    if device.product_key in LOCK_PRODUCT_KEYS:
        yield HotataReportedEvent(
            coordinator, device, LOCK_EVENT_IDENTIFIERS, "门锁事件"
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create and dynamically discover event entities."""
    coordinator: HotataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda device: _entities(coordinator, device),
    )


class HotataReportedEvent(HotataEntity, EventEntity):
    _attr_event_types = ["changed"]

    def __init__(
        self,
        coordinator: HotataCoordinator,
        device,
        identifiers: tuple[str, ...],
        name: str,
    ) -> None:
        super().__init__(coordinator, device)
        self._identifiers = identifiers
        self._last_values = {
            key: self.property_value(key)
            for key in identifiers
            if self.property_value(key) is not None
        }
        self._attr_name = name
        self._attr_unique_id = entity_identity(device, identifiers[0])

    def _handle_coordinator_update(self) -> None:
        for identifier in self._identifiers:
            value = self.property_value(identifier)
            if value is None:
                continue
            if (
                identifier in self._last_values
                and self._last_values[identifier] != value
            ):
                self._trigger_event(
                    "changed",
                    {"identifier": identifier, "value": value},
                )
            self._last_values[identifier] = value
        self.async_write_ha_state()
