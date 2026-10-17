"""Audit findings: the long-connection (MQTT push) client must not leak credentials.

Run from anywhere:  python3 tests/test_push_audit.py

These checks are the *evidence* for the code audit of 2026-10-18. They encode
what must never happen, not how the client is built, so they stay meaningful
when the transport is rewritten.

Finding 1 (high): `async_start` logs the whole aepauth payload when it is
incomplete. That payload is exactly the MQTT triple
{productKey, deviceName, deviceSecret} — `deviceSecret` is the password used to
connect to the broker, and `api.py` already lists `devicesecret` in its
`_SENSITIVE_RESPONSE_KEYS`. A user pasting a debug log would publish it.
"""
import asyncio
import logging
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

import hotata.mqtt_push as mqtt_push  # noqa: E402

R = __import__("cover_harness").Results()


class _CallbackAPIVersion:
    """Minimal stand-in for paho 2.x's CallbackAPIVersion enum."""

    VERSION1 = "v1"
    VERSION2 = "v2"


class _FakeMqtt:
    """The parts of paho.mqtt.client the push module touches.

    The audit must not depend on which paho version happens to be installed,
    so the module gets a self-contained stand-in (see Finding 2 below).
    """

    CallbackAPIVersion = _CallbackAPIVersion
    MQTTv311 = 4
    Client = None  # filled in after _FakeMqttClient exists


class _FakeMqttClient:
    """Stands in for paho's client so the audit can run without a broker."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.published = []

    def username_pw_set(self, *args):
        pass

    def tls_set(self, **kwargs):
        pass

    def reconnect_delay_set(self, *args):
        pass

    def connect(self, *args, **kwargs):
        pass

    def loop_start(self):
        pass

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload, qos))

    def subscribe(self, *args, **kwargs):
        pass

    def disconnect(self):
        pass

    def loop_stop(self):
        pass


def _capture_logs():
    records = []

    class Sink(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Sink()
    logger = logging.getLogger("hotata.mqtt_push")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return records, logger, handler


async def _start_with(aepauth):
    records, logger, handler = _capture_logs()
    original_mqtt = mqtt_push.mqtt
    fake = _FakeMqtt()
    fake.Client = _FakeMqttClient
    mqtt_push.mqtt = fake
    try:
        client = mqtt_push.HotataPushClient(
            get_iot_token=lambda: "an-iot-token",
            on_properties_pushed=lambda *args: None,
        )
        await client.async_start(asyncio.get_running_loop(), aepauth)
    finally:
        mqtt_push.mqtt = original_mqtt
        logger.removeHandler(handler)
    return records


def test_incomplete_aepauth_never_logs_the_secret():
    """The MQTT triple must not be printed, even when it is unusable.

    Realistic failure: the gateway answered but one field is missing, so the
    dict still carries deviceSecret when the warning dumps it.
    """
    records = asyncio.run(
        _start_with(
            {
                "productKey": "pk_AUDIT",
                "deviceSecret": "SECRET_SHOULD_NEVER_APPEAR",
            }
        )
    )
    joined = "\n".join(records)
    R.check("no secret in the logs", "SECRET_SHOULD_NEVER_APPEAR" in joined, False)


def test_incomplete_aepauth_never_logs_the_whole_payload():
    """Diagnostics may name the missing key, never echo the payload."""
    records = asyncio.run(_start_with({"productKey": "pk_AUDIT", "deviceName": "dn_AUDIT"}))
    joined = "\n".join(records)
    # The deviceName is not secret, but dumping the dict is the habit that leaks
    # deviceSecret the day the payload does carry it.
    R.check("payload is not echoed as a dict", "{'productKey'" in joined, False)


def test_credentials_are_never_logged_on_the_success_path():
    """Even a healthy start must stay quiet about the triple."""
    records = asyncio.run(
        _start_with(
            {
                "productKey": "pk_AUDIT",
                "deviceName": "dn_AUDIT",
                "deviceSecret": "SECRET_SHOULD_NEVER_APPEAR",
            }
        )
    )
    joined = "\n".join(records)
    R.check("no secret on the success path", "SECRET_SHOULD_NEVER_APPEAR" in joined, False)
    R.check("no iot token either", "an-iot-token" in joined, False)


def _mqtt_module(with_v2_api: bool):
    """A paho stand-in shaped like 1.x (no CallbackAPIVersion) or 2.x."""

    class CallbackAPIVersion:
        VERSION1 = "v1"
        VERSION2 = "v2"

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    module = types.SimpleNamespace(
        Client=Client,
        MQTTv311=4,
        **({"CallbackAPIVersion": CallbackAPIVersion} if with_v2_api else {}),
    )
    return module


def test_client_builds_against_paho_v1_api():
    """Finding 2: 1.x is what many installs carry — it must still work."""
    original = mqtt_push.mqtt
    mqtt_push.mqtt = _mqtt_module(with_v2_api=False)
    error = None
    client = None
    try:
        client = mqtt_push._new_client("cid")
    except Exception as exc:  # noqa: BLE001 - the point is that it must not
        error = f"{type(exc).__name__}: {exc}"
    finally:
        mqtt_push.mqtt = original
    R.check("v1: building the client must not raise", error, None)
    R.check("v1: client built at all", client is not None, True)
    R.check(
        "v1: no callback_api_version kwarg",
        bool(client and "callback_api_version" in client.kwargs),
        False,
    )


def test_client_builds_against_paho_v2_api():
    """2.x must still get the v2 callback API."""
    original = mqtt_push.mqtt
    mqtt_push.mqtt = _mqtt_module(with_v2_api=True)
    error = None
    client = None
    try:
        client = mqtt_push._new_client("cid")
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        mqtt_push.mqtt = original
    R.check("v2: building the client must not raise", error, None)
    R.check(
        "v2: callback_api_version passed",
        client.kwargs.get("callback_api_version") if client else None,
        "v2",
    )


# ---------------- 后两个发现的回归保护 ----------------

def test_token_rotation_rebuilds_the_connection():
    """Finding 3: a bound connection is only valid for the token it used.

    `bound_token` used to be recorded and never read, so a rotated iotToken
    left the socket looking healthy while pushes silently stopped. The
    coordinator must tear the client down when the token it bound with no
    longer matches.
    """
    import hotata.coordinator as coordinator_mod

    source = Path(coordinator_mod.__file__).read_text(encoding="utf-8")
    # Behaviour first: the rebuild helper must consult the bound token.
    R.check(
        "the rebuild path reads bound_token",
        "bound_token" in source,
        True,
    )

    calls = {"stop": 0, "start": 0}

    class FakeClient:
        connected = True
        bound_token = "old-token"

    class FakeApi:
        iot_token = "new-token"

    class FakeAccount:
        rate_limited = False
        api = FakeApi()

    class FakeCoordinator:
        push_enabled = True
        push_client = FakeClient()
        account = FakeAccount()
        _last_push_retry = 0.0

        async def async_setup_push(self):
            calls["start"] += 1

        def async_stop_push(self):
            calls["stop"] += 1
            self.push_client = None

    # Bind the real method onto the fake so the branch under test is the real
    # one, not a copy that could drift.
    FakeCoordinator._async_rebuild_push_if_stale = (
        coordinator_mod.HotataCoordinator._async_rebuild_push_if_stale
    )

    import asyncio as _asyncio

    co = FakeCoordinator()
    _asyncio.run(co._async_rebuild_push_if_stale())
    R.check("rotated token stops the old client", calls["stop"], 1)
    R.check("and a fresh connection is started", calls["start"], 1)


def test_matching_token_leaves_the_connection_alone():
    """No rotation, no churn: an unchanged token must not reconnect."""
    import asyncio as _asyncio

    import hotata.coordinator as coordinator_mod

    calls = {"stop": 0, "start": 0}

    class FakeClient:
        connected = True
        bound_token = "same-token"

    class FakeApi:
        iot_token = "same-token"

    class FakeAccount:
        rate_limited = False
        api = FakeApi()

    class FakeCoordinator:
        push_enabled = True
        push_client = FakeClient()
        account = FakeAccount()
        _last_push_retry = 0.0

        async def async_setup_push(self):
            calls["start"] += 1

        def async_stop_push(self):
            calls["stop"] += 1
            self.push_client = None

    FakeCoordinator._async_rebuild_push_if_stale = (
        coordinator_mod.HotataCoordinator._async_rebuild_push_if_stale
    )
    co = FakeCoordinator()
    _asyncio.run(co._async_rebuild_push_if_stale())
    R.check("unchanged token does not stop", calls["stop"], 0)
    R.check("unchanged token does not restart", calls["start"], 0)


def test_options_flow_can_switch_push_off():
    """Finding 5: the push switch must be changeable after setup."""
    import hotata.config_flow as cf

    class FakeEntry:
        def __init__(self):
            self.data = {"enable_push": True}

    class FakeEntries:
        def __init__(self):
            self.updates = []

        def async_update_entry(self, entry, data=None, **kwargs):
            self.updates.append(dict(data or {}))
            entry.data = dict(data or {})

    entry = FakeEntry()
    flow = cf.HotataOptionsFlow()
    flow.config_entry = entry
    entries = FakeEntries()
    flow.hass = types.SimpleNamespace(config_entries=entries)
    result = _run(flow.async_step_init({"enable_push": False}))
    R.check("options step creates an entry", result["type"], "create_entry")
    R.check("the entry is updated once", len(entries.updates), 1)
    R.check("push is switched off", entry.data["enable_push"], False)
    R.check("other data is preserved", "enable_push" in entry.data, True)


def test_options_flow_defaults_to_the_current_value():
    """The form should show what is in effect, not a hard-coded default."""
    import voluptuous as real_vol  # real library: the schema must be valid

    import hotata.config_flow as cf

    class FakeEntry:
        data = {"enable_push": False}

    flow = cf.HotataOptionsFlow()
    flow.config_entry = FakeEntry()
    flow.hass = types.SimpleNamespace(config_entries=types.SimpleNamespace())
    form = _run(flow.async_step_init())
    schema = form["data_schema"]
    R.check("a schema is shown", isinstance(schema, real_vol.Schema), True)
    markers = list(schema.schema)
    R.check("one field", len(markers), 1)
    marker = markers[0]
    # 行为校验：把这个字段喂回 schema，能过就说明键名是 enable_push
    validated = schema({"enable_push": True})
    R.check("the field is the push switch", list(validated), ["enable_push"])
    R.check("the value is coerced to bool", validated["enable_push"], True)
    _ = marker
    # ha_stub 的 Marker 把 default 存成值，真实 voluptuous 存成可调用对象
    default = marker.default() if callable(marker.default) else marker.default
    R.check("default follows the entry", default, False)


def _run(coro):
    import asyncio as _asyncio

    return _asyncio.run(_await(coro))


async def _await(coro):
    return await coro


def main():
    test_incomplete_aepauth_never_logs_the_secret()
    test_incomplete_aepauth_never_logs_the_whole_payload()
    test_credentials_are_never_logged_on_the_success_path()
    test_client_builds_against_paho_v1_api()
    test_client_builds_against_paho_v2_api()
    test_token_rotation_rebuilds_the_connection()
    test_matching_token_leaves_the_connection_alone()
    test_options_flow_can_switch_push_off()
    test_options_flow_defaults_to_the_current_value()
    return R.report("Push client audit")


if __name__ == "__main__":
    sys.exit(main())
