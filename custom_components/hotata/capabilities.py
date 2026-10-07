"""Resolve which optional airer functions a device really has.

The vendor's own app decides this from the device's ``ModelFunctionList``
property — a text bit string — and the table plus reader below are copied from
the code that actually builds the device page in the mini-program airer
subpackage (``subPackages/smarkairer``, ``FUN_INDEX`` / ``ifSupportFun``):

    ifSupportFun(list, idx) === list.substr(list.length - idx, 1) === "1"

so the index counts from the RIGHT, 1-based: index 5 is the fifth character
from the end of the string. Everything the app renders conditionally (照明,
除菌, 风干, 烘干, 亮度, 夜灯, 色温, 自然风, 双杆, …) is read this way;
升降/电源/暂停/设置 are unconditional.

Devices that publish no ``ModelFunctionList`` fall back to ``DeviceModelType``
(0-3), whose values encode the same four core functions, and older firmware
used the cloud's ``v1.0/app/device/gethangerinfo`` table. See
``tests/test_bit_table.py`` for the decoded reference values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Capability names used by the entity descriptions.
CAP_LIGHT = "light"
CAP_DISINFECTION = "disinfection"
CAP_AIR_DRYING = "air_drying"
CAP_DRYING = "drying"
CAP_IONS = "ions"
CAP_BRIGHTNESS = "light_brightness"
CAP_NIGHT_LIGHT = "night_light"
CAP_CCT_LIGHT = "cct_light"
CAP_NATURAL_WIND = "natural_wind"
CAP_DOUBLE_POLE = "double_pole"
CAP_LIGHT_MODE = "light_mode"
CAP_DRYING_MODE = "drying_mode"

#: ``FUN_INDEX`` from the mini-program: capability name -> position counted
#: from the right of the ModelFunctionList string.
FUN_INDEX: dict[str, int] = {
    CAP_LIGHT: 5,
    CAP_DISINFECTION: 6,
    CAP_AIR_DRYING: 7,
    CAP_DRYING: 8,
    CAP_BRIGHTNESS: 10,
    "route_diy1": 14,
    "route_diy2": 15,
    "sensor_night_light": 17,
    "sensor_light": 18,
    "voice": 20,
    CAP_NIGHT_LIGHT: 22,
    CAP_CCT_LIGHT: 23,
    CAP_DOUBLE_POLE: 24,
    "solar_trace": 25,
    "t_pole": 26,
    CAP_LIGHT_MODE: 27,
    CAP_DRYING_MODE: 28,
    "voice_broadcast": 29,
    CAP_NATURAL_WIND: 30,
    "sun_trace": 31,
    "custom_time": 32,
}

#: ``DeviceModelType`` semantics from the cloud TSL, expressed as capabilities.
#: 0 = 照明_消毒_风干_烘干, 1 = 照明, 2 = 照明_消毒, 3 = 照明_消毒_风干.
#: ``ions`` is not part of the vendor's capability model at all (it has no
#: FUN_INDEX bit and is absent from the app's property subscription); it is
#: kept for model 0 only, which is what this integration has always done.
MODEL_TYPE_FUNCTIONS: dict[int, frozenset[str]] = {
    0: frozenset({CAP_LIGHT, CAP_DISINFECTION, CAP_AIR_DRYING, CAP_DRYING, CAP_IONS}),
    1: frozenset({CAP_LIGHT}),
    2: frozenset({CAP_LIGHT, CAP_DISINFECTION}),
    3: frozenset({CAP_LIGHT, CAP_DISINFECTION, CAP_AIR_DRYING}),
}

#: Shortest string that can still express the four core functions (index 8).
MIN_FUNCTION_LIST_LENGTH = max(FUN_INDEX.values())


def supports(bit_string: str | None, index: int) -> bool:
    """Return whether position ``index`` (counted from the right) is set.

    This is a faithful port of the mini-program's ``ifSupportFun``, including
    its length guard: a string shorter than the index reports "not supported".
    """
    if not bit_string or index < 1 or len(bit_string) < index:
        return False
    return bit_string[len(bit_string) - index] == "1"


@dataclass(frozen=True)
class AirerCapabilities:
    """The optional functions a device was determined to have."""

    source: str
    functions: frozenset[str]
    raw: Any = None

    def has(self, capability: str | None) -> bool:
        if capability is None:
            return True
        return capability in self.functions


def _looks_like_bit_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < MIN_FUNCTION_LIST_LENGTH:
        return None
    if set(text) - {"0", "1"}:
        return None
    return text


def from_function_list(value: Any) -> AirerCapabilities | None:
    """Build capabilities from a reported ``ModelFunctionList`` value."""
    bit_string = _looks_like_bit_string(value)
    if bit_string is None:
        return None
    return AirerCapabilities(
        source="ModelFunctionList",
        functions=frozenset(
            name for name, index in FUN_INDEX.items() if supports(bit_string, index)
        ),
        raw=bit_string,
    )


def from_model_type(value: Any) -> AirerCapabilities | None:
    """Build capabilities from a reported ``DeviceModelType`` (0-3)."""
    if value is None:
        return None
    try:
        model = int(value)
    except (TypeError, ValueError):
        return None
    functions = MODEL_TYPE_FUNCTIONS.get(model)
    if functions is None:
        return None
    return AirerCapabilities(
        source="DeviceModelType", functions=functions, raw=model
    )


def _reported(device: Any, identifier: str) -> Any:
    properties = getattr(device, "properties", None)
    if not isinstance(properties, dict):
        return None
    value = properties.get(identifier)
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def resolve(device: Any) -> AirerCapabilities | None:
    """Determine a device's functions, or None when nothing is published.

    ``ModelFunctionList`` wins because it is what the vendor's own device page
    reads to build its buttons; ``DeviceModelType`` is the documented fallback
    for devices that publish no list. Returning None means "unknown" — callers
    create the entity rather than guess it away.
    """
    capabilities = from_function_list(_reported(device, "ModelFunctionList"))
    if capabilities is not None:
        return capabilities
    return from_model_type(_reported(device, "DeviceModelType"))
