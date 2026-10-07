"""Helper harness for driving HotataAirerCover under bare CPython.

Shared by test_airer_cover_position.py and test_airer_cover_preservation.py.
Imports ha_stub first (it installs every homeassistant.* stub the cover
platform needs), then exposes a fake coordinator/device/runtime plus a
recording entity so tests can assert on commands, timers and state writes.

Run from anywhere; the test scripts insert this directory on sys.path.
"""
import os
import sys
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

import ha_stub  # noqa: E402  (must precede the hotata import)

PKG = REPO_ROOT / "custom_components"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

_pkg = types.ModuleType("hotata")
_pkg.__path__ = [str(PKG / "hotata")]
sys.modules.setdefault("hotata", _pkg)

# Escape hatch used by the review harness to prove these tests fail on the
# pre-fix code: point HOTATA_PREFIX_COVER at a pre-fix cover.py.
_PREFIX = os.environ.get("HOTATA_PREFIX_COVER")
if _PREFIX:
    import importlib.util

    _spec = importlib.util.spec_from_file_location("hotata.cover", _PREFIX)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["hotata.cover"] = _mod
    _spec.loader.exec_module(_mod)

cover_mod = __import__("hotata.cover", fromlist=["HotataAirerCover"])
models_mod = __import__("hotata.models", fromlist=["HotataDevice"])
const_mod = __import__("hotata.const", fromlist=["DEFAULT_DESCENT_TIME"])

HotataAirerCover = cover_mod.HotataAirerCover
HotataError = cover_mod.HotataError
HotataDevice = models_mod.HotataDevice

MOTOR_STOP = cover_mod.MOTOR_STOP
MOTOR_OPEN = cover_mod.MOTOR_OPEN
MOTOR_CLOSE = cover_mod.MOTOR_CLOSE

TIMERS = ha_stub.TIMERS


def reset_timers():
    """Drop recorded timers so each case sees only its own."""
    del TIMERS[:]


def live_timers():
    """Timers that were armed and never cancelled."""
    return [t for t in TIMERS if not t["cancelled"]]


def make_device(iot_id="dev-airer", properties=None):
    """A HotataDevice wired the way the coordinator builds it."""
    return HotataDevice(
        iot_id=iot_id,
        name="晾衣架",
        product_key="PK_AIRER",
        device_name="airer",
        online=True,
        raw={"productName": "HotataAirer", "iotId": iot_id},
        properties=dict(properties or {}),
    )


class FakeRuntime:
    """Stands in for coordinator.DeviceRuntime position-simulation state."""

    def __init__(self, descent_time=40, simulated_position=100,
                 invert_direction=False):
        self.descent_time = descent_time
        self.simulated_position = simulated_position
        self.closing_start = None
        self.target_position = None
        # Per-device direction preference (issue #15). Tests flip it directly.
        self.invert_direction = invert_direction
        self.saved = None

    async def async_set_invert_direction(self, value):
        self.invert_direction = bool(value)
        self.saved = {"invert_direction": bool(value)}


class FakeAccount:
    def __init__(self):
        self.bumped = 0

    def bump_active_window(self):
        self.bumped += 1


class FakeCoordinator:
    """Minimum surface HotataEntity / HotataAirerCover touch."""

    def __init__(self, device=None, runtime=None, descent_time=40):
        self.hass = types.SimpleNamespace()
        self.device = device or make_device()
        self._runtime = runtime or FakeRuntime(descent_time=descent_time)
        self.data = {self.device.iot_id: self.device}
        self.account = FakeAccount()
        self.refreshes = 0

    def runtime(self, iot_id):
        return self._runtime

    async def async_request_refresh(self):
        self.refreshes += 1


class RecordingCover(HotataAirerCover):
    """HotataAirerCover with async_set_property replaced by a recorder.

    Keeps the real HA-visible surface (properties, timers, state writes) while
    removing the cloud call, so command values can be asserted directly.
    """

    def __init__(self, coordinator, device, fail_on=None):
        super().__init__(coordinator, device)
        self.commands = []
        # fail_on maps a property identifier to the HotataError it should raise.
        self.fail_on = dict(fail_on or {})

    async def async_set_property(self, identifier, value, **kwargs):
        if identifier in self.fail_on:
            raise self.fail_on[identifier]
        self.commands.append((identifier, value))


def build(coordinator=None, device=None, fail_on=None, descent_time=40,
          position=100, simulated=None):
    """Return (coordinator, entity) with positions primed for a scenario."""
    device = device or make_device()
    coordinator = coordinator or FakeCoordinator(device, descent_time=descent_time)
    entity = RecordingCover(coordinator, device, fail_on=fail_on)
    entity._position = position
    coordinator._runtime.simulated_position = (
        position if simulated is None else simulated
    )
    return coordinator, entity


def run_auto_stop(entity):
    """Fire the most recently armed auto-stop callback synchronously."""
    timers = live_timers()
    assert timers, "no auto-stop timer was armed"
    return timers[-1]["action"](None)


# ---- tiny assertion helpers (no pytest dependency, like test_v4_account) ----

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
