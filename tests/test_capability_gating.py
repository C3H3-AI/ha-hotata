"""Capability-gating tests: which entities exist for which device state.

The gate has two layers (issue #11):

- presence: the identifier is TSL-declared or actually reported;
- authority: ``DeviceModelType``, the 0-3 model code.

The model code is the hardware authority. The TSL is a product-line template
listing every possible function, and the report stream mirrors that template
rather than the fitted hardware — verified live on a model-2 device
(``a1kM9JAZ7aQ``) that reports DryingSwitch/AirDryingSwitch/IonsSwitch it does
not have, which is why v4.0.11's "trust the report" rule was wrong.

When no model code is published (the D-3072S, pk ``a1abYBCSVlV``, reports
``ModelFunctionList`` instead), there is nothing to check a declaration
against. The rule there is **unknown means allow**: create, and log it. That
matches the other public Hotata integration (chliny/ha-hotata) and the
maintainer's stated preference for over-providing over silently dropping.

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
        self.hass = None


def _entity_key(entity):
    """Key of a description-driven entity, or the property/local name otherwise."""
    description = getattr(entity, "entity_description", None)
    if description is not None and getattr(description, "key", None):
        return description.key
    return getattr(entity, "identifier", None) or getattr(entity, "_attr_name", None)


def switch_keys(device):
    """Run the factory and return the created switch keys, in order."""
    co = FakeCoordinator(device)
    return [_entity_key(e) for e in _switches_for_device(co, device)]


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


def gated_sensor_keys(device):
    """Keys of capability-gated sensors the factory would yield."""
    from hotata.capabilities import resolve
    from hotata.sensor import SENSORS
    out = []
    capabilities = resolve(device)
    for description in SENSORS:
        if description.capability is None:
            continue
        if capabilities is None:
            if not device.properties:
                continue
        elif not capabilities.has(description.capability):
            continue
        out.append(description.key)
    return out


def test_unknown_model_reported_property_present():
    """DisinfectionSwitch reported but DeviceModelType absent.

    The report mirrors the TSL, not the hardware (proven live on a model-2
    device reporting switches it lacks), so a report is NOT evidence either
    way. With no model code there is nothing to check the declaration
    against, and the rule is to create rather than guess — "unknown means
    allow". Same rule as chliny/ha-hotata's `_airer_model_supported`.
    """
    d = make_device(
        {"PowerSwitch": 1, "DisinfectionSwitch": 0},
        tsl_ids=TSL_AIRER,
    )
    got = switch_keys(d)
    check("unknown model: disinfection created (unknown means allow)",
          "DisinfectionSwitch" in got, True)


def test_unknown_model_tsl_only_declaration_created():
    """TSL declares it and the device never reports it: still created.

    Without a model code the declaration is the only capability statement we
    have, and the integration prefers a visible-but-possibly-spurious entity
    over silently dropping a real one.
    """
    d = make_device({"PowerSwitch": 1}, tsl_ids=TSL_AIRER)
    got = switch_keys(d)
    check("unknown model + TSL-only: disinfection created",
          "DisinfectionSwitch" in got, True)


def test_unparseable_model_created():
    """A garbled model value cannot be checked, so it is treated as unknown."""
    d = make_device(
        {"PowerSwitch": 1, "DisinfectionSwitch": 0, "DeviceModelType": "x9"},
        tsl_ids=TSL_AIRER,
    )
    got = switch_keys(d)
    check("garbled model: disinfection created",
          "DisinfectionSwitch" in got, True)


def test_out_of_range_model_created():
    """A model code outside 0-3 is unmappable, hence unknown — and allowed.

    There is no table entry to apply, so this is the same situation as a device
    publishing nothing at all: create and log rather than guess away. (The
    D-3072S publishes no model code but a function list; a device may also
    publish a value from a newer product line this version has never seen.)
    """
    d = make_device(
        {"PowerSwitch": 1, "DisinfectionSwitch": 0, "DeviceModelType": 99},
        tsl_ids=TSL_AIRER,
    )
    got = switch_keys(d)
    check("model 99: disinfection created (unmappable)", "DisinfectionSwitch" in got, True)


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


def test_advanced_airer_with_a_report_but_no_model_code_is_created():
    """The advanced family publishes no model code, so a report is the evidence.

    ``a1abYBCSVlV`` (the D-3072S) reports ``ModelFunctionList`` but no
    ``DeviceModelType``; a device that reports at all and states no model code
    is genuinely unknown, so the entity is created (issue #11). Only a device
    with nothing reported yet waits.
    """
    d = make_device(
        {"PowerSwitch": 1, "DisinfectionSwitch": 0},
        product_key="a1abYBCSVlV",
        tsl_ids=("DisinfectionSwitch", "AirDryingSwitch", "DryingSwitch",
                 "DeviceModelType"),
    )
    got = switch_keys(d)
    check("advanced, reported, no model code: created",
          "DisinfectionSwitch" in got, True)


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
    for model, want in ((0, True), (2, True), (3, True), (1, False), (99, True), (None, True)):
        d = make_device({"DisinfectionSwitch": 0, "DeviceModelType": model},
                        tsl_ids=TSL_AIRER)
        check(f"gate(model={model})", _airer_model_supported(d, desc), want)


def test_empty_report_waits_instead_of_guessing():
    """A cold start (no properties yet) must not create gated entities.

    Entities are created once and never withdrawn, so guessing while the first
    poll is still in flight leaves spurious switches behind forever. Seen live
    on 2026-10-07: 风干/烘干/负离子 reappeared on the model-2 device because its
    first poll returned no properties, while the next one reported
    DeviceModelType 2.
    """
    d = make_device({}, tsl_ids=TSL_AIRER)
    got = switch_keys(d)
    check("no report yet: disinfection not created", "DisinfectionSwitch" in got, False)
    check("no report yet: no drying either", "DryingSwitch" in got, False)
    check("no report yet: power is not gated", "PowerSwitch" in got, True)


def test_report_without_a_model_code_still_allows():
    """Issue #11: a device that reports but publishes no model code is allowed.

    The distinction is "has not reported" versus "reported, but without a model
    code" — only the first one waits.
    """
    d = make_device({"PowerSwitch": 1, "DisinfectionSwitch": 0}, tsl_ids=TSL_AIRER)
    got = switch_keys(d)
    check("reported, no model code: disinfection created",
          "DisinfectionSwitch" in got, True)


def test_gated_sensor_waits_for_the_first_report():
    d = make_device({}, tsl_ids=TSL_AIRER)
    check("sensor gate: waiting while there is no report",
          gated_sensor_keys(d), [])
    d2 = make_device(
        {"PowerSwitch": 1, "DissolveOxygenSwitch": 0, "DisinfectionSwitch": 0},
        tsl_ids=TSL_AIRER,
    )
    check("sensor gate: reported without a model code -> allowed",
          "DisinfectionRemainingTime" in gated_sensor_keys(d2), True)


def _seed_registry(device, key, entity_id):
    """A fresh registry holding one entry an earlier version left behind.

    Fresh per test on purpose: the stub keeps a module-level registry, so
    sharing it would leak removals between cases.
    """
    from homeassistant.helpers import entity_registry as er
    from hotata.entity import entity_identity
    registry = er.EntityRegistry()
    registry.seed(entity_identity(device, key), entity_id)
    return registry


def test_stale_entry_is_removed_when_capability_says_absent():
    """Self-healing: a leftover entity for a function the device lacks goes away."""
    import types as _types
    d = make_device(
        {"PowerSwitch": 1, "DryingSwitch": 0, "DeviceModelType": 2},
        tsl_ids=TSL_AIRER,
    )
    registry = _seed_registry(d, "DryingSwitch", "switch.dev1_hot_drying")
    co = FakeCoordinator(d)
    co.hass = _types.SimpleNamespace(entity_registry=registry)
    keys = [_entity_key(e) for e in _switches_for_device(co, d)]
    check("drying switch not created", "DryingSwitch" in keys, False)
    check("stale registry entry removed",
          registry.removed, ["switch.dev1_hot_drying"])


def test_stale_entry_survives_while_waiting_for_the_first_report():
    """Waiting is not proof: an entity must not be deleted on a cold start."""
    import types as _types
    d = make_device({}, tsl_ids=TSL_AIRER)
    registry = _seed_registry(d, "DryingSwitch", "switch.dev1_hot_drying")
    co = FakeCoordinator(d)
    co.hass = _types.SimpleNamespace(entity_registry=registry)
    keys = [_entity_key(e) for e in _switches_for_device(co, d)]
    check("nothing created while waiting", "DryingSwitch" in keys, False)
    check("but nothing was deleted either", registry.removed, [])


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
