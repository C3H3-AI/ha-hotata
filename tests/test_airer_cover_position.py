"""Regression tests for issue #13: an airer request must always reach the motor.

Root cause guarded here: `self._position or 100` treated the legitimate device
position 0 as falsy and substituted 100, so a HomeKit "open" tap
(async_set_cover_position(position=100)) hit `target == current` and was
dropped silently — no motor command, no log.

Direction, as of the 2026-10-07 decision (issue #15): the cover is
``device_class: awning`` and 打开 (open) LOWERS the rail, so

    Home Assistant 0 %   = rail raised (device 100)   = 已关闭
    Home Assistant 100 % = rail lowered (device 0)    = 已打开

Open is therefore the *timed* movement (the rail descends and auto-stops);
close is the instant one. ``build(position=…)`` still takes the DEVICE
coordinate, and assertions prefer ``current_cover_position`` (the percentage
Home Assistant shows) so the intent stays readable.

Every assertion in this file fails on the pre-fix code and passes after it,
except where a case is explicitly marked as a preservation guard.

Run from anywhere:  python3 tests/test_airer_cover_position.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cover_harness import (  # noqa: E402
    MOTOR_CLOSE,
    MOTOR_OPEN,
    MOTOR_STOP,
    HotataError,
    RecordingCover,
    Results,
    build,
    live_timers,
    reset_timers,
    run_auto_stop,
)

R = Results()
MODE = "MotorControlMode"


async def test_closed_cover_opens_on_open_request():
    """The issue: an open request from the closed state must reach the motor.

    Closed is the rail at the top (device 100); opening lowers it, so the
    command is MOTOR_CLOSE and the descent timer is armed.
    """
    reset_timers()
    _, ent = build(position=100)
    await ent.async_set_cover_position(position=100)
    R.check("closed->100 emits MOTOR_CLOSE", ent.commands, [(MODE, MOTOR_CLOSE)])
    R.check("closed->100 arms one descent timer", len(live_timers()), 1)
    await run_auto_stop(ent)
    R.check("closed->100 reads 100 % once arrived",
            ent.current_cover_position, 100)


async def test_closed_cover_opens_via_open_cover():
    """HomeKit icon tap can also route straight to async_open_cover."""
    reset_timers()
    _, ent = build(position=100)
    await ent.async_open_cover()
    R.check("open_cover from closed emits MOTOR_CLOSE",
            ent.commands, [(MODE, MOTOR_CLOSE)])
    await run_auto_stop(ent)
    R.check("open_cover from closed ends at 100 %",
            ent.current_cover_position, 100)


async def test_closed_cover_does_not_emit_reverse_command():
    """Pre-fix, a falsy position made `current` 100, so an open request reversed."""
    reset_timers()
    _, ent = build(position=100)
    await ent.async_set_cover_position(position=100)
    emitted = [v for _, v in ent.commands]
    R.check("never emits MOTOR_OPEN when asked to open", MOTOR_OPEN in emitted, False)


async def test_homekit_close_from_closed_is_idempotent():
    """HomeKit close sends position=0; on a closed cover that is a no-op."""
    reset_timers()
    _, ent = build(position=100)
    await ent.async_set_cover_position(position=0)
    R.check("closed->0 emits nothing", ent.commands, [])
    R.check("closed->0 arms no timer", live_timers(), [])
    R.check("closed->0 keeps position 0", ent.current_cover_position, 0)


async def test_drag_from_the_bottom_moves_the_right_way():
    """Issue #13 step 4: dragging to a position must move in the right
    direction. Device 0 (fully open) dragged to 60 % must rise, not descend."""
    reset_timers()
    _, ent = build(position=0)
    await ent.async_set_cover_position(position=60)
    R.check("drag 100->60 emits MOTOR_OPEN", ent.commands, [(MODE, MOTOR_OPEN)])
    R.check("drag 100->60 leaves no pending descent", live_timers(), [])


async def test_short_open_uses_minimum_duration():
    """A sliver of travel must use the 1s minimum, not the full descent_time."""
    reset_timers()
    co, ent = build(position=1, descent_time=40)
    await ent.async_open_cover()
    timers = live_timers()
    R.check("open from device 1 emits MOTOR_CLOSE",
            ent.commands, [(MODE, MOTOR_CLOSE)])
    R.check("open from device 1 arms one timer", len(timers), 1)
    R.check("open from device 1 uses 1s minimum",
            timers[0]["delay"] if timers else None, 1)


async def test_close_is_instant_because_it_rises():
    """Closing raises the rail to the top; the simulation stops there at once."""
    reset_timers()
    _, ent = build(position=0, descent_time=40)
    await ent.async_close_cover()
    R.check("close emits MOTOR_OPEN", ent.commands, [(MODE, MOTOR_OPEN)])
    R.check("close arms no timer", live_timers(), [])
    R.check("close reads 0 %", ent.current_cover_position, 0)


async def test_set_position_to_the_top_is_a_short_rise():
    """Sliding to 0 % raises the rail; the move is instant and emits MOTOR_OPEN."""
    reset_timers()
    _, ent = build(position=10, descent_time=40)
    await ent.async_set_cover_position(position=0)
    R.check("10->0 emits MOTOR_OPEN", ent.commands, [(MODE, MOTOR_OPEN)])
    R.check("10->0 arms no timer", live_timers(), [])
    R.check("10->0 reads 0 %", ent.current_cover_position, 0)


async def test_set_position_records_target_for_auto_stop():
    """After a set_position descent, auto-stop must land on the target."""
    reset_timers()
    co, ent = build(position=100, descent_time=40)
    await ent.async_set_cover_position(position=25)
    R.check("0->25 emits MOTOR_CLOSE", ent.commands, [(MODE, MOTOR_CLOSE)])
    R.check("0->25 stores the device target", co._runtime.target_position, 75)
    await run_auto_stop(ent)
    R.check("auto-stop lands on target 25 %", ent.current_cover_position, 25)
    R.check("auto-stop syncs simulated position",
            co._runtime.simulated_position, 75)


async def test_auto_stop_without_target_keeps_simulated_position():
    """B2 guard: a cancelled descent that still fires auto-stop must not snap
    the rail to the bottom with a bare `else 0`."""
    reset_timers()
    co, ent = build(position=0, descent_time=40)
    # Arm a real device descent: park at the top and ask for 30 %.
    ent._position = 100
    co._runtime.simulated_position = 100
    await ent.async_set_cover_position(position=30)
    R.check("descent armed a timer", len(live_timers()), 1)
    # Simulate _cancel_stop_timer having cleared the target while the callback
    # was already scheduled (the case that hit the bare `else 0`).
    co._runtime.target_position = None
    co._runtime.simulated_position = 30
    await run_auto_stop(ent)
    R.check("auto-stop w/o target keeps 30", ent._position, 30)
    R.check("auto-stop w/o target syncs runtime", co._runtime.simulated_position, 30)


async def test_state_is_written_on_command_success():
    """Each successful command must publish state so HomeKit's
    CurrentPosition updates without waiting for the next poll."""
    for label, call in (
        ("open", lambda e: e.async_open_cover()),
        ("close", lambda e: e.async_close_cover()),
        ("stop", lambda e: e.async_stop_cover()),
        ("set_position", lambda e: e.async_set_cover_position(position=100)),
    ):
        reset_timers()
        # Arm a state where the command is not a no-op: open descends from the
        # top, close rises from the bottom, set_position mirrors open.
        _, ent = build(position=100 if label in ("open", "set_position") else 0)
        ent.ha_state_writes = 0
        await call(ent)
        R.check(f"{label} writes state once", ent.ha_state_writes, 1)


async def test_no_state_write_on_skipped_or_failed_command():
    """A dropped (target == current) or failed command must not write state."""
    reset_timers()
    _, ent = build(position=100)                     # already at HA 0 %
    ent.ha_state_writes = 0
    await ent.async_set_cover_position(position=0)   # skipped
    R.check("skipped command writes no state", ent.ha_state_writes, 0)

    reset_timers()
    _, ent = build(position=100, fail_on={MODE: HotataError("cloud down")})
    ent.ha_state_writes = 0
    await ent.async_open_cover()                     # fails
    R.check("failed command emits nothing", ent.commands, [])
    R.check("failed command writes no state", ent.ha_state_writes, 0)


async def test_skipped_command_is_logged():
    """The original bug was invisible in logs; the skip must be observable."""
    import logging

    reset_timers()
    _, ent = build(position=100)
    records = []

    class Sink(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("hotata.cover")
    handler = Sink()
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        await ent.async_set_cover_position(position=0)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    R.check("skip is logged", any("skipping command" in m for m in records), True)


async def test_no_command_after_lost_broadcast_is_recoverable():
    """A missed coordinator broadcast must not permanently wedge the entity:
    an explicit open request from the closed state still works."""
    reset_timers()
    co, ent = build(position=100, simulated=0)   # runtime out of sync
    await ent.async_set_cover_position(position=100)
    R.check("recovers and opens", ent.commands, [(MODE, MOTOR_CLOSE)])


async def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    for fn in tests:
        await fn()
    code = R.report("PR#14 position regression (issue #13)")
    print(f"total checks run: {R.passed + len(R.failed)}")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
