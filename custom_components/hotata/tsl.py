"""Reading Aliyun thing-model (TSL) declarations.

A TSL model is a JSON document listing the properties, services and events a
product can report. We use it to decide which entities a device really
supports and to label enum values, instead of hard-coding per-product tables.
"""

from __future__ import annotations

from typing import Any

from .models import HotataDevice

_WRITABLE_MARKER = "w"


def property_definition(
    device: HotataDevice, identifier: str
) -> dict[str, Any] | None:
    """Look up the declaration of one property, if the model has it."""
    for entry in device.thing_model.get("properties", []):
        if isinstance(entry, dict) and entry.get("identifier") == identifier:
            return entry
    return None


def data_specs(device: HotataDevice, identifier: str) -> dict[str, Any]:
    """Return the ``dataType.specs`` block of one property."""
    definition = property_definition(device, identifier) or {}
    data_type = definition.get("dataType")
    if not isinstance(data_type, dict):
        return {}
    specs = data_type.get("specs")
    return specs if isinstance(specs, dict) else {}


def enum_values(device: HotataDevice, identifier: str) -> dict[str, Any]:
    """Map human-readable option labels to their on-the-wire values.

    Aliyun encodes enums either under ``specs.enum`` or directly as ``specs``,
    with the wire value as key and the label as value.
    """
    specs = data_specs(device, identifier)
    if not specs:
        return {}

    raw = specs.get("enum")
    candidates = raw if isinstance(raw, dict) else specs

    options: dict[str, Any] = {}
    for wire_value, label in candidates.items():
        if isinstance(label, (dict, list)):
            continue
        name = str(label or wire_value)
        # Keep duplicated labels distinct so nothing silently overwrites.
        if name in options:
            name = f"{name} ({wire_value})"
        try:
            options[name] = int(wire_value)
        except (TypeError, ValueError):
            options[name] = wire_value
    return options


def is_writable_property(device: HotataDevice, identifier: str) -> bool:
    """True when the model grants write access to the property."""
    definition = property_definition(device, identifier)
    if definition is None:
        return False
    access = str(
        definition.get("accessMode") or definition.get("access") or ""
    ).lower()
    return _WRITABLE_MARKER in access
