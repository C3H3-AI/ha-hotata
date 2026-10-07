"""Switch entities for Hotata devices (all product lines)."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .capabilities import (
    CAP_AIR_DRYING,
    CAP_DISINFECTION,
    CAP_DRYING,
    CAP_IONS,
    resolve,
)
from .const import (
    ADVANCED_AIRER_PRODUCT_KEYS,
    BROADCAST_PRODUCT_KEYS,
    DOMAIN,
    FAMILY_AIRER,
    SOCKET_PRODUCT_KEYS,
    WALL_SWITCH_PRODUCT_KEYS,
    product_family,
)
from .coordinator import HotataCoordinator
from .entity import (
    HotataEntity,
    async_setup_dynamic_entities,
    entity_identity,
    has_property,
    property_value,
)

_LOGGER = logging.getLogger(__name__)

# The factory runs on every coordinator update, so the "cannot tell" warning is
# emitted once per device and capability instead of on every poll.
_UNRESOLVED_WARNED: set[tuple[str, str]] = set()


@dataclass(frozen=True, kw_only=True)
class HotataSwitchDescription(SwitchEntityDescription):
    """Describe one boolean property."""

    #: Optional airer capability this switch belongs to (see capabilities.py).
    #: None means the switch is not capability-gated.
    capability: str | None = None


AIRER_SWITCHES: tuple[HotataSwitchDescription, ...] = (
    HotataSwitchDescription(
        key="PowerSwitch",
        translation_key="power",
        icon="mdi:power",
        device_class=SwitchDeviceClass.SWITCH,
    ),
    HotataSwitchDescription(
        key="DisinfectionSwitch",
        translation_key="disinfection",
        icon="mdi:shield-sun-outline",
        capability=CAP_DISINFECTION,
    ),
    HotataSwitchDescription(
        key="AirDryingSwitch",
        translation_key="air_drying",
        icon="mdi:fan",
        capability=CAP_AIR_DRYING,
    ),
    HotataSwitchDescription(
        key="DryingSwitch",
        translation_key="drying",
        icon="mdi:heat-wave",
        capability=CAP_DRYING,
    ),
    HotataSwitchDescription(
        key="IonsSwitch",
        translation_key="ions",
        icon="mdi:atom",
        capability=CAP_IONS,
    ),
)

ADVANCED_AIRER_SWITCHES = (
    HotataSwitchDescription(
        key="BodyInductionSwitch", name="人体感应", icon="mdi:motion-sensor"
    ),
    HotataSwitchDescription(
        key="SolarTraceSwitch", name="太阳追踪", icon="mdi:white-balance-sunny"
    ),
    HotataSwitchDescription(
        key="SunTraceSwitch", name="智能晾晒", icon="mdi:weather-sunny"
    ),
    HotataSwitchDescription(
        key="VoiceInteractionSwitch", name="语音交互", icon="mdi:microphone"
    ),
    HotataSwitchDescription(
        key="BestPickUpPositionSwitch",
        name="最佳取衣位",
        icon="mdi:human-handsup",
    ),
    HotataSwitchDescription(
        key="BestSunCurePositionSwitch",
        name="最佳晾晒位",
        icon="mdi:sun-angle-outline",
    ),
)


def _airer_model_supported(device, description: HotataSwitchDescription) -> bool:
    """Presence check plus capability resolution.

    A capability is decided by ``capabilities.resolve``, which reads the
    device's own ``ModelFunctionList`` bit string first (the value the vendor's
    app renders its buttons from) and falls back to ``DeviceModelType``. Both
    the TSL and the property report are product-line templates rather than
    hardware — verified on a model-2 device that reports DryingSwitch,
    AirDryingSwitch and IonsSwitch it does not have — so neither is evidence on
    its own; only the capability fields are.

    When a device publishes neither field there is nothing to check against,
    and the rule is to create rather than guess away ("unknown means allow"),
    which is also what the other public Hotata integration converged on
    (chliny/ha-hotata). The case is logged so it stays visible.
    """
    if not has_property(device, description.key):
        return False
    if description.capability is None:
        return True
    capabilities = resolve(device)
    if capabilities is None:
        token = (device.iot_id, description.capability or description.key)
        if token not in _UNRESOLVED_WARNED:
            _UNRESOLVED_WARNED.add(token)
            _LOGGER.warning(
                "Capability %s: device %s publishes neither ModelFunctionList "
                "nor a usable DeviceModelType, so the entity is created. "
                "Please share diagnostics at the integration's issue tracker.",
                description.key,
                device.device_name or device.iot_id,
            )
        return True
    return capabilities.has(description.capability)


def _switches_for_device(
    coordinator: HotataCoordinator, device
) -> Iterable[SwitchEntity]:
    seen: set[str] = set()

    def add(description: HotataSwitchDescription) -> HotataSwitch | None:
        if description.key in seen or not _airer_model_supported(
            device, description
        ):
            return None
        seen.add(description.key)
        return HotataSwitch(coordinator, device, description)

    # Airer switches belong to airers. A device whose product key is a known
    # non-airer family never gets them, even if its product-line TSL template
    # happens to declare the identifier. Unknown keys keep the legacy path.
    family = product_family(device.product_key)
    if family in (None, FAMILY_AIRER):
        if family is None:
            _LOGGER.debug(
                "Unknown product key %s: falling back to property gating",
                device.product_key,
            )
        for description in AIRER_SWITCHES:
            entity = add(description)
            if entity:
                yield entity
    if device.product_key in ADVANCED_AIRER_PRODUCT_KEYS:
        for description in ADVANCED_AIRER_SWITCHES:
            entity = add(description)
            if entity:
                yield entity
    if device.product_key in SOCKET_PRODUCT_KEYS:
        for description in (
            HotataSwitchDescription(
                key="ChildLockSwitch", name="童锁", icon="mdi:lock-outline"
            ),
            HotataSwitchDescription(
                key="LedSwitch", name="指示灯", icon="mdi:led-on"
            ),
            HotataSwitchDescription(
                key="FailureProtectionSwitch",
                name="断电保护",
                icon="mdi:shield-bolt-outline",
            ),
        ):
            entity = add(description)
            if entity:
                yield entity
    if device.product_key in WALL_SWITCH_PRODUCT_KEYS:
        for index in range(1, 4):
            description = HotataSwitchDescription(
                key=f"PowerSwitch_{index}",
                name=f"开关 {index}",
                icon="mdi:light-switch",
            )
            entity = add(description)
            if entity:
                yield entity
    if device.product_key in BROADCAST_PRODUCT_KEYS:
        for description in (
            HotataSwitchDescription(
                key="DoorBellSwitch", name="门铃", icon="mdi:doorbell"
            ),
            HotataSwitchDescription(
                key="VoiceMessageSwitch",
                name="语音消息",
                icon="mdi:message-audio",
            ),
        ):
            entity = add(description)
            if entity:
                yield entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create and dynamically discover switches."""
    coordinator: HotataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda device: _switches_for_device(coordinator, device),
    )


class HotataSwitch(HotataEntity, SwitchEntity):
    """Control one boolean device property."""

    entity_description: HotataSwitchDescription

    def __init__(
        self, coordinator: HotataCoordinator, device, description
    ) -> None:
        super().__init__(coordinator, device)
        self.entity_description = description
        self._attr_unique_id = entity_identity(device, description.key)

    @property
    def is_on(self) -> bool:
        return self.property_value(self.entity_description.key) == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_set_property(self.entity_description.key, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_set_property(self.entity_description.key, 0)
