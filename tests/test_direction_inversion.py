"""The per-device travel-direction preference (issue #15).

Decided 2026-10-07 with the maintainer (issue #15). The cover is
``device_class: awning``, so Home Assistant draws 展开/合拢 arrows instead of a
hard-wired ⬆️=打开, and the natural reading is "展开 = 放下 = 打开":

    off (default)  open (展开) LOWERS the rail: HA 100 % = rail down
    on (行程反转)  open RAISES the rail:       HA 100 % = rail up
                   close (收起) then lowers it

Internal coordinates never change: 100 always means "device: rail at the top",
0 always means "device: rail at the bottom", and the simulation, the auto-stop
timer and the descent-time number all stay in those terms. Only the values and
commands that Home Assistant sees are translated.

Run from anywhere:  python3 tests/test_direction_inversion.py
"""
import asyncio
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

HotataDevice = __import__("hotata.models", fromlist=["HotataDevice"]).HotataDevice
cover_mod = __import__("hotata.cover", fromlist=["X"])
switch_mod = __import__("hotata.switch", fromlist=["_switches_for_device"])
coordinator_mod = __import__("hotata.coordinator", fromlist=["DeviceRuntime"])
DeviceRuntime = coordinator_mod.DeviceRuntime
DIRECTION_SEMANTICS_VERSION = coordinator_mod.DIRECTION_SEMANTICS_VERSION

MOTOR_STOP = cover_mod.MOTOR_STOP
MOTOR_OPEN = cover_mod.MOTOR_OPEN      # 上升 (device: raise)
MOTOR_CLOSE = cover_mod.MOTOR_CLOSE    # 下降 (device: lower)

R = cover_harness.Results()


def build(inverted=False, position=100, descent_time=40):
    coordinator, entity = cover_harness.build(
        descent_time=descent_time, position=position
    )
    coordinator._runtime.invert_direction = inverted
    return coordinator, entity


def commands(entity):
    return list(entity.commands)


# ---- default direction -----------------------------------------------------

def test_default_direction_reports_the_rail_state():
    coordinator, entity = build(inverted=False, position=0)
    R.check("rail down reads as open", entity.current_cover_position, 100)
    R.check("rail down is not closed", entity.is_closed, False)

    coordinator, entity = build(inverted=False, position=100)
    R.check("rail up reads as closed", entity.current_cover_position, 0)
    R.check("rail up is closed", entity.is_closed, True)


def test_default_open_lowers_the_rail():
    coordinator, entity = build(inverted=False, position=100)
    asyncio.run(entity.async_open_cover())
    R.check("open sends 下降", commands(entity), [("MotorControlMode", MOTOR_CLOSE)])
    R.check("targets the bottom", coordinator._runtime.target_position, 0)
    asyncio.run(cover_harness.run_auto_stop(entity))
    R.check("arrives open at 100 %", entity.current_cover_position, 100)
    R.check("no longer closed", entity.is_closed, False)


def test_default_close_raises_the_rail():
    coordinator, entity = build(inverted=False, position=0)
    asyncio.run(entity.async_close_cover())
    R.check("close sends 上升", commands(entity), [("MotorControlMode", MOTOR_OPEN)])
    R.check("ends at the device top", entity._position, 100)
    R.check("closed at the top", entity.is_closed, True)
    R.check("reads 0 %", entity.current_cover_position, 0)


def test_default_set_position_mirrors_onto_device_coordinates():
    coordinator, entity = build(inverted=False, position=100)
    asyncio.run(entity.async_set_cover_position(position=30))
    R.check("HA 30 % lowers the rail",
            commands(entity), [("MotorControlMode", MOTOR_CLOSE)])
    R.check("device target is 70", coordinator._runtime.target_position, 70)

    coordinator, entity = build(inverted=False, position=0)
    asyncio.run(entity.async_set_cover_position(position=0))
    R.check("HA 0 % raises the rail",
            commands(entity), [("MotorControlMode", MOTOR_OPEN)])
    R.check("ends at the device top", entity._position, 100)


# ---- reversed direction ----------------------------------------------------

def test_reversed_reports_the_mirrored_position():
    coordinator, entity = build(inverted=True, position=0)
    R.check("rail down reads as closed", entity.current_cover_position, 0)
    R.check("rail down is closed", entity.is_closed, True)

    coordinator, entity = build(inverted=True, position=100)
    R.check("rail up reads as open", entity.current_cover_position, 100)
    R.check("rail up is not closed", entity.is_closed, False)


def test_reversed_open_raises_the_rail():
    coordinator, entity = build(inverted=True, position=0)
    asyncio.run(entity.async_open_cover())
    R.check("open sends 上升", commands(entity), [("MotorControlMode", MOTOR_OPEN)])
    R.check("ends at the device top", entity._position, 100)
    R.check("arrives open at 100 %", entity.current_cover_position, 100)
    R.check("no longer closed", entity.is_closed, False)


def test_reversed_close_lowers_the_rail():
    coordinator, entity = build(inverted=True, position=100, descent_time=40)
    asyncio.run(entity.async_close_cover())
    R.check("close sends 下降", commands(entity), [("MotorControlMode", MOTOR_CLOSE)])
    R.check("targets the bottom", coordinator._runtime.target_position, 0)
    # The descent only moves on auto-stop; until then the cloud echo drives the
    # motion flags.
    entity.device.properties["MotorControlMode"] = {"value": MOTOR_CLOSE}
    R.check("reports closing while it travels", entity.is_closing, True)
    asyncio.run(cover_harness.run_auto_stop(entity))
    R.check("arrives closed at 0 %", entity.current_cover_position, 0)
    R.check("closed", entity.is_closed, True)


def test_reversed_set_position_uses_device_commands():
    """The translation happens once; the branches stay device-coordinate."""
    coordinator, entity = build(inverted=True, position=0)
    asyncio.run(entity.async_set_cover_position(position=100))
    R.check("HA 100% raises the rail",
            commands(entity), [("MotorControlMode", MOTOR_OPEN)])
    R.check("ends at the device top", entity._position, 100)

    coordinator, entity = build(inverted=True, position=100)
    asyncio.run(entity.async_set_cover_position(position=0))
    R.check("HA 0% lowers the rail",
            commands(entity), [("MotorControlMode", MOTOR_CLOSE)])
    R.check("device target is the bottom",
            coordinator._runtime.target_position, 0)


def test_reversed_motion_flags_follow_ha_semantics():
    coordinator, entity = build(inverted=True, position=0)
    entity.device.properties["MotorControlMode"] = {"value": MOTOR_OPEN}
    R.check("device 上升 reads as opening", entity.is_opening, True)
    R.check("device 上升 is not closing", entity.is_closing, False)
    entity.device.properties["MotorControlMode"] = {"value": MOTOR_CLOSE}
    R.check("device 下降 reads as closing", entity.is_closing, True)
    R.check("device 下降 is not opening", entity.is_opening, False)


# ---- the switch entity -----------------------------------------------------

def make_switch_device(inverted=False):
    device = HotataDevice(
        iot_id="d1",
        name="晾衣架",
        product_key="a1kM9JAZ7aQ",
        device_name="d1",
        online=True,
        raw={},
        properties={"MotorControlMode": {"value": MOTOR_STOP}},
        thing_model={"properties": [{"identifier": "MotorControlMode"}]},
    )
    runtime = cover_harness.FakeRuntime(invert_direction=inverted)
    coordinator = cover_harness.FakeCoordinator(device, runtime=runtime)
    return coordinator, device


def test_default_motion_flags_read_the_lowering_command():
    coordinator, entity = build(inverted=False, position=100)
    entity.device.properties["MotorControlMode"] = {"value": MOTOR_CLOSE}
    R.check("device 下降 reads as opening", entity.is_opening, True)
    R.check("device 下降 is not closing", entity.is_closing, False)
    entity.device.properties["MotorControlMode"] = {"value": MOTOR_OPEN}
    R.check("device 上升 reads as closing", entity.is_closing, True)


def test_invert_switch_is_created_for_covers():
    coordinator, device = make_switch_device()
    keys = [getattr(getattr(e, "entity_description", None), "key", None)
            or getattr(e, "identifier", None)
            or getattr(e, "_attr_name", None)
            for e in switch_mod._switches_for_device(coordinator, device)]
    R.check("direction switch present", "行程反转" in keys, True)


def test_invert_switch_toggles_and_persists():
    coordinator, device = make_switch_device()
    entity = switch_mod.HotataInvertDirectionSwitch(coordinator, device)
    R.check("starts off", entity.is_on, False)
    from homeassistant.const import EntityCategory as StubEntityCategory
    R.check("entity is a config entity",
            entity._attr_entity_category, StubEntityCategory.CONFIG)
    R.check("usable while offline", entity.available, True)
    asyncio.run(entity.async_turn_on())
    R.check("turned on", entity.is_on, True)
    R.check("runtime flag set", coordinator._runtime.invert_direction, True)
    R.check("runtime persisted", coordinator._runtime.saved,
            {"invert_direction": True})
    R.check("a refresh was requested so the cover re-reads now",
            coordinator.refreshes >= 1, True)
    asyncio.run(entity.async_turn_off())
    R.check("turned off", entity.is_on, False)


# ---- persistence -----------------------------------------------------------

def test_preferences_do_not_overwrite_each_other():
    """Saving one per-device preference must keep the other."""
    asyncio.run(_run_persistence())


async def _run_persistence():
    hass = types.SimpleNamespace()
    first = DeviceRuntime(hass=hass, iot_id="dev-1")
    await first.async_load()
    await first.async_set_invert_direction(True)
    await first.async_set_descent_time(75)

    reloaded = DeviceRuntime(hass=hass, iot_id="dev-1")
    await reloaded.async_load()
    R.check("invert survived the descent-time write",
            reloaded.invert_direction, True)
    R.check("descent time survived", reloaded.descent_time, 75)

    await reloaded.async_set_invert_direction(False)
    third = DeviceRuntime(hass=hass, iot_id="dev-1")
    await third.async_load()
    R.check("direction toggled back", third.invert_direction, False)
    R.check("descent time still there", third.descent_time, 75)


def test_stored_choice_is_migrated_once():
    """4.0.14 flipped what "off" means; old stores migrate to the new default.

    Old semantics: ON = "make 打开 lower the rail" — now the default, so ON
    becomes OFF. Old OFF was the *old* default (打开 raises), and someone who
    never touched the switch has no preference to preserve, so it also lands on
    the new default. The migration writes back and runs exactly once.
    """
    asyncio.run(_run_migration())


async def _run_migration():
    hass = types.SimpleNamespace()
    legacy = DeviceRuntime(hass=hass, iot_id="dev-migrate")
    await legacy.async_load()
    # Simulate a store written by 4.0.13: the flag only, no semantics version.
    await legacy.store.async_save({"descent_time": 40, "invert_direction": True})

    migrated = DeviceRuntime(hass=hass, iot_id="dev-migrate")
    await migrated.async_load()
    R.check("old ON becomes the new default (off)", migrated.invert_direction, False)
    R.check("descent time carried over", migrated.descent_time, 40)
    stored = await migrated.store.async_load()
    R.check("migration was written back",
            stored.get("direction_semantics"), DIRECTION_SEMANTICS_VERSION)

    again = DeviceRuntime(hass=hass, iot_id="dev-migrate")
    await again.async_load()
    R.check("migration is not applied twice", again.invert_direction, False)

    # Someone who never touched the switch (their store only has descent_time,
    # written by the number entity) adopts the new default too.
    await again.store.async_save({"descent_time": 30, "invert_direction": False})
    legacy_off = DeviceRuntime(hass=hass, iot_id="dev-migrate")
    await legacy_off.async_load()
    R.check("old OFF adopts the new default", legacy_off.invert_direction, False)
    R.check("descent time still read", legacy_off.descent_time, 30)


def test_new_device_keeps_the_default_direction():
    asyncio.run(_run_new_device())


async def _run_new_device():
    hass = types.SimpleNamespace()
    fresh = DeviceRuntime(hass=hass, iot_id="dev-fresh")
    await fresh.async_load()
    R.check("fresh device uses the new default", fresh.invert_direction, False)


def main():
    test_default_direction_reports_the_rail_state()
    test_default_open_lowers_the_rail()
    test_default_close_raises_the_rail()
    test_default_set_position_mirrors_onto_device_coordinates()
    test_default_motion_flags_read_the_lowering_command()
    test_reversed_reports_the_mirrored_position()
    test_reversed_open_raises_the_rail()
    test_reversed_close_lowers_the_rail()
    test_reversed_set_position_uses_device_commands()
    test_reversed_motion_flags_follow_ha_semantics()
    test_invert_switch_is_created_for_covers()
    test_invert_switch_toggles_and_persists()
    test_preferences_do_not_overwrite_each_other()
    test_stored_choice_is_migrated_once()
    test_new_device_keeps_the_default_direction()
    return R.report("Travel direction preference")


if __name__ == "__main__":
    sys.exit(main())
