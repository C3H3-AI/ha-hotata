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
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .capabilities import (
    CAP_AIR_DRYING,
    CAP_DISINFECTION,
    CAP_DRYING,
    CAP_IONS,
    CAP_SOLAR_TRACE,
    CAP_SUN_TRACE,
    CAP_VOICE,
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
    remove_stale_entity,
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
        key="SolarTraceSwitch",
        name="太阳追踪",
        icon="mdi:white-balance-sunny",
        capability=CAP_SOLAR_TRACE,
    ),
    HotataSwitchDescription(
        key="SunTraceSwitch",
        name="智能晾晒",
        icon="mdi:weather-sunny",
        capability=CAP_SUN_TRACE,
    ),
    HotataSwitchDescription(
        key="VoiceInteractionSwitch",
        name="语音交互",
        icon="mdi:microphone",
        capability=CAP_VOICE,
    ),
    # 最佳取衣位 / 最佳晾晒位 have no FUN_INDEX bit; the device reports them
    # itself, so presence is the best evidence available.
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
        if not device.properties:
            # Nothing reported yet: a cold start before the first poll lands, or
            # an offline unit with an empty cloud shadow. Entities are created
            # once and never removed, so guessing here leaves spurious switches
            # behind forever (seen on 2026-10-07: 风干/烘干/负离子 came back on a
            # device whose first poll returned no properties, while the report
            # that arrived a moment later said DeviceModelType 2). Wait for the
            # report instead — the factory runs again on every update.
            _LOGGER.debug(
                "Capability %s: device %s has reported nothing yet, waiting",
                description.key,
                device.device_name or device.iot_id,
            )
            return False
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


class HotataInvertDirectionSwitch(HotataEntity, SwitchEntity):
    """Local preference: swap the rail travel direction (issue #15).

    Off (default, 4.0.14): 打开/展开 lowers the rail. On: 打开 raises it, which
    is what installations that read the travel the other way round want.
    Nothing is sent to the cloud — the flag is stored per device and only
    changes how the cover reports and commands its position.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "行程反转"
    _attr_icon = "mdi:swap-vertical"

    def __init__(self, coordinator: HotataCoordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = entity_identity(device, "invert_direction")

    @property
    def available(self) -> bool:
        """A local preference: it stays usable while the device is offline."""
        return True

    @property
    def is_on(self) -> bool:
        return bool(
            self.coordinator.runtime(self.device.iot_id).invert_direction
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_direction(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_direction(False)

    async def _async_set_direction(self, value: bool) -> None:
        await self.coordinator.runtime(
            self.device.iot_id
        ).async_set_invert_direction(value)
        self.async_write_ha_state()
        # The cover reads the flag on its next state write, so ask for one now
        # instead of leaving the old position on screen until the next poll.
        # Coordinator refreshes are debounced, so repeated toggles coalesce.
        await self.coordinator.async_request_refresh()


def _switches_for_device(
    coordinator: HotataCoordinator, device
) -> Iterable[SwitchEntity]:
    seen: set[str] = set()

    def add(description: HotataSwitchDescription) -> HotataSwitch | None:
        if description.key in seen:
            return None
        if not _airer_model_supported(device, description):
            if description.capability is not None:
                resolved = resolve(device)
                if resolved is not None and not resolved.has(
                    description.capability
                ):
                    # The device says it lacks this function: an entry an
                    # earlier version created must not linger as unavailable.
                    remove_stale_entity(
                        coordinator.hass, device, "switch", description.key
                    )
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
    # Direction preference: only where there is a cover to reverse.
    if (
        family in (None, FAMILY_AIRER)
        and has_property(device, "MotorControlMode")
    ):
        yield HotataInvertDirectionSwitch(coordinator, device)
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
