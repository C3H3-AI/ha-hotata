"""Bit-table tests for airer capability resolution.

The reference reader and table come from the vendor's own device page, which
this suite pins with recorded values:

* ``subPackages/smarkairer`` (mini-program) defines ``FUN_INDEX`` and
  ``ifSupportFun(list, idx) === list.substr(list.length - idx, 1) === "1"`` —
  count from the RIGHT, 1-based.
* A D-3072S (pk ``a1abYBCSVlV``, a unit shared to this account) reports
  ``ModelFunctionList = "00000000000000000011111100110000"`` together with
  ``LightBrightness = 50``, ``BestPickUpPositionSwitch = 1`` and *no*
  disinfection/drying hardware — decoded below as light + disinfection +
  brightness + custom route, and no air drying / drying.
* A standard airer (pk ``a1kM9JAZ7aQ``) publishes no function list at all and
  reports ``DeviceModelType = 2`` (= 照明_消毒).

Run from anywhere:  python3 tests/test_bit_table.py
"""
import sys
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

import ha_stub  # noqa: E402

PKG = REPO_ROOT / "custom_components"
sys.path.insert(0, str(PKG))
_pkg = types.ModuleType("hotata")
_pkg.__path__ = [str(PKG / "hotata")]
sys.modules.setdefault("hotata", _pkg)

from hotata.capabilities import (  # noqa: E402
    CAP_AIR_DRYING,
    CAP_BRIGHTNESS,
    CAP_CCT_LIGHT,
    CAP_DISINFECTION,
    CAP_DRYING,
    CAP_IONS,
    CAP_LIGHT,
    CAP_NATURAL_WIND,
    CAP_NIGHT_LIGHT,
    FUN_INDEX,
    from_function_list,
    from_model_type,
    resolve,
    supports,
)
from hotata.models import HotataDevice  # noqa: E402
from hotata.switch import _switches_for_device  # noqa: E402

R_PASS = 0
R_FAIL = []

# Live value from the D-3072S.
D3072S_FUNCTION_LIST = "00000000000000000011111100110000"
ALL_FOUR = "00000000000000000000000011110000"  # light+disinfect+air_dry+drying


def check(label, got, want):
    global R_PASS
    if got == want:
        R_PASS += 1
    else:
        R_FAIL.append(f"{label}: got {got!r}, want {want!r}")


def test_supports_counts_from_the_right():
    """Index 5 is the fifth character from the end, not from the start."""
    check("idx 1 of '00001'", supports("00001", 1), True)
    check("idx 5 of '00001'", supports("00001", 5), False)
    check("idx 5 of '10000'", supports("10000", 5), True)
    check("index 0 is invalid", supports("11111", 0), False)
    check("string shorter than index", supports("111", 5), False)
    check("empty string", supports("", 5), False)
    check("None", supports(None, 5), False)
    check("only '1' counts", supports("00002", 1), False)


def test_fun_index_matches_the_vendor_table():
    check("LIGHT index", FUN_INDEX[CAP_LIGHT], 5)
    check("Disinfect index", FUN_INDEX[CAP_DISINFECTION], 6)
    check("AIR_DRY index", FUN_INDEX[CAP_AIR_DRYING], 7)
    check("DRYING index", FUN_INDEX[CAP_DRYING], 8)
    check("brightness index", FUN_INDEX[CAP_BRIGHTNESS], 10)
    check("night light index", FUN_INDEX[CAP_NIGHT_LIGHT], 22)
    check("cct index", FUN_INDEX[CAP_CCT_LIGHT], 23)
    check("natural wind index", FUN_INDEX[CAP_NATURAL_WIND], 30)


def test_d3072s_decodes_to_light_disinfection_brightness():
    """The recorded D-3072S value decodes exactly as the vendor app renders it."""
    caps = from_function_list(D3072S_FUNCTION_LIST)
    check("source", caps.source, "ModelFunctionList")
    check("has light", caps.has(CAP_LIGHT), True)
    check("has disinfection", caps.has(CAP_DISINFECTION), True)
    check("has brightness", caps.has(CAP_BRIGHTNESS), True)
    check("lacks air drying", caps.has(CAP_AIR_DRYING), False)
    check("lacks drying", caps.has(CAP_DRYING), False)
    check("lacks ions", caps.has(CAP_IONS), False)
    check("lacks night light", caps.has(CAP_NIGHT_LIGHT), False)
    check("raw kept verbatim", caps.raw, D3072S_FUNCTION_LIST)


def test_all_four_bits_set():
    caps = from_function_list(ALL_FOUR)
    for name in (CAP_LIGHT, CAP_DISINFECTION, CAP_AIR_DRYING, CAP_DRYING):
        check(f"all-four has {name}", caps.has(name), True)


def test_short_or_bogus_values_are_rejected():
    check("too short", from_function_list("00011110000"), None)
    check("not bits", from_function_list("x" * 32), None)
    check("not a string", from_function_list(240), None)
    check("None", from_function_list(None), None)
    check("empty", from_function_list(""), None)


def test_model_type_mapping():
    check("model 0 light", from_model_type(0).has(CAP_LIGHT), True)
    check("model 0 drying", from_model_type(0).has(CAP_DRYING), True)
    check("model 1 light only (drying)", from_model_type(1).has(CAP_DRYING), False)
    check("model 1 light only (light)", from_model_type(1).has(CAP_LIGHT), True)
    check("model 2 disinfection", from_model_type(2).has(CAP_DISINFECTION), True)
    check("model 2 air drying", from_model_type(2).has(CAP_AIR_DRYING), False)
    check("model 3 air drying", from_model_type(3).has(CAP_AIR_DRYING), True)
    check("model 3 drying", from_model_type(3).has(CAP_DRYING), False)
    check("model 0 ions (legacy behaviour)", from_model_type(0).has(CAP_IONS), True)
    check("model 2 ions", from_model_type(2).has(CAP_IONS), False)
    check("model 99 unknown", from_model_type(99), None)
    check("model 'x9' unknown", from_model_type("x9"), None)
    check("model None unknown", from_model_type(None), None)
    check("model '2' string works", from_model_type("2").has(CAP_DISINFECTION), True)


def make_device(properties, product_key="a1kM9JAZ7aQ", tsl_ids=()):
    return HotataDevice(
        iot_id="dev1",
        name="airer",
        product_key=product_key,
        device_name="airer",
        online=True,
        raw={"productName": "X"},
        properties=dict(properties),
        thing_model={"properties": [{"identifier": i} for i in tsl_ids]},
    )


class FakeCoordinator:
    def __init__(self, device):
        self.data = {device.iot_id: device}


def switch_keys(device):
    co = FakeCoordinator(device)
    return [e.entity_description.key for e in _switches_for_device(co, device)]


TSL_AIRER = (
    "PowerSwitch",
    "DisinfectionSwitch",
    "AirDryingSwitch",
    "DryingSwitch",
    "IonsSwitch",
    "DeviceModelType",
    "ModelFunctionList",
)


def test_function_list_wins_over_model_type():
    """A device publishing both is read the way the vendor app reads it."""
    caps = resolve(
        make_device(
            {
                "ModelFunctionList": D3072S_FUNCTION_LIST,
                # DeviceModelType 0 would allow drying; the list says otherwise.
                "DeviceModelType": 0,
            }
        )
    )
    check("source is the function list", caps.source, "ModelFunctionList")
    check("drying refused", caps.has(CAP_DRYING), False)


def test_model_type_is_the_fallback():
    caps = resolve(make_device({"DeviceModelType": 2}))
    check("source is the model type", caps.source, "DeviceModelType")
    check("disinfection", caps.has(CAP_DISINFECTION), True)


def test_nothing_published_is_unknown():
    check("no capability fields", resolve(make_device({})), None)


def test_d3072s_loses_drying_switches():
    """End to end: the recorded D-3072S switch set."""
    device = make_device(
        {
            "PowerSwitch": 1,
            "LightSwitch": 0,
            "DisinfectionSwitch": 0,
            "DryingSwitch": 0,
            "AirDryingSwitch": 0,
            "IonsSwitch": 0,
            "ModelFunctionList": D3072S_FUNCTION_LIST,
        },
        product_key="a1abYBCSVlV",
        tsl_ids=TSL_AIRER,
    )
    keys = switch_keys(device)
    check("power created", "PowerSwitch" in keys, True)
    check("disinfection created", "DisinfectionSwitch" in keys, True)
    check("drying suppressed", "DryingSwitch" in keys, False)
    check("air drying suppressed", "AirDryingSwitch" in keys, False)
    check("ions suppressed", "IonsSwitch" in keys, False)


def test_standard_model_2_still_exact():
    device = make_device(
        {
            "PowerSwitch": 1,
            "DisinfectionSwitch": 0,
            "DryingSwitch": 0,
            "AirDryingSwitch": 0,
            "IonsSwitch": 0,
            "DeviceModelType": 2,
        },
        product_key="a1kM9JAZ7aQ",
        tsl_ids=TSL_AIRER,
    )
    keys = switch_keys(device)
    check("model 2 disinfection", "DisinfectionSwitch" in keys, True)
    check("model 2 no drying", "DryingSwitch" in keys, False)
    check("model 2 no air drying", "AirDryingSwitch" in keys, False)
    check("model 2 no ions", "IonsSwitch" in keys, False)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"bit table: {R_PASS} passed, {len(R_FAIL)} failed")
    for line in R_FAIL:
        print("  FAIL", line)
    return 1 if R_FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
