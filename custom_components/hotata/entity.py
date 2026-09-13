"""Shared entity support (base class + dynamic entity factory)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN, NAME
from .coordinator import HotataCoordinator
from .models import HotataDevice


def _identity_part(value: str) -> str:
    """Convert an identity component to a readable entity-id fragment."""
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return slugify(words)


def entity_identity(device: HotataDevice, entity_name: str) -> str:
    """Return a stable and readable identity for one device entity."""
    device_id = device.device_name or device.iot_id
    return "_".join(
        (
            DOMAIN,
            _identity_part(str(device_id)),
            _identity_part(entity_name),
        )
    )


def property_value(device: HotataDevice, identifier: str) -> Any:
    """Return the value from an Aliyun property report."""
    value = device.properties.get(identifier)
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def has_property(device: HotataDevice, identifier: str) -> bool:
    """Return whether a property is reported or declared in the TSL."""
    if identifier in device.properties:
        return True
    properties = device.thing_model.get("properties", [])
    return any(
        isinstance(item, dict) and item.get("identifier") == identifier
        for item in properties
    )


def async_setup_dynamic_entities(
    entry,
    coordinator: HotataCoordinator,
    async_add_entities,
    factory: Callable[[HotataDevice], Iterable[Any]],
) -> None:
    """Add entities now and whenever polling discovers a new device."""
    known: set[str] = set()

    def add_new_entities() -> None:
        entities = []
        for device in (coordinator.data or {}).values():
            for entity in factory(device):
                unique_id = entity.unique_id
                if unique_id in known:
                    continue
                known.add(unique_id)
                entities.append(entity)
        if entities:
            async_add_entities(entities)

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


def _mac_connection(device: HotataDevice) -> tuple[str, str] | None:
    """Return a (mac, ...) registry connection if the device exposes a MAC.

    Aliyun's ``deviceName`` is the wifi MAC for Hotata devices, but some
    product lines use random identifiers — only accept a real 12-hex MAC.
    """
    candidates = (device.raw.get("mac"), device.device_name)
    for value in candidates:
        mac = str(value or "").replace(":", "").replace("-", "").lower()
        if len(mac) == 12 and all(c in "0123456789abcdef" for c in mac):
            return ("mac", ":".join(mac[i : i + 2] for i in range(0, 12, 2)))
    return None


class HotataEntity(CoordinatorEntity[HotataCoordinator]):
    """Base class for entities associated with one cloud device.

    Sits before the platform base class in the MRO. Availability follows the
    device's online state; control helpers write through the active identity
    and keep the poller in its fast window.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: HotataCoordinator, device: HotataDevice
    ) -> None:
        super().__init__(coordinator)
        self.device = device
        info = DeviceInfo(
            identifiers={(DOMAIN, device.iot_id)},
            name=device.name,
            manufacturer=NAME,
            model=(
                device.raw.get("productName")
                or device.product_key
                or "Smart Device"
            ),
            serial_number=device.device_name,
            via_device=(
                (DOMAIN, device.parent_iot_id)
                if device.parent_iot_id
                else None
            ),
        )
        mac = _mac_connection(device)
        if mac is not None:
            info["connections"] = {mac}
        self._attr_device_info = info

    @property
    def available(self) -> bool:
        """Expose controls while the device is online."""
        return self.current_device.online is not False

    @property
    def current_device(self) -> HotataDevice:
        """Return the latest device value from the coordinator."""
        return (self.coordinator.data or {}).get(
            self.device.iot_id, self.device
        )

    # helpers ---------------------------------------------------------------

    def property_value(self, identifier: str) -> Any:
        """Return a normalized property from the latest coordinator data."""
        return property_value(self.current_device, identifier)

    async def async_set_property(self, identifier: str, value: Any) -> None:
        """Write a property and refresh state shortly after."""
        await self.coordinator.account.api.async_set_property(
            self.device.iot_id, identifier, value
        )
        await self._after_command()

    async def async_set_properties(self, items: dict[str, Any]) -> None:
        """Write multiple properties and refresh state shortly after."""
        await self.coordinator.account.api.async_set_properties(
            self.device.iot_id, items
        )
        await self._after_command()

    async def async_invoke_service(
        self, identifier: str, args: dict[str, Any] | None = None
    ) -> Any:
        """Invoke a device service and refresh state shortly after."""
        result = await self.coordinator.account.api.async_invoke_service(
            self.device.iot_id, identifier, args
        )
        await self._after_command()
        return result

    async def _after_command(self) -> None:
        """Bump the fast-poll window and refresh once right away."""
        self.coordinator.account.bump_active_window()
        await self.coordinator.async_request_refresh()
