"""Device-identification tests: which family does a product key belong to.

The vendor's own app identifies a device from its productKey alone, through a
dispatch table keyed by productKey (mini-program `libs/hotata/smartlock.js` ->
`exports.DEVICE`). That table is the ground truth used here; it was verified
live on 2026-10-07 against three cloud signals:

* `v2.0/device/home/getSPProductCategory` / `getSPProductListByCategoryId`
  expose exactly two categories — 10000 智能锁 and 11000 智能晾衣机 — and every
  product key in them is a key from that table.
* `HangerFeaturesConfig` (from `v1.0/app/sys/getappcommonparam`) is keyed by
  *model* for airers, while locks get *productKey*-prefixed keys such as
  `<pk>BatteryChange` / `<pk>LockDetection`; no airer key carries any.
* `v2.0/device/mqtt/info` hands the airer the family-scoped MQTT account
  `hanger_fast_bind` (base64).

Negative results worth remembering: `v2.0/ota/info`, `v2.0/device/checkDeviceGoods`,
`v2.0/hangerActivate/getDeviceInfoByDN` and `v2.0/device/active/getActiveCode`
answer identically for a garbage product key, so none of them can be used to
prove that a product key exists.

Run from anywhere:  python3 tests/test_device_family.py
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

from hotata.const import (  # noqa: E402
    FAMILY_AIRER,
    FAMILY_LOCK,
    SOURCE_PLATFORM_ALI,
    SOURCE_PLATFORM_BLE,
    SOURCE_PLATFORM_TENCENT,
    SOURCE_PLATFORM_TUYA,
    SOURCE_PLATFORM_YIYUAN,
    product_family,
    source_platform,
)
from hotata.models import HotataDevice  # noqa: E402
from hotata.switch import _switches_for_device  # noqa: E402

R_PASS = 0
R_FAIL = []


def check(label, got, want):
    global R_PASS
    if got == want:
        R_PASS += 1
    else:
        R_FAIL.append(f"{label}: got {got!r}, want {want!r}")


# Straight out of the vendor dispatch table: productKey -> (family, platform).
DISPATCH = {
    # airers (devType 17)
    "a1kM9JAZ7aQ": (FAMILY_AIRER, SOURCE_PLATFORM_ALI),
    "a1abYBCSVlV": (FAMILY_AIRER, SOURCE_PLATFORM_ALI),
    "a1WWvhXa6HQ": (FAMILY_AIRER, SOURCE_PLATFORM_ALI),
    "a1VoPdoApAu": (FAMILY_AIRER, SOURCE_PLATFORM_BLE),
    "p11wW9": (FAMILY_AIRER, SOURCE_PLATFORM_YIYUAN),
    # locks (devType 16)
    "8TBLTNQ7Y4": (FAMILY_LOCK, SOURCE_PLATFORM_TENCENT),
    "S0YEYJPNF2": (FAMILY_LOCK, SOURCE_PLATFORM_TENCENT),
    "6GHRSFPHF2": (FAMILY_LOCK, SOURCE_PLATFORM_TENCENT),
    "9CVBLHNPYE": (FAMILY_LOCK, SOURCE_PLATFORM_TENCENT),
    "GX784V2PCO": (FAMILY_LOCK, SOURCE_PLATFORM_TENCENT),
    "3JAWFEBH12": (FAMILY_LOCK, SOURCE_PLATFORM_TENCENT),
    "98D3WJ6WGP": (FAMILY_LOCK, SOURCE_PLATFORM_TENCENT),
    "S0L3SUM7N6": (FAMILY_LOCK, SOURCE_PLATFORM_TENCENT),
    "7BCANSHA33": (FAMILY_LOCK, SOURCE_PLATFORM_TENCENT),
    "d3dr3slkb2moukaa": (FAMILY_LOCK, SOURCE_PLATFORM_TUYA),
    "10ku5njsp8xsytid": (FAMILY_LOCK, SOURCE_PLATFORM_TUYA),
}

TSL_AIRER = (
    "PowerSwitch", "DisinfectionSwitch", "AirDryingSwitch", "DryingSwitch",
    "IonsSwitch", "DeviceModelType", "Position",
)


def test_every_dispatch_key_is_identified():
    for pk, (family, platform) in DISPATCH.items():
        check(f"{pk}: family", product_family(pk), family)
        check(f"{pk}: source platform", source_platform(pk), platform)


def test_unknown_key_is_not_guessed():
    check("unknown pk family", product_family("p0123456789"), None)
    check("unknown pk platform", source_platform("p0123456789"), None)
    check("empty pk family", product_family(""), None)
    check("none pk family", product_family(None), None)


def test_legacy_airer_key_still_identified():
    check("legacy airer key", product_family("a1H7OfeWFTS"), FAMILY_AIRER)


def make_device(properties, product_key, tsl_ids=()):
    return HotataDevice(
        iot_id="dev1",
        name="device",
        product_key=product_key,
        device_name="device",
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
    return [
        e.entity_description.key
        for e in _switches_for_device(co, device)
    ]


AIRER_REPORT = {
    "PowerSwitch": 1,
    "DisinfectionSwitch": 0,
    "DeviceModelType": 2,
}


def test_airer_gets_airer_switches():
    d = make_device(AIRER_REPORT, "a1kM9JAZ7aQ", tsl_ids=TSL_AIRER)
    got = switch_keys(d)
    check("airer: power created", "PowerSwitch" in got, True)
    check("airer: disinfection created", "DisinfectionSwitch" in got, True)


def test_new_airer_generations_are_airers_too():
    for pk in ("a1VoPdoApAu", "p11wW9"):
        d = make_device(AIRER_REPORT, pk, tsl_ids=TSL_AIRER)
        got = switch_keys(d)
        check(f"{pk}: disinfection created", "DisinfectionSwitch" in got, True)


def test_lock_never_gets_airer_switches():
    """A lock's product line may declare airer identifiers; it must not
    inherit airer switches from them."""
    for pk in ("98D3WJ6WGP", "d3dr3slkb2moukaa"):
        d = make_device(AIRER_REPORT, pk, tsl_ids=TSL_AIRER)
        check(f"{pk}: no switches", switch_keys(d), [])


def test_unknown_key_keeps_legacy_behaviour():
    """An unseen product key is not suppressed: it falls back to property
    gating instead of silently losing its entities."""
    d = make_device(AIRER_REPORT, "PK_UNSEEN", tsl_ids=TSL_AIRER)
    got = switch_keys(d)
    check("unseen: power created", "PowerSwitch" in got, True)
    check("unseen: disinfection created", "DisinfectionSwitch" in got, True)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"device identification: {R_PASS} passed, {len(R_FAIL)} failed")
    for line in R_FAIL:
        print("  FAIL", line)
    return 1 if R_FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
