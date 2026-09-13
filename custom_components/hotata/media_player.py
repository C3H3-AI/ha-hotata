"""Media player support for Hotata music devices."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MUSIC_PRODUCT_KEYS
from .coordinator import HotataCoordinator
from .entity import HotataEntity, async_setup_dynamic_entities, entity_identity

_LOGGER = logging.getLogger(__name__)


def _entities(coordinator: HotataCoordinator, device) -> Iterable[Any]:
    if device.product_key in MUSIC_PRODUCT_KEYS:
        yield HotataMusicPlayer(coordinator, device)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create and dynamically discover media players."""
    coordinator: HotataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda device: _entities(coordinator, device),
    )


class HotataMusicPlayer(HotataEntity, MediaPlayerEntity):
    _attr_name = "音乐"
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.VOLUME_SET
    )

    def __init__(self, coordinator: HotataCoordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = entity_identity(device, "music")

    @property
    def state(self) -> MediaPlayerState:
        value = self.property_value("playStatus")
        if value == 1:
            return MediaPlayerState.PLAYING
        if value == 0:
            return MediaPlayerState.PAUSED
        return MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float | None:
        value = self.property_value("volume")
        try:
            return max(0.0, min(1.0, float(value) / 100))
        except (TypeError, ValueError):
            return None

    @property
    def media_title(self) -> str | None:
        song = self.property_value("song")
        return (
            str(song.get("Name"))
            if isinstance(song, dict) and song.get("Name")
            else None
        )

    @property
    def media_artist(self) -> str | None:
        song = self.property_value("song")
        return (
            str(song.get("singer"))
            if isinstance(song, dict) and song.get("singer")
            else None
        )

    @property
    def media_duration(self) -> int | None:
        value = self.property_value("totalTime")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @property
    def media_position(self) -> int | None:
        value = self.property_value("progress")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "play_mode": self.property_value("playMode"),
            "bluetooth": self.property_value("bluetooth"),
            "scene_music": self.property_value("sceneMusic"),
        }

    async def async_media_play(self) -> None:
        await self.async_set_property("playStatus", 1)

    async def async_media_pause(self) -> None:
        await self.async_set_property("playStatus", 0)

    async def async_media_next_track(self) -> None:
        await self.async_set_property("switchMusic", 2)

    async def async_media_previous_track(self) -> None:
        await self.async_set_property("switchMusic", 1)

    async def async_set_volume_level(self, volume: float) -> None:
        await self.async_set_property("volume", round(volume * 100))
