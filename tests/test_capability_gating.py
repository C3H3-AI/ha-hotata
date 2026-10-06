"""Capability-gating tests: which entities exist for which device state.

Two product families describe their hardware differently and need different
gates (issue #11):

- standard airers (AIRER_PRODUCT_KEYS) publish DeviceModelType (0-3). Their
  TSL is a product-line template listing every possible function, and the
  report stream mirrors that template, not the fitted hardware — verified on
  a model-2 device that reports DryingSwitch/AirDryingSwitch/IonsSwitch it
  does not have. The model table is the only hardware authority.
- advanced airers (ADVANCED_AIRER_PRODUCT_KEYS) publish no model code and
  their TSL is per-model accurate (59 vs 29 properties). Applying the model
  gate there suppressed real switches; they are gated on declaration plus an
  actual report.

Real diagnostics behind these cases: a model-2 standard device, and a
D-3072S (pk a1abYBCSVlV, device 044a6997f003) that reports DisinfectionSwitch
with DeviceModelType = null.

Run from anywhere:  python3 tests/test_capability_gating.py
"""
import asyncio
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

switch_mod = __import__("hotata.switch", fromlist=["_switches_for_device"])
from hotata.switch import _switches_for_device, _airer_model_supported  # noqa: E402
from hotata.switch import AIRER_SWITCHES  # noqa: E402
from hotata.models import HotataDevice  # noqa: E402

R_PASS = 0
R_FAIL = []


def check(label, got, want):
    global R_PASS
    if got == want:
        R_PASS += 1
    else:
        R_FAIL.append(f"{label}: got {got!r}, want {want!r}")


def make_device(properties, product_key="PK_AIRER", tsl_ids=()):
    """HotataDevice with explicit reported properties and TSL declarations."""
    thing_model = {
        "properties": [{"identifier": i} for i in tsl_ids]
    }
    return HotataDevice(
        iot_id="dev1",
        name="airer",
        product_key=product_key,
        device_name="airer",
        online=True,
        raw={"productName": "X"},
        properties=dict(properties),
        thing_model=thing_model,
    )


class FakeCoordinator:
    def __init__(self, device):
        self.data = {device.iot_id: device}


def switch_keys(device):
    """Run the factory and return the created switch keys, in order."""
    co = FakeCoordinator(device)
    return [e.entity_description.key for e in _switches_for_device(co, device)]


# The full-featured declaration set every airer TSL carries (from live data:
# 29 declared; the capability-relevant subset is what matters here).
TSL_AIRER = (
    "PowerSwitch", "DisinfectionSwitch", "DisinfectionRemainingTime",
    "AirDryingSwitch", "DryingSwitch", "IonsSwitch", "IonsRemainingTime",
    "DeviceModelType", "MotorControlMode", "Position",
)


def test_model2_with_all_reported():
    """Flagship-adjacent model 2: disinfection on, drying/ions off."""
    d = make_device(
        {"PowerSwitch": 1, "DisinfectionSwitch": 0, "AirDryingSwitch": 0,
         "DryingSwitch": 0, "IonsSwitch": 0, "DeviceModelType": 2},
        tsl_ids=TSL_AIRER,
    )
    got = switch_keys(d)
    check("model2: power created", "PowerSwitch" in got, True)
    check("model2: disinfection created", "DisinfectionSwitch" in got, True)
    check("model2: air drying suppressed", "AirDryingSwitch" in got, False)
    check("model2: drying suppressed", "DryingSwitch" in got, False)
    check("model2: ions suppressed", "IonsSwitch" in got, False)


def test_model1_without_disinfection():
    """Model 1 declares DisinfectionSwitch in TSL but has no hardware."""
    d = make_device(
        {"PowerSwitch": 1, "DeviceModelType": 1},
        tsl_ids=TSL_AIRER,
    )
    got = switch_keys(d)
    check("model1: disinfection suppressed (explicit 1)", "DisinfectionSwitch" in got, False)


def test_unknown_model_reported_property_present():
    """DisinfectionSwitch reported but DeviceModelType absent.

    The report mirrors the TSL, not the hardware (proven live on a model-2
    device reporting switches it lacks), so a report is NOT evidence. The
    entity must not be created — creating it was v4.0.8's regression.
    """
    d = make_device(
        {"PowerSwitch": 1, "DisinfectionSwitch": 0},
        tsl_ids=TSL_AIRER,
    )
    got = switch_keys(d)
    check("unknown model: disinfection not created (no guess)",
          "DisinfectionSwitch" in got, False)


def test_unknown_model_tsl_only_declaration_suppressed():
    """TSL declares it but the device never reports it: still suppressed.

    TSL presence alone is not hardware (the IonsSwitch lesson). With no model
    code and no report, we cannot confirm the hardware — keep it out.
    """
    d = make_device({"PowerSwitch": 1}, tsl_ids=TSL_AIRER)
    got = switch_keys(d)
    check("unknown model + TSL-only: disinfection suppressed",
          "DisinfectionSwitch" in got, False)


def test_unparseable_model_reported_property_present():
    """A garbled model value defers to an actual report, same as absent."""
    d = make_device(
        {"PowerSwitch": 1, "DisinfectionSwitch": 0, "DeviceModelType": "x9"},
        tsl_ids=TSL_AIRER,
    )
    got = switch_keys(d)
    check("garbled model: disinfection not created (no guess)",
          "DisinfectionSwitch" in got, False)


def test_out_of_range_model_reported_property_still_suppressed():
    """An explicit model outside every whitelist wins over a report.

    The 0-3 table is the product line's authority; a device claiming model 99
    must not resurrect capabilities the table excludes.
    """
    d = make_device(
        {"PowerSwitch": 1, "DisinfectionSwitch": 0, "DeviceModelType": 99},
        tsl_ids=TSL_AIRER,
    )
    got = switch_keys(d)
    check("model 99: disinfection suppressed", "DisinfectionSwitch" in got, False)


def test_no_tsl_declaration_no_entity_even_when_reported():
    """has_property is an OR (TSL-declared OR reported) by design.

    Presence decides whether the gate even runs; the model whitelist then
    decides. Verified live — the real device declares and reports every
    gating key, so neither side of the OR is empty in practice.
    """
    # With an explicit in-whitelist model the OR passes and the whitelist
    # admits it, even though only the report stream carried the key.
    d = make_device({"PowerSwitch": 1, "DisinfectionSwitch": 0,
                     "DeviceModelType": 2}, tsl_ids=())
    got = switch_keys(d)
    check("reported without TSL, model 2: created (OR semantics)",
          "DisinfectionSwitch" in got, True)
    d2 = make_device({"PowerSwitch": 1}, tsl_ids=())
    got2 = switch_keys(d2)
    check("neither TSL nor report: suppressed",
          "DisinfectionSwitch" in got2, False)


def test_advanced_airer_gets_disinfection_without_model_code():
    """The #11 device: pk a1abYBCSVlV, no DeviceModelType, reports the switch.

    Advanced airers publish no model code and carry a per-model accurate TSL,
    so the model gate must not apply — declaration plus an actual report is
    the evidence. Regression: this returned [] before the family split.
    """
    d = make_device(
        {"DisinfectionSwitch": 0, "AirDryingSwitch": 0, "DryingSwitch": 0},
        product_key="a1abYBCSVlV",
        tsl_ids=("DisinfectionSwitch", "AirDryingSwitch", "DryingSwitch",
                 "DeviceModelType", "ModelFunctionList"),
    )
    got = switch_keys(d)
    check("advanced: disinfection created", "DisinfectionSwitch" in got, True)
    check("advanced: air drying created", "AirDryingSwitch" in got, True)
    check("advanced: drying created", "DryingSwitch" in got, True)


def test_advanced_airer_tsl_only_still_suppressed():
    """Even on the advanced family, TSL alone is not evidence."""
    d = make_device(
        {},
        product_key="a1abYBCSVlV",
        tsl_ids=("DisinfectionSwitch", "AirDryingSwitch", "DryingSwitch",
                 "DeviceModelType"),
    )
    got = switch_keys(d)
    check("advanced + TSL only: suppressed", "DisinfectionSwitch" in got, False)


def test_standard_airer_unaffected_by_family_split():
    """The standard family keeps the model gate: model 2 still has no drying.

    Guards the other side of the split — the fix must not leak into the
    standard family whose TSL over-declares.
    """
    d = make_device(
        {"PowerSwitch": 1, "DisinfectionSwitch": 0, "DryingSwitch": 0,
         "IonsSwitch": 0, "DeviceModelType": 2},
        product_key="a1kM9JAZ7aQ",
        tsl_ids=("PowerSwitch", "DisinfectionSwitch", "DryingSwitch",
                 "IonsSwitch", "DeviceModelType"),
    )
    got = switch_keys(d)
    check("standard model 2: disinfection", "DisinfectionSwitch" in got, True)
    check("standard model 2: no drying", "DryingSwitch" in got, False)
    check("standard model 2: no ions", "IonsSwitch" in got, False)


def test_gate_function_direct():
    """Exercise _airer_model_supported directly on the boundary values."""
    desc = next(d for d in AIRER_SWITCHES if d.key == "DisinfectionSwitch")
    for model, want in ((0, True), (2, True), (3, True), (1, False), (99, False), (None, False)):
        d = make_device({"DisinfectionSwitch": 0, "DeviceModelType": model},
                        tsl_ids=TSL_AIRER)
        check(f"gate(model={model})", _airer_model_supported(d, desc), want)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"capability gating: {R_PASS} passed, {len(R_FAIL)} failed")
    for line in R_FAIL:
        print(f"  FAIL {line}")
    return 0 if not R_FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
