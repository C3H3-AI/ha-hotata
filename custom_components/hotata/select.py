"""Enum select entities derived from device TSL declarations."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .capabilities import (
    CAP_AIR_DRYING,
    CAP_DRYING,
    CAP_DRYING_MODE,
    CAP_LIGHT_MODE,
    supported,
)
from .const import (
    BROADCAST_PRODUCT_KEYS,
    CAMERA_PRODUCT_KEYS,
    CLOTHES_CARE_PRODUCT_KEYS,
    DOMAIN,
    FAMILY_AIRER,
    product_family,
)
from .coordinator import HotataCoordinator
from .entity import (
    HotataEntity,
    async_setup_dynamic_entities,
    entity_identity,
    remove_stale_entity,
)
from .tsl import enum_values, is_writable_property

_LOGGER = logging.getLogger(__name__)

ENUM_PROPERTIES = {
    **{key: ("WorkMode", "工作模式") for key in CLOTHES_CARE_PRODUCT_KEYS},
    **{key: ("StreamVideoQuality", "视频清晰度") for key in CAMERA_PRODUCT_KEYS},
    **{key: ("ArmMode", "布防模式") for key in BROADCAST_PRODUCT_KEYS},
}
EXTRA_ENUM_PROPERTIES = {
    **{
        key: (("ClothingType", "衣物类型"),)
        for key in CLOTHES_CARE_PRODUCT_KEYS
    },
    **{
        key: (("MotionDetectSensitivity", "移动侦测灵敏度"),)
        for key in CAMERA_PRODUCT_KEYS
    },
}

# Airer enum controls discovered from the device's own TSL: the identifier must
# be declared writable and carry an enum. Where the vendor's FUN_INDEX table has
# a bit for the function a property drives, that bit gets the last word — the
# D-3072S declares 烘干/风干运行时间 in its product-line TSL while its function
# list has no drying bit, i.e. a declaration is a template, not hardware. The
# entries without a capability (人体感应) have no bit at all; the device's own
# declaration is then the only evidence, exactly as for the best-position
# switches in switch.py.
AIRER_ENUM_PROPERTIES: tuple[tuple[str, str, str | None], ...] = (
    ("LightMode", "照明模式", CAP_LIGHT_MODE),
    ("DryingMode", "烘干模式", CAP_DRYING_MODE),
    ("DryingRunningTime", "烘干运行时间", CAP_DRYING),
    ("AirDryingRunningTime", "风干打开时间", CAP_AIR_DRYING),
    ("BodyInductionSensitivitySwitch", "人体感应灵敏度", None),
)


def _entities(coordinator: HotataCoordinator, device) -> Iterable[SelectEntity]:
    definitions = []
    primary = ENUM_PROPERTIES.get(device.product_key)
    if primary:
        definitions.append(primary)
    definitions.extend(EXTRA_ENUM_PROPERTIES.get(device.product_key, ()))
    for identifier, name in definitions:
        values = enum_values(device, identifier)
        if values and is_writable_property(device, identifier):
            yield HotataEnumSelect(coordinator, device, identifier, name, values)

    # Airers additionally get the enum controls their own model declares. A
    # capability that cannot be resolved creates the entity rather than guessing
    # it away; one the device states it lacks removes an entry an earlier
    # version (or a poll that ran before the first report) left behind.
    if product_family(device.product_key) not in (None, FAMILY_AIRER):
        return
    for identifier, name, capability in AIRER_ENUM_PROPERTIES:
        if not is_writable_property(device, identifier):
            continue
        values = enum_values(device, identifier)
        if not values:
            continue
        if not supported(device, capability):
            remove_stale_entity(coordinator.hass, device, "select", identifier)
            continue
        yield HotataEnumSelect(coordinator, device, identifier, name, values)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create and dynamically discover selects."""
    coordinator: HotataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda device: _entities(coordinator, device),
    )


class HotataEnumSelect(HotataEntity, SelectEntity):
    def __init__(
        self,
        coordinator: HotataCoordinator,
        device,
        identifier: str,
        name: str,
        values: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, device)
        self._identifier = identifier
        self._values = values
        self._attr_name = name
        self._attr_options = list(values)
        self._attr_unique_id = entity_identity(device, identifier)

    @property
    def current_option(self) -> str | None:
        current = self.property_value(self._identifier)
        return next(
            (
                option
                for option, wire_value in self._values.items()
                if str(wire_value) == str(current)
            ),
            None,
        )

    async def async_select_option(self, option: str) -> None:
        await self.async_set_property(self._identifier, self._values[option])
