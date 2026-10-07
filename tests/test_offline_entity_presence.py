"""Offline device, cold start: its entities must still exist.

This is the regression the production instance hit on 2026-10-07. The airer
"智能晾衣机01" (pk a1kM9JAZ7aQ) was reported offline by the cloud (status 3),
the container restarted, and every one of its entities disappeared — cover,
light, switch, button, number, sensor — leaving only online status and the
diagnostics. Automations referencing those entity ids break when that happens.

Root cause: the offline short-circuit in ``HotataCoordinator._async_update_data``
skipped the thing-model fetch along with the property read. On a cold start both
were empty, so ``has_property()`` was false for every key and the entity
factories created nothing.

The live measurement that disproves the assumption behind that short-circuit —
a device the cloud calls offline still answers with everything the integration
needs — is replayed here with the values captured from the real device:

  uc/listBindingByAccount -> status 3            (offline)
  /thing/properties/get   -> 15 properties, DeviceModelType = 2
  /thing/tsl/get          -> 29 declared properties

Note the reported set: it contains AirDryingSwitch, DryingSwitch and IonsSwitch
even though the device lacks that hardware (DeviceModelType 2 = 照明+消毒) —
which is why capabilities, and not presence, must decide the switches.

Run from anywhere:  python3 tests/test_offline_entity_presence.py
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

const = __import__("hotata.const", fromlist=["X"])
HotataDevice = __import__("hotata.models", fromlist=["HotataDevice"]).HotataDevice
HotataError = __import__("hotata.exceptions", fromlist=["HotataError"]).HotataError
HotataCoordinator = __import__("hotata.coordinator", fromlist=["HotataCoordinator"]).HotataCoordinator
entity_mod = __import__("hotata.entity", fromlist=["has_property"])
switch_mod = __import__("hotata.switch", fromlist=["_switches_for_device"])
sensor_mod = __import__("hotata.sensor", fromlist=["_entities"])
capabilities = __import__("hotata.capabilities", fromlist=["X"])

# ---- values captured from the real device (2026-10-07) ----------------------
TSL_IDENTIFIERS = [
    "PowerSwitch", "ModelFunctionList", "PM25", "WiFI_SNR", "Temperature",
    "Brand", "WIFI_Band", "McuVersion", "LightRemainingTime",
    "PauseDisinfection", "MotorControlMode", "DeviceModelType", "Humidity",
    "DryingRemainingTime", "WIFI_AP_BSSID", "DisinfectionRemainingTime",
    "DryingSwitch", "AirDryingSwitch", "IonsRemainingTime",
    "AirDryingRemainingTime", "WIFI_Channel", "SolarTerms", "Weather",
    "Position", "IonsSwitch", "DisinfectionSwitch", "WiFI_RSSI", "WindLevel",
    "LightSwitch",
]
REPORTED_PROPERTIES = {
    "AirDryingRemainingTime": 0, "AirDryingSwitch": 0, "Brand": 0,
    "DeviceModelType": 2, "DisinfectionRemainingTime": 0,
    "DisinfectionSwitch": 0, "DryingRemainingTime": 0, "DryingSwitch": 0,
    "IonsRemainingTime": 0, "IonsSwitch": 0, "LightRemainingTime": 0,
    "LightSwitch": 0, "MotorControlMode": 0, "Position": 0, "PowerSwitch": 1,
}
OFFLINE_DEVICE = "dNl3rypkpU6cBXGQHrzB000000"
PK = "a1kM9JAZ7aQ"


def _entity_key(entity):
    """Key of a description-driven entity, or the property/local name otherwise."""
    description = getattr(entity, "entity_description", None)
    if description is not None and getattr(description, "key", None):
        return description.key
    return getattr(entity, "identifier", None) or getattr(entity, "_attr_name", None)


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
            print(f"  FAIL {line}")
        return 0 if not self.failed else 1


R = Results()


def fresh_device():
    """A device object as discovery builds it: metadata only, nothing read yet."""
    return HotataDevice(
        iot_id=OFFLINE_DEVICE,
        name="智能晾衣机01",
        product_key=PK,
        device_name="e8fdf8b5415b",
        online=True,
        raw={"productName": "好太太晾衣机-"},
        properties={},
    )


class FakeApi:
    def __init__(self, devices):
        self._devices = devices
        self.calls = []

    async def async_list_devices(self):
        self.calls.append(("list_devices", None))
        return list(self._devices)

    async def async_list_subdevices(self, gateway_iot_id):
        return []

    async def async_get_online(self, iot_id):
        self.calls.append(("get_online", iot_id))
        return False                       # the cloud says status 3

    async def async_get_properties(self, iot_id):
        self.calls.append(("get_properties", iot_id))
        # The cloud answers with the full shadow even while "offline".
        return {k: {"value": v} for k, v in REPORTED_PROPERTIES.items()}

    async def async_get_thing_model(self, iot_id):
        self.calls.append(("get_thing_model", iot_id))
        return {"properties": [{"identifier": i} for i in TSL_IDENTIFIERS]}

    async def async_get_latest_event(self, iot_id, ident):
        return None

    def calls_for(self, name):
        return [c for c in self.calls if c[0] == name]


class FakeAccount:
    def __init__(self, api):
        self.api = api
        self.rate_limited = False
        self.poll_active = False

    def note_cloud_success(self):
        pass

    def note_cloud_failure(self, err):
        pass

    def maybe_persist_primary_tokens(self):
        pass


def make_coordinator(devices):
    api = FakeApi(devices)
    account = FakeAccount(api)
    co = HotataCoordinator.__new__(HotataCoordinator)
    co.hass = types.SimpleNamespace()
    co.entry = types.SimpleNamespace(data={})
    co.account = account
    co.thing_models = {}
    co.runtimes = {}
    co.data = None
    co.update_interval = const.POLL_INTERVAL_SLOW
    co._listeners = set()
    return co, api


class StubCoordinator:
    """Minimal coordinator for the entity factories (they only need .data)."""

    def __init__(self, device):
        self.data = {device.iot_id: device}
        self.hass = None
        self.runtimes = {}


def test_cold_offline_start_still_reads_the_device():
    async def run():
        device = fresh_device()
        co, api = make_coordinator([device])
        data = await co._async_update_data()
        polled = data[OFFLINE_DEVICE]
        R.check("offline flag set", polled.online, False)
        R.check("properties were read despite offline",
                len(api.calls_for("get_properties")), 1)
        R.check("thing model was read despite offline",
                len(api.calls_for("get_thing_model")), 1)
        R.check("properties populated", bool(polled.properties), True)
        R.check("TSL populated", len(polled.thing_model.get("properties", [])), 29)
        return polled

    return asyncio.run(run())


def test_entities_exist_for_the_offline_device(polled):
    """The whole point: presence survives an offline cold start."""
    R.check("has_property on a TSL key", entity_mod.has_property(polled, "Weather"), True)
    R.check("has_property on a reported key",
            entity_mod.has_property(polled, "DryingSwitch"), True)
    R.check("presence alone is not capability — DryingSwitch is reported",
            "DryingSwitch" in polled.properties, True)

    stub = StubCoordinator(polled)
    switches = {_entity_key(e) for e in switch_mod._switches_for_device(stub, polled)}
    R.check("power switch exists", "PowerSwitch" in switches, True)
    R.check("disinfection switch exists", "DisinfectionSwitch" in switches, True)
    R.check("drying switch suppressed by model 2", "DryingSwitch" in switches, False)
    R.check("air-drying switch suppressed by model 2", "AirDryingSwitch" in switches, False)
    R.check("ions switch suppressed by model 2", "IonsSwitch" in switches, False)

    caps = capabilities.resolve(polled)
    # Assert first, then fall back so the remaining checks still report: a
    # broken warm-up shows up as an assertion failure, not a stack trace.
    R.check("capability source resolvable while offline", caps is not None, True)
    caps = caps or capabilities.from_model_type(2)
    sensors = {d.key for d in sensor_mod.SENSORS
               if d.capability is None or caps.has(d.capability)}
    R.check("disinfection remaining time exists",
            "DisinfectionRemainingTime" in sensors, True)
    R.check("drying remaining time suppressed",
            "DryingRemainingTime" in sensors, False)
    R.check("air-drying remaining time suppressed",
            "AirDryingRemainingTime" in sensors, False)
    R.check("ions remaining time suppressed",
            "IonsRemainingTime" in sensors, False)


def test_the_old_behaviour_is_reproduced():
    """Control: with nothing read, the factories create nothing at all.

    This is what the production restart looked like.
    """
    blank = HotataDevice(
        iot_id=OFFLINE_DEVICE, name="智能晾衣机01", product_key=PK,
        device_name="e8fdf8b5415b", online=False, raw={}, properties={},
    )
    stub = StubCoordinator(blank)
    switches = list(switch_mod._switches_for_device(stub, blank))
    R.check("no properties + no TSL -> no switches (the production failure)",
            switches, [])
    R.check("resolve() has nothing to work with",
            capabilities.resolve(blank), None)


def test_warm_offline_poll_skips_the_reads():
    async def run():
        device = fresh_device()
        co, api = make_coordinator([device])
        first = await co._async_update_data()
        before = len(api.calls_for("get_properties"))
        # HA stores the returned map, so the next poll sees the previous one.
        co.data = first
        second = await co._async_update_data()
        R.check("no additional property read while warm + offline",
                len(api.calls_for("get_properties")), before)
        R.check("properties carried over from the previous poll",
                second[OFFLINE_DEVICE].properties, first[OFFLINE_DEVICE].properties)
        R.check("thing model still attached",
                bool(second[OFFLINE_DEVICE].thing_model.get("properties")), True)
        R.check("still reported offline", second[OFFLINE_DEVICE].online, False)
        # And the entity set is unchanged.
        stub = StubCoordinator(second[OFFLINE_DEVICE])
        keys = {_entity_key(e) for e in switch_mod._switches_for_device(stub, second[OFFLINE_DEVICE])}
        R.check("disinfection switch still present", "DisinfectionSwitch" in keys, True)
        R.check("drying switch still absent", "DryingSwitch" in keys, False)

    asyncio.run(run())


def test_online_device_is_unaffected():
    async def run():
        device = fresh_device()

        class OnlineApi(FakeApi):
            async def async_get_online(self, iot_id):
                self.calls.append(("get_online", iot_id))
                return True

        api = OnlineApi([device])
        co = HotataCoordinator.__new__(HotataCoordinator)
        co.hass = types.SimpleNamespace()
        co.entry = types.SimpleNamespace(data={})
        co.account = FakeAccount(api)
        co.thing_models = {}
        co.runtimes = {}
        co.data = None
        co.update_interval = const.POLL_INTERVAL_SLOW
        co._listeners = set()
        await co._async_update_data()
        R.check("online: properties read", len(api.calls_for("get_properties")), 1)
        R.check("online: TSL read", len(api.calls_for("get_thing_model")), 1)

    asyncio.run(run())


def main():
    polled = test_cold_offline_start_still_reads_the_device()
    test_entities_exist_for_the_offline_device(polled)
    test_the_old_behaviour_is_reproduced()
    test_warm_offline_poll_skips_the_reads()
    test_online_device_is_unaffected()
    return R.report("Offline entity presence")


if __name__ == "__main__":
    sys.exit(main())
