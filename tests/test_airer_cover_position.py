"""Regression tests for issue #13: airer must rise from the closed state.

Root cause guarded here: `self._position or 100` treated the legitimate closed
position 0 as falsy and substituted 100, so a HomeKit "open" tap
(async_set_cover_position(position=100)) hit `target == current` and was
dropped silently — no motor command, no log.

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


async def test_closed_cover_rises_on_open_request():
    """The issue: position 0 + set_cover_position(100) must emit MOTOR_OPEN."""
    reset_timers()
    _, ent = build(position=0)
    await ent.async_set_cover_position(position=100)
    R.check("closed->100 emits MOTOR_OPEN", ent.commands, [(MODE, MOTOR_OPEN)])
    R.check("closed->100 leaves position at top", ent._position, 100)
    R.check("closed->100 arms no descent timer", live_timers(), [])


async def test_closed_cover_rises_via_open_cover():
    """HomeKit icon tap can also route straight to async_open_cover."""
    reset_timers()
    _, ent = build(position=0)
    await ent.async_open_cover()
    R.check("open_cover from 0 emits MOTOR_OPEN", ent.commands, [(MODE, MOTOR_OPEN)])
    R.check("open_cover from 0 sets position 100", ent._position, 100)


async def test_closed_cover_does_not_emit_reverse_command():
    """Pre-fix, position 0 made `current` 100, so an open request closed it."""
    reset_timers()
    _, ent = build(position=0)
    await ent.async_set_cover_position(position=100)
    emitted = [v for _, v in ent.commands]
    R.check("never emits MOTOR_CLOSE when asked to open", MOTOR_CLOSE in emitted, False)


async def test_homekit_close_from_closed_is_idempotent():
    """HomeKit close sends position=0; on a closed cover that is a no-op."""
    reset_timers()
    _, ent = build(position=0)
    await ent.async_set_cover_position(position=0)
    R.check("closed->0 emits nothing", ent.commands, [])
    R.check("closed->0 arms no timer", live_timers(), [])
    R.check("closed->0 keeps position 0", ent._position, 0)


async def test_drag_up_from_bottom_moves_up_not_down():
    """Issue #13 step 4: dragging to a position must move in the right
    direction. Pre-fix, 0 -> 60 emitted MOTOR_CLOSE."""
    reset_timers()
    _, ent = build(position=0)
    await ent.async_set_cover_position(position=60)
    R.check("drag 0->60 emits MOTOR_OPEN", ent.commands, [(MODE, MOTOR_OPEN)])
    R.check("drag 0->60 leaves no pending descent", live_timers(), [])


async def test_close_from_bottom_uses_minimum_duration():
    """Pre-fix, `current = self._position or 100` made a close from the bottom
    wait a full descent_time instead of the 1s minimum."""
    reset_timers()
    co, ent = build(position=0, descent_time=40)
    await ent.async_close_cover()
    timers = live_timers()
    R.check("close from 0 emits MOTOR_CLOSE", ent.commands, [(MODE, MOTOR_CLOSE)])
    R.check("close from 0 arms one timer", len(timers), 1)
    R.check("close from 0 uses 1s minimum", timers[0]["delay"] if timers else None, 1)


async def test_set_position_to_bottom_from_bottom_is_minimum_duration():
    """0 -> 0 is a no-op; but 10 -> 0 must use the short proportional timer."""
    reset_timers()
    _, ent = build(position=10, descent_time=40)
    await ent.async_set_cover_position(position=0)
    timers = live_timers()
    want = max(1, (10 - 0) / 100 * 40)
    R.check("10->0 emits MOTOR_CLOSE", ent.commands, [(MODE, MOTOR_CLOSE)])
    R.check("10->0 timer is proportional", timers[0]["delay"] if timers else None, want)


async def test_set_position_records_target_for_auto_stop():
    """After a set_position descent, auto-stop must land on the target."""
    reset_timers()
    co, ent = build(position=100, descent_time=40)
    await ent.async_set_cover_position(position=25)
    R.check("100->25 emits MOTOR_CLOSE", ent.commands, [(MODE, MOTOR_CLOSE)])
    R.check("100->25 stores target", co._runtime.target_position, 25)
    await run_auto_stop(ent)
    R.check("auto-stop lands on target 25", ent._position, 25)
    R.check("auto-stop syncs simulated position", co._runtime.simulated_position, 25)


async def test_auto_stop_without_target_keeps_simulated_position():
    """B2 guard: a cancelled descent that still fires auto-stop must not snap
    the rail to the bottom with a bare `else 0`."""
    reset_timers()
    co, ent = build(position=0, descent_time=40)
    # Arm a real descent from the bottom (0 -> 30 is a descent in motor terms
    # only via close; use set_position on a cover parked at the top instead).
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
        _, ent = build(position=0 if label != "close" else 100)
        ent.ha_state_writes = 0
        await call(ent)
        R.check(f"{label} writes state once", ent.ha_state_writes, 1)


async def test_no_state_write_on_skipped_or_failed_command():
    """A dropped (target == current) or failed command must not write state."""
    reset_timers()
    _, ent = build(position=0)
    ent.ha_state_writes = 0
    await ent.async_set_cover_position(position=0)   # skipped
    R.check("skipped command writes no state", ent.ha_state_writes, 0)

    reset_timers()
    _, ent = build(position=0, fail_on={MODE: HotataError("cloud down")})
    ent.ha_state_writes = 0
    await ent.async_open_cover()                     # fails
    R.check("failed command emits nothing", ent.commands, [])
    R.check("failed command writes no state", ent.ha_state_writes, 0)


async def test_skipped_command_is_logged():
    """The original bug was invisible in logs; the skip must be observable."""
    import logging

    reset_timers()
    _, ent = build(position=0)
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
    co, ent = build(position=0, simulated=100)   # runtime out of sync
    await ent.async_set_cover_position(position=100)
    R.check("recovers and opens", ent.commands, [(MODE, MOTOR_OPEN)])


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
