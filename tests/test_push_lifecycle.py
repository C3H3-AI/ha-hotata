"""生命周期护栏：MQTT 客户端必须在事件循环之外停掉。

Run: python3 tests/test_push_lifecycle.py
"""
import sys
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

import ha_stub  # noqa: E402,F401

PKG = REPO_ROOT / "custom_components"
sys.path.insert(0, str(PKG))
_pkg = types.ModuleType("hotata")
_pkg.__path__ = [str(PKG / "hotata")]
sys.modules.setdefault("hotata", _pkg)

import hotata.coordinator as cm  # noqa: E402
import hotata.mqtt_push as mp  # noqa: E402

R = __import__("cover_harness").Results()


def test_stop_never_sleeps():
    """stop() 曾经用 time.sleep(0.5) 等 unbind，阻塞事件循环。"""
    source = Path(mp.__file__).read_text(encoding="utf-8")
    R.check("stop() does not time.sleep", "time.sleep(" in source, False)
    R.check("unbind waits bounded", "wait_for_publish" in source, True)


def test_stop_releases_the_socket_and_thread():
    class Info:
        waited = None

        def wait_for_publish(self, timeout=None):
            Info.waited = timeout

    class Client:
        def __init__(self):
            self.published = []
            self.disconnected = False
            self.stopped = False
            self._thread = None

        def publish(self, topic, payload, qos=0):
            self.published.append(topic)
            return Info()

        def disconnect(self):
            self.disconnected = True

        def loop_stop(self):
            self.stopped = True

    c = mp.HotataPushClient(
        get_iot_token=lambda: "t",
        on_properties_pushed=lambda *args: None,
    )
    fake = Client()
    c._client = fake
    c._product_key = "pk"
    c._device_name = "dn"
    c._base_id = "dn&pk"
    c._bound = True

    c.stop()

    R.check(
        "unbind published",
        any("account/unbind" in t for t in fake.published),
        True,
    )
    R.check("bounded wait used", Info.waited, 2)
    R.check("disconnected", fake.disconnected, True)
    R.check("loop stopped (joins thread)", fake.stopped, True)
    R.check("client released", c._client, None)


def test_rebuild_stops_via_executor():
    """重建路径不能在事件循环里同步 stop()。"""
    source = Path(cm.__file__).read_text(encoding="utf-8")
    R.check(
        "rebuild uses executor for stop",
        "async_add_executor_job(self.async_stop_push)" in source,
        True,
    )


def test_reference_cleared_only_after_stop():
    events = []

    class Client:
        def stop(self):
            events.append("stop")

    class C:
        push_client = Client()
        push_bound = True

    co = C()
    cm.HotataCoordinator.async_stop_push(co)
    R.check("client stopped before being dropped", events, ["stop"])
    R.check("reference cleared", co.push_client, None)


def main():
    test_stop_never_sleeps()
    test_stop_releases_the_socket_and_thread()
    test_rebuild_stops_via_executor()
    test_reference_cleared_only_after_stop()
    return R.report("Push lifecycle")


if __name__ == "__main__":
    sys.exit(main())
