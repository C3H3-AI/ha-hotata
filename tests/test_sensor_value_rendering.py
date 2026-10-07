"""Sensors must render values the way the device's own TSL declares them.

The same identifier means different things on different product lines, and a
hard-coded map silently mis-renders one of them. Measured on 2026-10-07:

  衣杆位置 / Position, 智能晾衣机01 (pk a1kM9JAZ7aQ)
      dataType = {"type": "enum",
                  "specs": {"0": "无此功能", "1": "最上面", "2": "中间", "3": "最下面"}}
      reported 0  -> the cloud itself says "no such function"

  晾杆位置 / Position, D-3072S (pk a1abYBCSVlV)
      dataType = {"type": "int", "specs": {"min": "0", "max": "255",
                                           "unit": "%", "step": "1"}}
      reported 0  -> 0 %, i.e. the rail parked at the top

The integration's description carried value_map={0: "无此功能", 1: "顶部", …},
so the D-3072S's 0 % was reported as "无此功能" — a wrong reading of a healthy
value. The fix reads the enum (and only the enum) out of the TSL, per device.

Run from anywhere:  python3 tests/test_sensor_value_rendering.py
"""
import sys
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

import ha_stub  # noqa: E402
import cover_harness  # noqa: E402

PKG = REPO_ROOT / "custom_components"
sys.path.insert(0, str(PKG))
_pkg = types.ModuleType("hotata")
_pkg.__path__ = [str(PKG / "hotata")]
sys.modules.setdefault("hotata", _pkg)

models = __import__("hotata.models", fromlist=["HotataDevice"])
HotataDevice = models.HotataDevice
sensor_mod = __import__("hotata.sensor", fromlist=["SENSORS", "HotataPropertySensor"])
tsl_mod = __import__("hotata.tsl", fromlist=["enum_display"])

# The two real declarations for the same identifier.
ENUM_POSITION = {
    "type": "enum",
    "specs": {"0": "无此功能", "1": "最上面", "2": "中间", "3": "最下面"},
}
INT_POSITION = {
    "type": "int",
    "specs": {"min": "0", "max": "255", "step": "1", "unit": "%", "unitName": "百分比"},
}


class Results:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, label, got, want):
        if got == want:
            self.passed += 1
        else:
            self.failed.append(f"{label}: got {got!r}, want {want!r}")

    def report(self, title):
        print(f"{title}: {self.passed} passed, {len(self.failed)} failed")
        for line in self.failed:
            print("  FAIL", line)
        return 0 if not self.failed else 1


R = Results()

DESCRIPTIONS = {d.key: d for d in sensor_mod.SENSORS}


def make_device(data_type, reported, key="Position", extra_tsl=None):
    declaration = {"identifier": key}
    if data_type is not None:
        declaration["dataType"] = data_type
    properties = [declaration]
    properties.extend(extra_tsl or [])
    device = HotataDevice(
        iot_id="dev1",
        name="airer",
        product_key="a1kM9JAZ7aQ",
        device_name="dev1",
        online=True,
        raw={},
        properties={key: {"value": reported}},
        thing_model={"properties": properties},
    )
    coordinator = cover_harness.FakeCoordinator(device)
    coordinator.data = {device.iot_id: device}
    return device, coordinator


def render(data_type, reported, key="Position", description_key="Position", extra_tsl=None):
    device, coordinator = make_device(data_type, reported, key, extra_tsl)
    entity = sensor_mod.HotataPropertySensor(
        coordinator, device, DESCRIPTIONS[description_key]
    )
    return entity.native_value


def test_int_percentage_is_not_mistaken_for_an_enum():
    """The D-3072S case: 0 % must read as 0, not "无此功能"."""
    R.check("0 with int % TSL", render(INT_POSITION, 0), 0)
    R.check("17 with int % TSL", render(INT_POSITION, 17), 17)
    R.check("255 with int % TSL", render(INT_POSITION, 255), 255)


def test_enum_labels_come_from_the_device_tsl():
    """The airer case: the cloud's own labels, including its 0."""
    R.check("0 with enum TSL", render(ENUM_POSITION, 0), "无此功能")
    R.check("1 uses the TSL label, not our wording",
            render(ENUM_POSITION, 1), "最上面")
    R.check("2 with enum TSL", render(ENUM_POSITION, 2), "中间")
    R.check("3 with enum TSL", render(ENUM_POSITION, 3), "最下面")


def test_no_tsl_declaration_returns_the_raw_value():
    """Position carries no map of its own any more: raw when the TSL is silent."""
    R.check("0 without a TSL declaration", render(None, 0), 0)
    R.check("1 without a TSL declaration", render(None, 1), 1)


def test_unknown_string_still_falls_back_to_the_value():
    R.check("unmapped enum value passes through as reported",
            render(ENUM_POSITION, 9), 9)


def test_enum_display_only_answers_for_enums():
    device, _ = make_device(INT_POSITION, 0)
    R.check("int declaration -> no enum display",
            tsl_mod.enum_display(device, "Position"), {})
    device2, _ = make_device(ENUM_POSITION, 0)
    R.check("enum declaration -> wire values mapped",
            tsl_mod.enum_display(device2, "Position")[0], "无此功能")
    R.check("enum display also keyed by string",
            tsl_mod.enum_display(device2, "Position")["1"], "最上面")
    R.check("undeclared identifier -> empty",
            tsl_mod.enum_display(device2, "NotAProperty"), {})


def test_device_model_type_keeps_its_translation():
    """A curated map wins: DeviceModelType keeps our wording (and the cloud's
    own enum, 照明_消毒机型, does not leak into the state)."""
    device, coordinator = make_device(
        {"type": "int", "specs": {"min": "0", "max": "255"}},
        2,
        key="DeviceModelType",
    )
    entity = sensor_mod.HotataPropertySensor(
        coordinator, device, DESCRIPTIONS["DeviceModelType"]
    )
    R.check("model 2 translation kept", entity.native_value, "照明、消毒")


def main():
    test_int_percentage_is_not_mistaken_for_an_enum()
    test_enum_labels_come_from_the_device_tsl()
    test_no_tsl_declaration_returns_the_raw_value()
    test_unknown_string_still_falls_back_to_the_value()
    test_enum_display_only_answers_for_enums()
    test_device_model_type_keeps_its_translation()
    return R.report("Sensor value rendering")


if __name__ == "__main__":
    sys.exit(main())
