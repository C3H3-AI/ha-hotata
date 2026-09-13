"""Light entities for Hotata devices."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ADVANCED_AIRER_PRODUCT_KEYS,
    DOMAIN,
    LIGHT_BAND_COLOR_PRODUCT_KEYS,
    LIGHT_BAND_PRODUCT_KEYS,
    PLANT_PRODUCT_KEYS,
)
from .coordinator import HotataCoordinator
from .entity import (
    HotataEntity,
    async_setup_dynamic_entities,
    entity_identity,
    has_property,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create and dynamically discover lights."""
    coordinator: HotataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda device: _lights_for_device(coordinator, device),
    )


def _lights_for_device(
    coordinator: HotataCoordinator, device
) -> Iterable[LightEntity]:
    if device.product_key in LIGHT_BAND_PRODUCT_KEYS:
        yield HotataLightBand(coordinator, device)
        return
    if device.product_key in PLANT_PRODUCT_KEYS and has_property(
        device, "LightingStatus"
    ):
        yield HotataOnOffLight(
            coordinator, device, "LightingStatus", "植物灯", "plant_light"
        )
        return
    if has_property(device, "LightSwitch"):
        if (
            device.product_key in ADVANCED_AIRER_PRODUCT_KEYS
            and has_property(device, "LightBrightness")
        ):
            yield HotataAdvancedAirerLight(coordinator, device)
        else:
            yield HotataOnOffLight(
                coordinator, device, "LightSwitch", "照明", "light"
            )
    if device.product_key in ADVANCED_AIRER_PRODUCT_KEYS:
        for identifier, name in (
            ("ApoleLightSwitch", "A 杆照明"),
            ("BpoleLightSwitch", "B 杆照明"),
            ("NightLightSwitch", "夜灯"),
        ):
            if has_property(device, identifier):
                yield HotataOnOffLight(coordinator, device, identifier, name)


class HotataOnOffLight(HotataEntity, LightEntity):
    """Control one boolean light property."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(
        self,
        coordinator: HotataCoordinator,
        device,
        identifier: str,
        name: str,
        translation_key: str | None = None,
    ) -> None:
        super().__init__(coordinator, device)
        self.identifier = identifier
        self._attr_name = name
        if translation_key:
            self._attr_translation_key = translation_key
        self._attr_unique_id = entity_identity(device, identifier)

    @property
    def is_on(self) -> bool:
        return self.property_value(self.identifier) == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_set_property(self.identifier, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_set_property(self.identifier, 0)


class HotataAdvancedAirerLight(HotataOnOffLight):
    """Dimmable light on a feature-rich clothes airer."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, coordinator: HotataCoordinator, device) -> None:
        super().__init__(coordinator, device, "LightSwitch", "照明", "light")

    @property
    def brightness(self) -> int | None:
        value = self.property_value("LightBrightness")
        try:
            return round(max(0, min(100, int(value))) * 255 / 100)
        except (TypeError, ValueError):
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            await self.async_invoke_service(
                "LightBrightnessControl",
                {"Brightness": round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)},
            )
            return
        await super().async_turn_on(**kwargs)


class HotataLightBand(HotataEntity, LightEntity):
    """White or color light band."""

    _attr_name = "灯带"
    _attr_min_color_temp_kelvin = 2700
    _attr_max_color_temp_kelvin = 6500

    def __init__(self, coordinator: HotataCoordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = entity_identity(device, "light_band")
        if device.product_key in LIGHT_BAND_COLOR_PRODUCT_KEYS:
            self._attr_supported_color_modes = {
                ColorMode.HS,
                ColorMode.COLOR_TEMP,
            }
        else:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}

    @property
    def color_mode(self) -> ColorMode:
        if self.current_device.product_key in LIGHT_BAND_COLOR_PRODUCT_KEYS:
            return ColorMode.HS
        return ColorMode.COLOR_TEMP

    @property
    def is_on(self) -> bool:
        return self.property_value("LightSwitch") == 1

    @property
    def brightness(self) -> int | None:
        value = self.property_value("Brightness")
        try:
            return round(max(0, min(100, int(value))) * 255 / 100)
        except (TypeError, ValueError):
            return None

    @property
    def color_temp_kelvin(self) -> int | None:
        value = self.property_value("ColorTemperature")
        try:
            return max(2700, min(6500, int(value)))
        except (TypeError, ValueError):
            return None

    @property
    def hs_color(self) -> tuple[float, float] | None:
        value = self.property_value("HSVColor")
        if not isinstance(value, dict):
            return None
        try:
            return float(value["Hue"]), float(value["Saturation"])
        except (KeyError, TypeError, ValueError):
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        items = {"LightSwitch": 1}
        if ATTR_BRIGHTNESS in kwargs:
            items["Brightness"] = round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            items["ColorTemperature"] = kwargs[ATTR_COLOR_TEMP_KELVIN]
        if ATTR_HS_COLOR in kwargs:
            hue, saturation = kwargs[ATTR_HS_COLOR]
            items["HSVColor"] = {
                "Hue": hue,
                "Saturation": saturation,
                "Value": round(kwargs.get(ATTR_BRIGHTNESS, 255) * 100 / 255),
            }
        await self.async_set_properties(items)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_set_property("LightSwitch", 0)
