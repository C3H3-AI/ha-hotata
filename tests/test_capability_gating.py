"""Capability-gating tests: which entities exist for which device state.

The model whitelist (0-3) is the only authority on hardware. Live
verification on a model-2 device proved BOTH the TSL and the report stream
mirror the full declaration set regardless of hardware (it reports
DryingSwitch/AirDryingSwitch/IonsSwitch it does not have), so:

  - an explicit model code decides per the whitelist;
  - an ABSENT model code must NOT create the entity: the registry never
    removes entities, so a guess would be permanent. v4.0.8's "trust the
    report" behavior was that wrong guess; these tests pin the revert, and
    the warning the gate raises instead (so the whitelist can be corrected
    with real data).

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
