"""Advanced airer entities must follow the device's own capability list.

The product line's TSL declares the A/B pole properties, the night lamp, voice
interaction, solar tracing and the slave-rail position on *every* model in the
family, so presence alone created all of them. Measured on a D-3072S
(pk a1abYBCSVlV) on 2026-10-07:

  * it never reports ApoleMotorControlMode / BpoleMotorControlMode /
    ApoleLightSwitch / BpoleLightSwitch / NightLightSwitch / SlavePosition;
  * its ModelFunctionList is 00000000000000000011111100110000, whose
    DOUBLE_POLE(24) and NIGHT_LIGHT(22) bits are 0;
  * the vendor app gates the A/B pole controls on exactly that bit
    (ifSupportFun(FUN_INDEX.DOUBLE_POLE)), so it shows neither rail.

HA nevertheless had cover.liang_yi_jia_a_gan / b_gan,
light.liang_yi_jia_a_gan_zhao_ming / b_gan_zhao_ming / ye_deng,
switch.liang_yi_jia_yu_yin_jiao_hu and the 副杆位置 / 日光颜色 sensors.
These tests pin the fix, and pin the safety valve: a device that publishes no
capability list at all keeps every entity (unknown means allow).

Run from anywhere:  python3 tests/test_advanced_entity_gating.py
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

capabilities = __import__("hotata.capabilities", fromlist=["X"])
models = __import__("hotata.models", fromlist=["HotataDevice"])
HotataDevice = models.HotataDevice
cover_mod = __import__("hotata.cover", fromlist=["_covers_for_device"])
light_mod = __import__("hotata.light", fromlist=["_lights_for_device"])
switch_mod = __import__("hotata.switch", fromlist=["_switches_for_device"])
sensor_mod = __import__("hotata.sensor", fromlist=["SENSORS"])

PK = "a1abYBCSVlV"          # the shared D-3072S
D3072S_MFL = "00000000000000000011111100110000"

# The advanced identifiers the family template declares for every model.
TSL = [
    "PowerSwitch", "MotorControlMode", "LightSwitch", "LightBrightness",
    "DisinfectionSwitch", "DryingSwitch", "AirDryingSwitch", "IonsSwitch",
    "Position", "CurrentPositionPoint", "DeviceModelType", "ModelFunctionList",
    "ApoleMotorControlMode", "BpoleMotorControlMode",
    "ApoleLightSwitch", "BpoleLightSwitch", "NightLightSwitch",
    "VoiceInteractionSwitch", "SolarTraceSwitch", "SunTraceSwitch",
    "SlavePosition", "DayLightColour",
]
# What the D-3072S actually reports (none of the advanced keys appear).
REPORTED = {
    "PowerSwitch": 1, "MotorControlMode": 0, "LightSwitch": 0,
    "LightBrightness": 50, "DisinfectionSwitch": 0, "DryingSwitch": 0,
    "AirDryingSwitch": 0, "IonsSwitch": 0, "Position": 17,
    "CurrentPositionPoint": 0, "DeviceModelType": 2,
    "ModelFunctionList": D3072S_MFL,
}


def mfl_from(names) -> str:
    """Build a ModelFunctionList string with the given capabilities set."""
    chars = ["0"] * 32
    for name in names:
        chars[32 - capabilities.FUN_INDEX[name]] = "1"
    return "".join(chars)


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


def make_device(extra=None, *, product_key=PK):
    properties = dict(REPORTED)
    properties.update(extra or {})
    device = HotataDevice(
        iot_id="d3072s",
        name="晾衣架",
        product_key=product_key,
        device_name="d3072s",
        online=True,
        raw={},
        properties={k: {"value": v} for k, v in properties.items()},
        thing_model={"properties": [{"identifier": i} for i in TSL]},
    )
    return device


def _key(entity):
    """The property identifier an entity was built from.

    Description-driven entities carry entity_description.key; the rail covers
    and on/off lamps are built per property and expose `identifier`.
    """
    description = getattr(entity, "entity_description", None)
    if description is not None and getattr(description, "key", None):
        return description.key
    return getattr(entity, "identifier", None)


def entity_keys(device):
    """Return (covers, lights, switches, capability-gated sensors)."""
    coordinator = cover_harness.FakeCoordinator(device)
    coordinator.data = {device.iot_id: device}
    covers = [_key(e) for e in cover_mod._covers_for_device(coordinator, device)]
    lights = [_key(e) for e in light_mod._lights_for_device(coordinator, device)]
    switches = [_key(e) for e in switch_mod._switches_for_device(coordinator, device)]
    sensors = [
        d.key for d in sensor_mod.SENSORS
        if capabilities.supported(device, d.capability)
    ]
    return covers, lights, switches, sensors


def test_d3072s_has_no_pole_or_night_entities():
    covers, lights, switches, sensors = entity_keys(make_device())
    R.check("A pole cover absent", "ApoleMotorControlMode" in covers, False)
    R.check("B pole cover absent", "BpoleMotorControlMode" in covers, False)
    R.check("main airer cover present", any(k is None for k in covers), True)
    R.check("A pole lamp absent", "ApoleLightSwitch" in lights, False)
    R.check("B pole lamp absent", "BpoleLightSwitch" in lights, False)
    R.check("night lamp absent", "NightLightSwitch" in lights, False)
    R.check("main lamp present", "LightSwitch" in lights or any(k is None for k in lights), True)
    R.check("voice switch absent", "VoiceInteractionSwitch" in switches, False)
    R.check("solar trace absent", "SolarTraceSwitch" in switches, False)
    R.check("sun trace absent", "SunTraceSwitch" in switches, False)
    R.check("disinfection switch present", "DisinfectionSwitch" in switches, True)
    R.check("slave position sensor absent", "SlavePosition" in sensors, False)
    R.check("daylight colour sensor absent", "DayLightColour" in sensors, False)


def test_entities_appear_when_the_bits_are_set():
    """A real dual-rail model must keep every one of these entities."""
    device = make_device(
        {"ModelFunctionList": mfl_from([
            "light", "disinfection", "night_light", "double_pole", "voice",
            "cct_light", "solar_trace", "sun_trace", "light_brightness",
        ])}
    )
    covers, lights, switches, sensors = entity_keys(device)
    R.check("A pole cover created", "ApoleMotorControlMode" in covers, True)
    R.check("B pole cover created", "BpoleMotorControlMode" in covers, True)
    R.check("A pole lamp created", "ApoleLightSwitch" in lights, True)
    R.check("B pole lamp created", "BpoleLightSwitch" in lights, True)
    R.check("night lamp created", "NightLightSwitch" in lights, True)
    R.check("voice switch created", "VoiceInteractionSwitch" in switches, True)
    R.check("solar trace created", "SolarTraceSwitch" in switches, True)
    R.check("sun trace created", "SunTraceSwitch" in switches, True)
    R.check("slave position created", "SlavePosition" in sensors, True)
    R.check("daylight colour created", "DayLightColour" in sensors, True)


def test_double_pole_alone_does_not_create_the_night_lamp():
    device = make_device({"ModelFunctionList": mfl_from(["light", "double_pole"])})
    covers, lights, switches, sensors = entity_keys(device)
    R.check("poles present", "ApoleMotorControlMode" in covers, True)
    R.check("night lamp still absent", "NightLightSwitch" in lights, False)
    R.check("slave position present", "SlavePosition" in sensors, True)
    R.check("daylight colour still absent", "DayLightColour" in sensors, False)


def test_unknown_capabilities_keep_every_entity():
    """No function list and no model code: nothing may be hidden."""
    device = make_device({"ModelFunctionList": "", "DeviceModelType": None})
    R.check("capabilities unknown", capabilities.resolve(device), None)
    covers, lights, switches, sensors = entity_keys(device)
    R.check("A pole cover kept", "ApoleMotorControlMode" in covers, True)
    R.check("B pole cover kept", "BpoleMotorControlMode" in covers, True)
    R.check("night lamp kept", "NightLightSwitch" in lights, True)
    R.check("voice switch kept", "VoiceInteractionSwitch" in switches, True)
    R.check("slave position kept", "SlavePosition" in sensors, True)
    R.check("daylight colour kept", "DayLightColour" in sensors, True)


def test_model_code_alone_still_gates_the_poles():
    """A device that publishes only DeviceModelType is still constrained."""
    device = make_device({"ModelFunctionList": "", "DeviceModelType": 2})
    caps = capabilities.resolve(device)
    R.check("source is the model code", caps.source, "DeviceModelType")
    covers, lights, switches, sensors = entity_keys(device)
    R.check("model 2 has no poles", "ApoleMotorControlMode" in covers, False)
    R.check("model 2 has no night lamp", "NightLightSwitch" in lights, False)
    R.check("model 2 keeps its main lamp", "LightSwitch" in lights or any(k is None for k in lights), True)


def main():
    test_d3072s_has_no_pole_or_night_entities()
    test_entities_appear_when_the_bits_are_set()
    test_double_pole_alone_does_not_create_the_night_lamp()
    test_unknown_capabilities_keep_every_entity()
    test_model_code_alone_still_gates_the_poles()
    return R.report("Advanced entity gating")


if __name__ == "__main__":
    sys.exit(main())
