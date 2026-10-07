"""Offline-aware polling: probe first, never re-read a known-offline device.

Covers the behaviour added on top of the fast/slow dynamic interval:
  - connectivity is read before properties, so a device that is down is
    detected without first wasting a property read on it;
  - WARM-UP: a device we have never read (no thing model, no properties) is
    polled fully even while offline, because the first poll is what tells the
    integration what the device is. Measured live: a device the cloud reports
    as offline (status 3) still answers /thing/properties/get with its full
    shadow — DeviceModelType included — and /thing/tsl/get with the whole
    declaration. Skipping those leaves has_property() empty, no entities are
    created for that device, and automations lose their entity ids on restart
    (seen on the production instance, 2026-10-07).
  - a device that is offline AND already known (thing model cached, properties
    present) gets get_online only — no get_properties, no get_latest_event, no
    get_thing_model;
  - the coordinator drops to the offline interval (30s) while one is down,
    and returns to the normal fast/slow choice once it answers again;
  - a mixed fleet keeps polling its online members normally.

Run from anywhere:  python3 tests/test_offline_polling.py
"""
import asyncio
import sys
import types
from datetime import timedelta
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
models = __import__("hotata.models", fromlist=["HotataDevice"])
HotataDevice = models.HotataDevice
coordinator_mod = __import__("hotata.coordinator", fromlist=["HotataCoordinator"])
HotataCoordinator = coordinator_mod.HotataCoordinator
HotataError = __import__("hotata.exceptions", fromlist=["HotataError"]).HotataError


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
        print(f"\n{title}: {self.passed} passed, {len(self.failed)} failed")
        for line in self.failed:
            print(f"  FAIL {line}")
        return 0 if not self.failed else 1


R = Results()


def device(iot_id, product_key="PK_AIRER"):
    return HotataDevice(
        iot_id=iot_id,
        name=iot_id,
        product_key=product_key,
        device_name=iot_id,
        online=True,
        raw={"productName": "X", "iotId": iot_id},
        properties={"MotorControlMode": 0},
    )


class FakeApi:
    """Records every call so tests can assert what was and was not polled."""

    def __init__(self, devices, online_map=None, fail_online=None):
        self._devices = devices
        # online_map: iot_id -> bool returned by async_get_online
        self._online = dict(online_map or {})
        self._fail_online = set(fail_online or [])
        self.calls = []

    async def async_list_devices(self):
        self.calls.append(("list_devices", None))
        return list(self._devices)

    async def async_list_subdevices(self, gateway_iot_id):
        self.calls.append(("list_subdevices", gateway_iot_id))
        return []

    async def async_get_online(self, iot_id):
        self.calls.append(("get_online", iot_id))
        if iot_id in self._fail_online:
            raise HotataError("status failed")
        return self._online.get(iot_id, True)

    async def async_get_properties(self, iot_id):
        self.calls.append(("get_properties", iot_id))
        return {"MotorControlMode": 0}

    async def async_get_thing_model(self, iot_id):
        self.calls.append(("get_thing_model", iot_id))
        return {"properties": []}

    async def async_get_latest_event(self, iot_id, ident):
        self.calls.append(("get_latest_event", iot_id))
        return None

    def calls_for(self, name, iot_id=None):
        return [
            c for c in self.calls
            if c[0] == name and (iot_id is None or c[1] == iot_id)
        ]


class FakeAccount:
    def __init__(self, api):
        self.api = api
        self.rate_limited = False
        self.poll_active = False
        self.successes = 0

    def note_cloud_success(self):
        self.successes += 1

    def note_cloud_failure(self, err):
        pass

    def maybe_persist_primary_tokens(self):
        pass


def make_coordinator(devices, online_map=None, fail_online=None, warm=False):
    """Build a coordinator. ``warm=True`` seeds the state a first poll leaves."""
    api = FakeApi(devices, online_map, fail_online)
    account = FakeAccount(api)
    co = HotataCoordinator.__new__(HotataCoordinator)
    co.hass = types.SimpleNamespace()
    co.entry = types.SimpleNamespace(data={})
    co.account = account
    co.thing_models = {}
    co.runtimes = {}
    co.data = None
    co.update_interval = timedelta(seconds=const.POLL_INTERVAL_SLOW)
    co._listeners = set()
    if warm:
        # Mirror the state a successful first poll leaves behind: the TSL cache
        # is populated and the coordinator still holds the previous device map
        # (HA sets ``self.data`` from the return value of every update).
        for dev in devices:
            co.thing_models[dev.product_key or dev.iot_id] = {
                "properties": [{"identifier": "MotorControlMode"}]
            }
        co.data = {dev.iot_id: dev for dev in devices}
    return co, api


async def test_warm_offline_device_is_probed_but_not_property_read():
    """Steady state: an offline device we already know -> get_online only."""
    devs = [device("d1")]
    co, api = make_coordinator(devs, online_map={"d1": False}, warm=True)
    data = await co._async_update_data()
    R.check("warm offline: get_online called", len(api.calls_for("get_online", "d1")), 1)
    R.check("warm offline: no get_properties", len(api.calls_for("get_properties")), 0)
    R.check("warm offline: no thing model read", len(api.calls_for("get_thing_model")), 0)
    R.check("warm offline: no event read", len(api.calls_for("get_latest_event")), 0)
    R.check("warm offline: cached thing model still attached",
            bool(data["d1"].thing_model.get("properties")), True)


async def test_cold_offline_device_is_warmed_up():
    """First poll after a restart: a cold device is read even while offline.

    This is the regression the production instance hit on 2026-10-07: without
    the warm-up, the device had no TSL and no properties, has_property() was
    false for every key, and all of its entities disappeared.
    """
    devs = [device("d1")]
    devs[0].properties = {}          # fresh start: nothing read yet
    co, api = make_coordinator(devs, online_map={"d1": False})
    data = await co._async_update_data()
    R.check("cold offline: probed", len(api.calls_for("get_online", "d1")), 1)
    R.check("cold offline: properties read", len(api.calls_for("get_properties", "d1")), 1)
    R.check("cold offline: thing model read", len(api.calls_for("get_thing_model", "d1")), 1)
    R.check("cold offline: properties populated",
            bool(data["d1"].properties), True)


async def test_warm_offline_device_without_thing_model_is_still_warmed():
    """Missing TSL alone is enough to justify the read, even with properties."""
    devs = [device("d1")]
    co, api = make_coordinator(devs, online_map={"d1": False})   # cold TSL cache
    await co._async_update_data()
    R.check("no TSL cached: properties read", len(api.calls_for("get_properties", "d1")), 1)
    R.check("no TSL cached: thing model read", len(api.calls_for("get_thing_model", "d1")), 1)


async def test_online_device_is_fully_polled():
    devs = [device("d1")]
    co, api = make_coordinator(devs, online_map={"d1": True})
    await co._async_update_data()
    R.check("online: get_online called", len(api.calls_for("get_online", "d1")), 1)
    R.check("online: get_properties called", len(api.calls_for("get_properties", "d1")), 1)
    R.check("online: thing model called", len(api.calls_for("get_thing_model", "d1")), 1)


async def test_connectivity_is_checked_before_properties():
    """Ordering matters: detecting 'down' must not cost a wasted read."""
    devs = [device("d1")]
    co, api = make_coordinator(devs, online_map={"d1": True})
    await co._async_update_data()
    names = [c[0] for c in api.calls]
    R.check("get_online precedes get_properties",
            names.index("get_online") < names.index("get_properties"), True)


async def test_offline_pins_interval_to_30s():
    devs = [device("d1")]
    co, api = make_coordinator(devs, online_map={"d1": False})
    await co._async_update_data()
    R.check("offline interval is 30s",
            co.update_interval.total_seconds(), const.POLL_INTERVAL_OFFLINE)


async def test_offline_beats_active_fast_window():
    """A just-sent command must not force 5s polling on a dead device."""
    devs = [device("d1")]
    co, api = make_coordinator(devs, online_map={"d1": False})
    co.account.poll_active = True
    await co._async_update_data()
    R.check("offline wins over active window",
            co.update_interval.total_seconds(), const.POLL_INTERVAL_OFFLINE)


async def test_recovery_restores_normal_cadence():
    devs = [device("d1")]
    co, api = make_coordinator(devs, online_map={"d1": False})
    await co._async_update_data()
    R.check("pinned offline", co.update_interval.total_seconds(),
            const.POLL_INTERVAL_OFFLINE)
    # Device comes back.
    api._online["d1"] = True
    co.account.poll_active = False
    await co._async_update_data()
    R.check("recovers to slow", co.update_interval.total_seconds(),
            const.POLL_INTERVAL_SLOW)
    R.check("properties polled after recovery",
            len(api.calls_for("get_properties", "d1")) >= 1, True)


async def test_recovery_into_active_window_goes_fast():
    devs = [device("d1")]
    co, api = make_coordinator(devs, online_map={"d1": False})
    await co._async_update_data()
    api._online["d1"] = True
    co.account.poll_active = True
    await co._async_update_data()
    R.check("recovers to fast when active",
            co.update_interval.total_seconds(), const.POLL_INTERVAL_FAST)


async def test_mixed_fleet_only_skips_the_offline_device():
    """Per-device split: the online member keeps its properties fresh."""
    devs = [device("up"), device("down")]
    co, api = make_coordinator(devs, online_map={"up": True, "down": False}, warm=True)
    await co._async_update_data()
    R.check("online member polled", len(api.calls_for("get_properties", "up")), 1)
    R.check("offline member skipped", len(api.calls_for("get_properties", "down")), 0)
    R.check("both probed for online", len(api.calls_for("get_online")), 2)


async def test_any_offline_device_pins_the_fleet_cadence():
    devs = [device("up"), device("down")]
    co, api = make_coordinator(devs, online_map={"up": True, "down": False})
    await co._async_update_data()
    R.check("one offline pins cadence", co.update_interval.total_seconds(),
            const.POLL_INTERVAL_OFFLINE)


async def test_offline_device_keeps_last_known_properties():
    """Skipping must not blank the device: entities decide availability."""
    devs = [device("d1")]
    devs[0].properties = {"MotorControlMode": 1}
    co, api = make_coordinator(devs, online_map={"d1": False}, warm=True)
    data = await co._async_update_data()
    R.check("properties preserved", data["d1"].properties, {"MotorControlMode": 1})
    R.check("online flag false", data["d1"].online, False)


async def test_online_failure_does_not_skip_properties():
    """An unreadable status is not proof of offline: keep polling normally."""
    devs = [device("d1")]
    co, api = make_coordinator(devs, online_map={"d1": True}, fail_online=["d1"])
    await co._async_update_data()
    R.check("status error still polls properties",
            len(api.calls_for("get_properties", "d1")), 1)


async def test_unknown_status_keeps_normal_cadence():
    """status None (not False) must not pin the offline interval."""
    devs = [device("d1")]
    co, api = make_coordinator(devs, online_map={})
    api._online.clear()          # async_get_online returns True default here
    await co._async_update_data()
    R.check("known-online keeps slow", co.update_interval.total_seconds(),
            const.POLL_INTERVAL_SLOW)


async def test_warm_all_offline_still_issues_only_probes():
    devs = [device("a"), device("b"), device("c")]
    co, api = make_coordinator(
        devs, online_map={"a": False, "b": False, "c": False}, warm=True
    )
    await co._async_update_data()
    R.check("no property reads at all", len(api.calls_for("get_properties")), 0)
    R.check("no thing model reads at all", len(api.calls_for("get_thing_model")), 0)
    R.check("three probes", len(api.calls_for("get_online")), 3)
    R.check("pinned to offline", co.update_interval.total_seconds(),
            const.POLL_INTERVAL_OFFLINE)


async def test_offline_interval_is_thirty_seconds():
    R.check("POLL_INTERVAL_OFFLINE == 30", const.POLL_INTERVAL_OFFLINE, 30)


async def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    for fn in tests:
        await fn()
    code = R.report("Offline-aware polling")
    print(f"total checks run: {R.passed + len(R.failed)}")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
