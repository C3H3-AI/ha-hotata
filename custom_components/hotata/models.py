"""Device data structures shared across the integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Aliyun returns the same field under a few spellings depending on endpoint.
_IOT_ID_KEYS = ("iotId", "iotid", "iot_id")
_NAME_KEYS = (
    "nickName",
    "nickname",
    "name",
    "productName",
    "deviceName",
    "devicename",
)
_PRODUCT_KEY_KEYS = ("productKey", "productkey")
_DEVICE_NAME_KEYS = ("deviceName", "devicename")


def _first(value: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first present, non-empty value among the given keys."""
    for key in keys:
        candidate = value.get(key)
        if candidate is not None and candidate != "":
            return candidate
    return None


@dataclass(slots=True)
class HotataDevice:
    """One device visible to the configured account."""

    iot_id: str
    name: str
    product_key: str | None = None
    device_name: str | None = None
    owned: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)
    thing_model: dict[str, Any] = field(default_factory=dict)
    online: bool | None = None
    parent_iot_id: str | None = None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> HotataDevice:
        """Normalize one device record from the cloud device list."""
        iot_id = str(_first(payload, _IOT_ID_KEYS) or "")
        name = str(_first(payload, _NAME_KEYS) or iot_id)
        return cls(
            iot_id=iot_id,
            name=name,
            product_key=_first(payload, _PRODUCT_KEY_KEYS),
            device_name=_first(payload, _DEVICE_NAME_KEYS),
            owned=payload.get("owned"),
            raw=payload,
        )

    @property
    def is_gateway_child(self) -> bool:
        """True when this device was discovered beneath a gateway."""
        return self.parent_iot_id is not None
