"""Preservation baseline: non-bug inputs must behave identically after PR #14.

Every assertion here describes behaviour that was already correct before the
fix. The point is to prove the position work did not change command values,
timer durations or the coordinator-driven runtime convergence for inputs that
never hit the `0`-is-falsy bug.

Run from anywhere:  python3 tests/test_airer_cover_preservation.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cover_harness import (  # noqa: E402
    MOTOR_CLOSE,
    MOTOR_OPEN,
    MOTOR_STOP,
    Results,
    build,
    live_timers,
    reset_timers,
)

R = Results()
MODE = "MotorControlMode"


async def test_open_from_midway():
    reset_timers()
    _, ent = build(position=40)
    await ent.async_open_cover()
    R.check("open from 40", ent.commands, [(MODE, MOTOR_OPEN)])
    R.check("open from 40 -> 100", ent._position, 100)


async def test_close_from_top_uses_full_descent():
    reset_timers()
    co, ent = build(position=100, descent_time=40)
    await ent.async_close_cover()
    timers = live_timers()
    R.check("close from 100 emits MOTOR_CLOSE", ent.commands, [(MODE, MOTOR_CLOSE)])
    R.check("close from 100 full descent", timers[0]["delay"] if timers else None, 40)
    R.check("close from 100 targets bottom", co._runtime.target_position, 0)
    R.check("close from 100 starts descent clock",
            co._runtime.closing_start is not None, True)


async def test_close_from_midway_is_proportional():
    reset_timers()
    _, ent = build(position=50, descent_time=40)
    await ent.async_close_cover()
    timers = live_timers()
    R.check("close from 50 half descent",
            timers[0]["delay"] if timers else None, 20.0)


async def test_stop_midway_freezes_estimate():
    reset_timers()
    co, ent = build(position=100, descent_time=40)
    await ent.async_close_cover()
    # Pretend 10s of the 40s descent elapsed.
    co._runtime.closing_start = time.time() - 10
    await ent.async_stop_cover()
    R.check("stop emits MOTOR_STOP", ent.commands[-1], (MODE, MOTOR_STOP))
    R.check("stop freezes around 75", 70 <= ent._position <= 80, True)
    R.check("stop clears closing_start", co._runtime.closing_start, None)
    R.check("stop syncs simulated position",
            co._runtime.simulated_position, ent._position)


async def test_stop_while_idle_is_harmless():
    reset_timers()
    co, ent = build(position=60)
    await ent.async_stop_cover()
    R.check("idle stop emits MOTOR_STOP", ent.commands, [(MODE, MOTOR_STOP)])
    R.check("idle stop keeps position", ent._position, 60)
    R.check("idle stop leaves closing_start None", co._runtime.closing_start, None)


async def test_set_position_below_current_descends_proportionally():
    reset_timers()
    co, ent = build(position=100, descent_time=40)
    await ent.async_set_cover_position(position=20)
    timers = live_timers()
    R.check("100->20 emits MOTOR_CLOSE", ent.commands, [(MODE, MOTOR_CLOSE)])
    R.check("100->20 timer", timers[0]["delay"] if timers else None, 32.0)
    R.check("100->20 targets 20", co._runtime.target_position, 20)


async def test_set_position_above_current_opens():
    reset_timers()
    _, ent = build(position=20)
    await ent.async_set_cover_position(position=80)
    R.check("20->80 emits MOTOR_OPEN", ent.commands, [(MODE, MOTOR_OPEN)])
    R.check("20->80 -> 100", ent._position, 100)


async def test_coordinator_advances_descent_estimate():
    """During a descent the coordinator callback must interpolate position."""
    reset_timers()
    co, ent = build(position=100, descent_time=40)
    co._runtime.closing_start = time.time() - 20
    ent.property_value = lambda k: MOTOR_CLOSE
    ent._handle_coordinator_update()
    R.check("midway estimate ~50", 45 <= ent._position <= 55, True)
    R.check("estimate synced to runtime",
            co._runtime.simulated_position, ent._position)
    R.check("last motor mode tracked", ent._last_motor_mode, MOTOR_CLOSE)


async def test_coordinator_snaps_to_bottom_on_motor_stop_after_close():
    """Motor runs to its limit: the callback must snap the estimate to 0."""
    reset_timers()
    co, ent = build(position=25)
    ent._last_motor_mode = MOTOR_CLOSE
    co._runtime.closing_start = None
    ent.property_value = lambda k: MOTOR_STOP
    ent._handle_coordinator_update()
    R.check("motor stop after close -> 0", ent._position, 0)
    R.check("runtime synced to 0", co._runtime.simulated_position, 0)


async def test_coordinator_snaps_to_top_on_motor_stop_after_open():
    reset_timers()
    co, ent = build(position=25)
    ent._last_motor_mode = MOTOR_OPEN
    co._runtime.closing_start = None
    ent.property_value = lambda k: MOTOR_STOP
    ent._handle_coordinator_update()
    R.check("motor stop after open -> 100", ent._position, 100)
    R.check("runtime synced to 100", co._runtime.simulated_position, 100)


async def test_coordinator_converges_when_idle():
    """Idle polls pull _position back to the shared simulated position."""
    reset_timers()
    co, ent = build(position=0)
    co._runtime.simulated_position = 100
    ent._last_motor_mode = MOTOR_STOP
    ent.property_value = lambda k: MOTOR_STOP
    ent._handle_coordinator_update()
    R.check("idle converges to runtime", ent._position, 100)


async def test_cancel_stop_timer_then_open_leaves_no_stray_descent():
    reset_timers()
    co, ent = build(position=100, descent_time=40)
    await ent.async_close_cover()
    armed = live_timers()
    R.check("descent armed", len(armed), 1)
    await ent.async_open_cover()
    R.check("open cancels the descent timer", armed[0]["cancelled"], True)
    R.check("open clears the target", co._runtime.target_position, None)
    R.check("open clears closing_start", co._runtime.closing_start, None)


async def test_auto_stop_retries_on_failure():
    """A failed stop must re-arm a retry rather than free the motor."""
    reset_timers()
    co, ent = build(position=100, descent_time=40)
    await ent.async_close_cover()
    ent.fail_on = {MODE: __import__("hotata.cover", fromlist=["HotataError"]).HotataError("x")}
    before = len(live_timers())
    await ent._async_auto_stop_cover(None)
    after = live_timers()
    R.check("retry timer armed", len(after), before + 1)
    R.check("retry delay is 15s", after[-1]["delay"], 15)
    R.check("position untouched on failure", ent._position, 100)


async def test_curtain_v1_untouched():
    """Curtain machines use a different code path; guard against regressions."""
    reset_timers()
    from cover_harness import HotataDevice, FakeCoordinator
    from hotata.cover import HotataCurtainV1
    device = HotataDevice(
        iot_id="curtain1", name="窗帘", product_key="CURTAIN_V1",
        device_name="c", online=True, raw={"productName": "Curtain"},
        properties={"CurtainPosition": 0},
    )
    co = FakeCoordinator(device)
    ent = HotataCurtainV1(co, device)
    R.check("curtain v1 position 0", ent.current_cover_position, 0)
    R.check("curtain v1 is_closed at 0", ent.is_closed, True)


async def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    for fn in tests:
        await fn()
    code = R.report("PR#14 preservation baseline")
    print(f"total checks run: {R.passed + len(R.failed)}")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
