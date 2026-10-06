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

from .const import (
    ADVANCED_AIRER_PRODUCT_KEYS,
    BROADCAST_PRODUCT_KEYS,
    DOMAIN,
    MODEL_AIR_DRYING,
    MODEL_DISINFECTION,
    MODEL_HOT_DRYING,
    SOCKET_PRODUCT_KEYS,
    WALL_SWITCH_PRODUCT_KEYS,
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


@dataclass(frozen=True, kw_only=True)
class HotataSwitchDescription(SwitchEntityDescription):
    """Describe one boolean property."""

    supported_models: frozenset[int] | None = None


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
        supported_models=MODEL_DISINFECTION,
    ),
    HotataSwitchDescription(
        key="AirDryingSwitch",
        translation_key="air_drying",
        icon="mdi:fan",
        supported_models=MODEL_AIR_DRYING,
    ),
    HotataSwitchDescription(
        key="DryingSwitch",
        translation_key="drying",
        icon="mdi:heat-wave",
        supported_models=MODEL_HOT_DRYING,
    ),
    HotataSwitchDescription(
        key="IonsSwitch",
        translation_key="ions",
        icon="mdi:atom",
        # Negative ions ship only on the full-featured flagship (model 0);
        # lesser models declare the property in TSL but lack the hardware.
        supported_models=MODEL_HOT_DRYING,
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
    """TSL presence + product-family capability gating.

    Two product families describe their hardware differently, so they need
    different gates (issue #11):

    - standard airers (``AIRER_PRODUCT_KEYS``) publish a model code
      (``DeviceModelType`` 0-3). Their TSL is a product-line template that
      lists every possible function, and the report stream mirrors that
      template rather than the fitted hardware — verified on a model-2 device
      that reports DryingSwitch/AirDryingSwitch/IonsSwitch it does not have.
      The model table is therefore the only authority on hardware.
    - advanced airers (``ADVANCED_AIRER_PRODUCT_KEYS``) do not publish the
      model code at all, and their TSL is per-model accurate (59 properties
      instead of 29). Applying the model gate to them suppressed real
      switches — the D-3072S lost its disinfection switch. They are gated on
      declaration plus an actual report instead.

    Where the model code is absent AND the family is not the advanced one,
    there is nothing to check the table against. Creating the entity would be
    a guess, and entities are never removed once added, so a wrong guess is
    permanent (that guess was v4.0.8's regression). The gap is surfaced with
    a warning instead.
    """
    if not has_property(device, description.key):
        return False
    if description.supported_models is None:
        return True
    if device.product_key in ADVANCED_AIRER_PRODUCT_KEYS:
        # No model code on this family: trust declaration + an actual report.
        return description.key in device.properties
    model = property_value(device, "DeviceModelType")
    if model is None:
        _LOGGER.warning(
            "Capability %s: device %s does not report DeviceModelType; "
            "cannot confirm hardware support, entity not created. Please "
            "report this at the integration's issue tracker.",
            description.key,
            device.device_name or device.iot_id,
        )
        return False
    try:
        return int(model) in description.supported_models
    except (TypeError, ValueError):
        _LOGGER.warning(
            "Capability %s: device %s reports an unparseable DeviceModelType "
            "(%r); entity not created. Please report this at the "
            "integration's issue tracker.",
            description.key,
            device.device_name or device.iot_id,
            model,
        )
        return False


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
