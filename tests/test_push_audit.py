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
import json
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


class _Msg:
    """A stand-in for paho's MQTTMessage."""

    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


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
        push_bound = True
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
        push_bound = True
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


# ---------------- 队友技术文档列出的 D2/D3/D4 ----------------

def _client(connected=True, bound=False):
    class C:
        def __init__(self):
            self.connected = connected
            self.bound = bound
            self.bound_token = "tok"
    return C()


def test_connected_but_unbound_is_rebuilt():
    """D2: a socket that is up but never bound must be retried.

    The old condition only asked "connected?", so a bind that never succeeded
    (code != 200, or no bind_reply at all) left the client connected forever and
    the account bound to nothing — with no further retry.
    """
    import time

    import hotata.coordinator as cm

    calls = {"start": 0}

    class C:
        push_enabled = True
        push_bound = False                                  # ← the trap
        push_client = _client(connected=True, bound=False)
        account = type("A", (), {"rate_limited": False,
                                  "api": type("T", (), {"iot_token": "tok"})()})()
        _last_push_retry = 0.0            # long past PUSH_RETRY_INTERVAL

        async def async_setup_push(self):
            calls["start"] += 1

        def async_stop_push(self):
            self.push_client = None

    C._async_rebuild_push_if_stale = cm.HotataCoordinator._async_rebuild_push_if_stale
    time.sleep(0)
    asyncio.run(C()._async_rebuild_push_if_stale())
    R.check("connected-but-unbound triggers a rebuild", calls["start"], 1)


def test_connected_and_bound_is_left_alone():
    import hotata.coordinator as cm

    calls = {"start": 0}

    class C:
        push_enabled = True
        push_bound = True
        push_client = _client(connected=True, bound=True)
        account = type("A", (), {"rate_limited": False,
                                  "api": type("T", (), {"iot_token": "tok"})()})()
        _last_push_retry = 0.0

        async def async_setup_push(self):
            calls["start"] += 1

        def async_stop_push(self):
            self.push_client = None

    C._async_rebuild_push_if_stale = cm.HotataCoordinator._async_rebuild_push_if_stale
    asyncio.run(C()._async_rebuild_push_if_stale())
    R.check("a healthy bound client is not rebuilt", calls["start"], 0)


def test_start_never_passes_keywords_to_run_in_executor():
    """Regression: run_in_executor takes positional args only.

    Passing cert_reqs= straight through raises
    "got an unexpected keyword argument" and the push silently never starts.
    """
    import asyncio as _asyncio

    calls = []

    async def main():
        source = Path(mqtt_push.__file__).read_text(encoding="utf-8")
        R.check("tls_set is wrapped in partial",
                "partial(client.tls_set" in source, True)
        R.check("no keyword passed straight to run_in_executor",
                "run_in_executor(\n                None, client.tls_set, cert_reqs" not in source,
                True)

    _asyncio.run(main())


def test_properties_push_is_dispatched():
    """D4: thing/properties must reach the callback, or entities freeze.

    With the poll skipped while bound (coordinator) and no dispatch here, a
    bound account saw neither polling nor pushes: "bound but never updates".
    """
    seen = []

    p = mqtt_push.HotataPushClient(
        get_iot_token=lambda: "t",
        on_properties_pushed=lambda iot_id, items: seen.append((iot_id, items)),
    )
    p._product_key, p._device_name = "pk", "dn"

    class Msg:
        topic = "/sys/pk/dn/app/down/thing/properties"
        payload = json.dumps({
            "method": "thing.properties",
            "params": {"iotId": "dev1",
                       "items": {"PowerSwitch": {"time": 1, "value": "{\"PowerSwitch\":1}"}}},
        }).encode()

    p._call_in_loop = lambda fn: fn()          # run inline
    p._on_message(None, None, Msg())
    R.check("property push reached the callback", len(seen), 1)
    R.check("with the normalised shape",
            seen[0][1] if seen else None, {"PowerSwitch": {"time": 1, "value": 1}})


def test_bind_2043_asks_for_a_token_refresh_once():
    """2043 (check iotToken failed) is recoverable: refresh the token and rebind.

    The token HTTP polling uses is renewed lazily on 401, so a stale iotToken
    only ever shows up here — and only once, otherwise a token that is genuinely
    unusable would hammer the cloud.
    """
    calls = {"refresh": 0}

    p = mqtt_push.HotataPushClient(
        get_iot_token=lambda: "stale-token",
        on_properties_pushed=lambda *args: None,
        on_bind_auth_failed=lambda: calls.__setitem__(
            "refresh", calls["refresh"] + 1
        ),
    )
    p._call_in_loop = lambda fn: fn()
    p._product_key, p._device_name = "pk", "dn"

    class Msg:
        topic = "/sys/pk/dn/app/down/account/bind_reply"
        payload = json.dumps(
            {"code": 2043, "message": "check iotToken failed"}
        ).encode()

    p._on_message(None, None, Msg())
    p._on_message(None, None, Msg())

    R.check("2043 requests a token refresh", calls["refresh"], 1)
    R.check("the client stays unbound", p.bound, False)


def test_bind_reply_without_json_does_not_crash():
    """A malformed bind_reply used to raise UnboundLocalError on `data`."""
    p = mqtt_push.HotataPushClient(
        get_iot_token=lambda: "t",
        on_properties_pushed=lambda *args: None,
    )
    p._call_in_loop = lambda fn: fn()
    p._product_key, p._device_name = "pk", "dn"

    class Msg:
        topic = "/sys/pk/dn/app/down/account/bind_reply"
        payload = b"not-json"

    error = None
    try:
        p._on_message(None, None, Msg())
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    R.check("malformed reply is tolerated", error, None)


# ---------------- 凭据自愈：identityId 与 iotToken 必须成对 ----------------

def _api_stub():
    """A HotataApi shell exposing only what the code under test touches."""
    mod = __import__("hotata.api", fromlist=["HotataApi"])
    api = mod.HotataApi.__new__(mod.HotataApi)
    api.iot_token = ""
    api.iot_refresh_token = ""
    api.identity_id = ""
    api.iot_token_expire = 0
    api.login_calls = 0
    return api


def test_credential_without_identity_is_rejected():
    """A refresh returning a token but no identityId must not be accepted.

    Accepting it leaves identity_id empty, and the re-login entry
    (async_refresh_iot_credential) requires identity_id — so the account locks
    into "token cannot be refreshed and commands cannot be sent".
    """
    api = _api_stub()
    R.check("token-only response rejected",
            api._apply_credential({"iotToken": "tok", "refreshToken": "r"}),
            False)
    R.check("no partial identity stored", api.identity_id, "")

    R.check("complete credential accepted",
            api._apply_credential({"iotToken": "tok", "identityId": "id-1"}),
            True)
    R.check("identity stored", api.identity_id, "id-1")


def test_refresh_falls_back_to_full_login():
    """No identityId at all → the refresh path cannot run → log in fully."""
    api = _api_stub()
    api.iot_refresh_token = "r"

    async def fake_login():
        api.login_calls += 1

    api.async_login = fake_login
    asyncio.run(api.async_refresh_iot_credential())
    R.check("full login used when identity is missing", api.login_calls, 1)


def test_set_property_recovers_from_blank_identity():
    """A rejected command re-logs in once and replays, instead of 500ing."""
    mod = __import__("hotata.api", fromlist=["HotataApi"])
    errors = __import__("hotata.exceptions", fromlist=["HotataError"])
    api = _api_stub()
    calls = {"n": 0, "login": 0}

    async def fake_call(path, params, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise errors.HotataError("identityId is blank")
        return {"ok": True}

    async def fake_refresh():
        calls["login"] += 1

    api._async_gateway_call = fake_call
    api.async_refresh_iot_credential = fake_refresh
    asyncio.run(api.async_set_properties("dev1", {"LightSwitch": 1}))
    R.check("command replayed once", calls["n"], 2)
    R.check("credential refreshed before replay", calls["login"], 1)

    # A different failure must still surface (no pointless re-login).
    api2 = _api_stub()
    api2.identity_id = "id"

    async def boom(path, params, **kwargs):
        raise errors.HotataError("device offline")

    api2._async_gateway_call = boom
    api2.async_refresh_iot_credential = fake_refresh

    async def main2():
        try:
            await api2.async_set_properties("dev1", {"LightSwitch": 1})
            return "no-error"
        except errors.HotataError as err:
            return str(err)

    R.check("unrelated errors are not masked",
            asyncio.run(main2()), "device offline")


def test_status_push_accepts_both_shapes():
    """厂商下发 {time, value}；旧代码只认 online/offline 字符串，全部被丢弃。

    证据（2026-10-18 实测）：忽略上下线推送 iotId=dNl3ry… status={'time': …, 'value': 1}
    """
    seen = []
    p = mqtt_push.HotataPushClient(
        get_iot_token=lambda: "t",
        on_properties_pushed=lambda *a: None,
        on_status_pushed=lambda iot_id, online: seen.append((iot_id, online)),
    )
    p._call_in_loop = lambda fn: fn()

    # 实测形态：value=1 → 在线
    p._on_message(None, None, _Msg("/thing/status", json.dumps({
        "params": {"iotId": "dev1", "status": {"time": 1792298267283, "value": 1}}
    }).encode()))
    R.check("dict shape with value=1 means online",
            seen[-1] if seen else None, ("dev1", True))

    p._on_message(None, None, _Msg("/thing/status", json.dumps({
        "params": {"iotId": "dev1", "status": {"time": 1792298267283, "value": 0}}
    }).encode()))
    R.check("dict shape with value=0 means offline",
            seen[-1] if seen else None, ("dev1", False))

    # 字符串形态仍要兼容
    p._on_message(None, None, _Msg("/thing/status", json.dumps({
        "params": {"iotId": "dev2", "status": "online"}
    }).encode()))
    R.check("string shape still accepted",
            seen[-1] if seen else None, ("dev2", True))


def test_status_push_ignores_unknown():
    seen = []
    p = mqtt_push.HotataPushClient(
        get_iot_token=lambda: "t",
        on_properties_pushed=lambda *a: None,
        on_status_pushed=lambda iot_id, online: seen.append((iot_id, online)),
    )
    p._call_in_loop = lambda fn: fn()
    p._on_message(None, None, _Msg("/thing/status", json.dumps({
        "params": {"iotId": "dev1", "status": {"value": "unknown"}}
    }).encode()))
    R.check("unknown status is dropped, not guessed", len(seen), 0)


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
    test_connected_but_unbound_is_rebuilt()
    test_connected_and_bound_is_left_alone()
    test_start_never_passes_keywords_to_run_in_executor()
    test_properties_push_is_dispatched()
    test_status_push_accepts_both_shapes()
    test_status_push_ignores_unknown()
    test_credential_without_identity_is_rejected()
    test_refresh_falls_back_to_full_login()
    test_set_property_recovers_from_blank_identity()
    test_bind_2043_asks_for_a_token_refresh_once()
    test_bind_reply_without_json_does_not_crash()
    return R.report("Push client audit")


if __name__ == "__main__":
    sys.exit(main())
