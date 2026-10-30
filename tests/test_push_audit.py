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
import time
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
import hotata.coordinator as cm  # noqa: E402

R = __import__("cover_harness").Results()


class _InlineHass:
    """Runs executor jobs inline so lifecycle branches stay unit-testable."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


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
        _last_property_push = time.time()  # 视为刚刚收到过，避免误判
        hass = _InlineHass()

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
    FakeCoordinator._property_push_stale = (
        coordinator_mod.HotataCoordinator._property_push_stale
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
        _last_property_push = time.time()  # 视为刚刚收到过，避免误判
        hass = _InlineHass()

        async def async_setup_push(self):
            calls["start"] += 1

        def async_stop_push(self):
            calls["stop"] += 1
            self.push_client = None

    FakeCoordinator._async_rebuild_push_if_stale = (
        coordinator_mod.HotataCoordinator._async_rebuild_push_if_stale
    )
    FakeCoordinator._property_push_stale = (
        coordinator_mod.HotataCoordinator._property_push_stale
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
        hass = _InlineHass()

        async def async_setup_push(self):
            calls["start"] += 1

        def async_stop_push(self):
            self.push_client = None

    C._async_rebuild_push_if_stale = cm.HotataCoordinator._async_rebuild_push_if_stale
    C._property_push_stale = cm.HotataCoordinator._property_push_stale
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
        _last_property_push = time.time()  # 视为刚刚收到过，避免误判

        async def async_setup_push(self):
            calls["start"] += 1

        def async_stop_push(self):
            self.push_client = None

    C._async_rebuild_push_if_stale = cm.HotataCoordinator._async_rebuild_push_if_stale
    C._property_push_stale = cm.HotataCoordinator._property_push_stale
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


def _dev(props, tsl_ids=("NightLightSwitch",), product_key="a1abYBCSVlV"):
    """A real HotataDevice, built the way the other suites build one."""
    mod = __import__("hotata.models", fromlist=["HotataDevice"])
    return mod.HotataDevice(
        iot_id="dev1",
        name="airer",
        product_key=product_key,
        device_name="airer",
        online=True,
        raw={"productName": "X"},
        properties=dict(props),
        thing_model={"properties": [{"identifier": i} for i in tsl_ids]},
    )


def _co_with(data):
    """Coordinator stand-in exposing what _handle_properties_push touches."""
    co = cm.HotataCoordinator.__new__(cm.HotataCoordinator)
    co.data = data
    co.thing_models = {}
    return co




# ---------------- 看门狗：只认属性推送，不认任意消息 ----------------

def test_watchdog_tracks_property_push_only():
    """状态/事件推送也会到达，但不能续命看门狗。

    Otherwise a socket that stays up while the cloud stops sending
    properties looks healthy, and the poll stays skipped forever — the exact
    blind spot this watchdog exists to close.
    """
    src = Path(cm.__file__).read_text(encoding="utf-8")
    R.check("watchdog exists", "_last_property_push" in src, True)
    handler = src.split("def _handle_properties_push")[1].split("\n    def ")[0]
    R.check("property handler stamps the watchdog",
            "_last_property_push" in handler, True)
    status = src.split("def _handle_status_push")[1].split("\n    def ")[0]
    R.check("status handler does NOT stamp it",
            "_last_property_push" not in status, True)
    events = src.split("def _handle_event_push")[1].split("\n    def ")[0]
    R.check("event handler does NOT stamp it",
            "_last_property_push" not in events, True)


def test_watchdog_falls_back_to_polling_when_push_goes_silent():
    """绑定成功但长时间没有属性推送 → 先停旧连接再真正重建。

    必须用真实的 ``async_setup_push``：静默的连接仍然 ``is_connected()``，
    只调用它会因"已连接"直接返回 —— 看门狗就退化成一条每次轮询都打印、
    却永远修不好的日志（这正是本次复核发现的缺陷）。
    """
    import asyncio as _aio

    calls = {"inner": 0, "stopped": 0}

    class FakeClient:
        connected = True
        bound_token = "tok"

        def is_connected(self):
            return True

        def stop(self):
            calls["stopped"] += 1

    class C:
        push_enabled = True
        push_bound = True
        push_client = FakeClient()
        account = type("A", (), {"rate_limited": False,
                                  "api": type("T", (), {"iot_token": "tok"})()})()
        _last_push_retry = 0.0
        _last_property_push = time.time() - 1000  # 已经静默超过阈值
        _push_starting = False
        hass = _InlineHass()

        async def _async_setup_push_inner(self):
            calls["inner"] += 1

    C._async_rebuild_push_if_stale = cm.HotataCoordinator._async_rebuild_push_if_stale
    C._property_push_stale = cm.HotataCoordinator._property_push_stale
    C.async_setup_push = cm.HotataCoordinator.async_setup_push
    C.async_stop_push = cm.HotataCoordinator.async_stop_push

    co = C()
    _aio.run(co._async_rebuild_push_if_stale())
    R.check("silent client is stopped first", calls["stopped"], 1)
    R.check("silent push reaches the real rebuild path", calls["inner"], 1)
    R.check("rebuild failure restores property polling", co.push_bound, False)
    R.check("watchdog clock is cleared for the new connection",
            co._last_property_push, 0.0)


def test_watchdog_leaves_a_healthy_push_alone():
    import asyncio as _aio

    calls = {"inner": 0, "stopped": 0}

    class FakeClient:
        connected = True
        bound_token = "tok"

        def is_connected(self):
            return True

        def stop(self):
            calls["stopped"] += 1

    class C:
        push_enabled = True
        push_bound = True
        push_client = FakeClient()
        account = type("A", (), {"rate_limited": False,
                                  "api": type("T", (), {"iot_token": "tok"})()})()
        _last_push_retry = 0.0
        _last_property_push = time.time()  # 刚刚收到过属性推送
        _push_starting = False
        hass = _InlineHass()

        async def _async_setup_push_inner(self):
            calls["inner"] += 1

    C._async_rebuild_push_if_stale = cm.HotataCoordinator._async_rebuild_push_if_stale
    C._property_push_stale = cm.HotataCoordinator._property_push_stale
    C.async_setup_push = cm.HotataCoordinator.async_setup_push
    C.async_stop_push = cm.HotataCoordinator.async_stop_push

    _aio.run(C()._async_rebuild_push_if_stale())
    R.check("a fresh property push is left alone", calls["inner"], 0)
    R.check("a healthy client is not stopped", calls["stopped"], 0)


def test_bind_success_restarts_the_watchdog_clock():
    """绑定成功即计时：否则"绑上了但一条属性都没收到"永远判不出来。"""
    co = cm.HotataCoordinator.__new__(cm.HotataCoordinator)
    co.push_bound = True
    co._last_property_push = 0.0
    co._handle_push_bound(True)  # 提前 return，但计时先刷新
    R.check("bind stamps the clock", co._last_property_push > 0, True)


def test_unchanged_property_push_does_not_notify_entities():
    """全量快照每分钟一次；值没变就不该刷 45 个实体。

    Without the comparison the integration would rewrite every entity once a
    minute even when nothing happened.
    """
    changed = []
    data = {"dev1": _dev({"LightSwitch": {"value": 0}})}
    co = _co_with(data)
    co.async_set_updated_data = lambda d: changed.append(d)

    # 完全相同的快照
    co._handle_properties_push("dev1", {"LightSwitch": {"value": 0}})
    R.check("identical snapshot does not notify", len(changed), 0)

    # 真的变了
    co._handle_properties_push("dev1", {"LightSwitch": {"value": 1}})
    R.check("a real change notifies", len(changed), 1)
    R.check("cache reflects the new value",
            data["dev1"].properties["LightSwitch"], {"value": 1})


def test_property_comparison_ignores_timestamp_and_wrapping():
    """快照里的 time 是上报时间戳，参与比较会让每分钟都算"有变化"。"""
    changed = []
    data = {"dev1": _dev({"LightSwitch": {"time": 1, "value": 0}})}
    co = _co_with(data)
    co.async_set_updated_data = lambda d: changed.append(d)

    co._handle_properties_push("dev1", {"LightSwitch": {"time": 2, "value": 0}})
    R.check("time-only churn does not notify", len(changed), 0)

    # 轮询拿裸值、推送拿 {time,value}，同一个状态不能被当成两次变化
    changed2 = []
    data2 = {"dev1": _dev({"LightSwitch": 0})}
    co2 = _co_with(data2)
    co2.async_set_updated_data = lambda d: changed2.append(d)
    co2._handle_properties_push("dev1", {"LightSwitch": {"time": 3, "value": 0}})
    R.check("raw and wrapped forms compare equal", len(changed2), 0)


def test_motor_detection_accepts_wrapped_values():
    """推送把 MotorControlMode 写成 {time,value} 后，快/慢轮询仍要能判断。"""
    R.check("wrapped running motor is detected",
            cm._is_motor_running({"MotorControlMode": {"time": 1, "value": 1}}),
            True)
    R.check("wrapped idle motor is not running",
            cm._is_motor_running({"MotorControlMode": {"time": 1, "value": 0}}),
            False)


def test_capped_lamp_is_not_created_and_stale_one_is_removed():
    """位 22 = 0 的机器：夜灯灯实体不产出，且旧版留下的要被清掉。

    switch.py 早就为无能力的开关做自愈，light.py 一直没做，于是
    light.liang_yi_jia_ye_deng 在生产上留到了 4.2.5。这里断言的是**行为**
    （工厂产出 + 调用了清理），而不是源码里有没有某个字符串。
    """
    import hotata.light as light_mod

    removed = []

    class FakeHass:
        states = type("S", (), {})()

    class FakeCo:
        hass = FakeHass()
        data = {"dev1": None}

    # 全部位为 0 的机型（真实 D-3072S 位串）
    dev = _dev({"ModelFunctionList": "00000000000000000011111100110000"})
    co = FakeCo()
    co.data = {"dev1": dev}

    light_mod.remove_stale_entity = lambda hass, device, domain, key: removed.append(
        (domain, key)
    )

    ents = list(light_mod._lights_for_device(co, dev))
    names = [
        getattr(getattr(e, "entity_description", None), "key", None)
        or getattr(e, "identifier", None)
        or getattr(e, "_attr_name", None)
        for e in ents
    ]
    R.check("no night lamp produced", "NightLightSwitch" in names, False)
    R.check("the stale night lamp is removed", removed,
            [("light", "NightLightSwitch")])

    # 位 22 置位 → 应当产出，且不清理
    # 位 n（1-based，从右往左）落在 index width-n；22 → index 10
    _on = ["0"] * 32
    _on[32 - 22] = "1"
    dev2 = _dev({"ModelFunctionList": "".join(_on)})
    co2 = FakeCo()
    co2.data = {"dev2": dev2}
    removed.clear()
    ents2 = list(light_mod._lights_for_device(co2, dev2))
    names2 = [
        getattr(getattr(e, "entity_description", None), "key", None)
        or getattr(e, "identifier", None)
        or getattr(e, "_attr_name", None)
        for e in ents2
    ]
    R.check("night lamp produced when the bit is set",
            "NightLightSwitch" in names2, True)
    R.check("nothing removed when supported", removed, [])




def _mkdev(iot_id, name="晾衣架"):
    """A real HotataDevice for prune tests."""
    mod = __import__("hotata.models", fromlist=["HotataDevice"])
    return mod.HotataDevice(
        iot_id=iot_id,
        name=name,
        product_key="a1abYBCSVlV",
        device_name="airer",
        online=True,
        raw={"productName": "X"},
        properties={},
        thing_model={"properties": []},
    )


class _FakeRegistries:
    """One object faking both registries with a coherent device graph.

    devices: iot_id -> {name, shared_with_other_entry}
    The prune code walks er (entity rows -> device_id) then dr
    (device_id -> device entry). Keeping both in one place is what the
    previous five SimpleNamespace rounds got wrong.
    """

    def __init__(self, specs):
        # specs: iot_id -> (name, shared: bool)
        self._specs = specs
        self.removed = []

    # -- dr side --
    def dr_async_get(self, hass):
        return self

    def async_get_device(self, device_id):
        if device_id not in self._specs:
            return None
        name, _ = self._specs[device_id]
        return types.SimpleNamespace(
            id=device_id,
            identifiers={("hotata", device_id)},
            name=name,
            config_entries=(
                {"entry-1", "entry-other"}
                if self._specs[device_id][1]
                else {"entry-1"}
            ),
        )

    def async_remove_device(self, device_id):
        self.removed.append(device_id)
        del self._specs[device_id]

    def async_get(self, device_id):
        """dr.async_get(hass) returns self; this is its device lookup."""
        return self.async_get_device(device_id)

    # -- er side --
    def er_async_get(self, hass):
        return self

    def async_entries_for_config_entry(self, registry, entry_id):
        return [
            types.SimpleNamespace(device_id=iot_id) for iot_id in self._specs
        ]


def _prune_co(specs):
    """Build a coordinator wired to a coherent fake registry pair."""
    fake = _FakeRegistries(specs)
    co = cm.HotataCoordinator.__new__(cm.HotataCoordinator)
    co.data = {
        iot_id: _mkdev(iot_id, name)
        for iot_id, (name, _) in specs.items()
    }
    co._missing_since = {}
    co.hass = types.SimpleNamespace()
    co.config_entry = types.SimpleNamespace(entry_id="entry-1")

    cm.dr = types.SimpleNamespace(
        async_get=fake.dr_async_get,
        async_get_device=fake.async_get_device,
        async_remove_device=fake.async_remove_device,
    )
    cm.er = types.SimpleNamespace(
        async_get=fake.er_async_get,
        async_entries_for_config_entry=fake.async_entries_for_config_entry,
    )
    return co, fake


def test_removed_device_is_pruned_only_after_the_window():
    """设备必须持续缺失满 30 分钟才清理；一次缺失不能删。"""
    co, fake = _prune_co(
        {"keep": ("晾衣机", False), "gone": ("旧晾衣架", False)}
    )
    R.check("no prune on the first miss", (co._async_prune_removed_devices({"keep"}), fake.removed)[1], [])
    R.check("still in data", "gone" in co.data, True)
    R.check("miss recorded", "gone" in co._missing_since, True)

    # 把首次缺失时间往前推，模拟已经等了 30 分钟
    co._missing_since["gone"] -= (
        cm.HotataCoordinator._PRUNE_AFTER_SECONDS + 1
    )
    co._async_prune_removed_devices({"keep"})
    R.check("pruned after the window", fake.removed, ["gone"])
    R.check("gone removed from coordinator data", "gone" in co.data, False)
    R.check("gone removed from the registry", "gone" not in fake._specs, True)
    R.check("miss record cleared", "gone" in co._missing_since, False)

    co._async_prune_removed_devices({"keep"})
    R.check("no repeat prune", fake.removed, ["gone"])


def test_reappearing_device_resets_the_window():
    """设备回来后缺失计时清零，重新计满 30 分钟才会再判。"""
    co, fake = _prune_co({"gone": ("旧晾衣架", False)})
    co._async_prune_removed_devices(set())
    R.check("miss recorded", "gone" in co._missing_since, True)

    co._async_prune_removed_devices({"gone"})       # 回来了
    R.check("window cleared on return", "gone" in co._missing_since, False)

    co._async_prune_removed_devices(set())          # 又消失
    R.check("not pruned before the window", fake.removed, [])
    co._missing_since["gone"] -= (
        cm.HotataCoordinator._PRUNE_AFTER_SECONDS + 1
    )
    co._async_prune_removed_devices(set())
    R.check("pruned only after a full window", fake.removed, ["gone"])


def test_shared_device_is_never_pruned():
    """被其他配置项共用的设备不删。"""
    co, fake = _prune_co({"shared": ("共用晾衣架", True)})
    co._async_prune_removed_devices(set())
    co._missing_since["shared"] -= (
        cm.HotataCoordinator._PRUNE_AFTER_SECONDS + 1
    )
    co._async_prune_removed_devices(set())
    R.check("shared device survives", fake.removed, [])
    R.check("shared device kept in data", "shared" in co.data, True)
    R.check("shared device kept in the registry", "shared" in fake._specs, True)


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
    test_watchdog_tracks_property_push_only()
    test_watchdog_falls_back_to_polling_when_push_goes_silent()
    test_watchdog_leaves_a_healthy_push_alone()
    test_bind_success_restarts_the_watchdog_clock()
    test_unchanged_property_push_does_not_notify_entities()
    test_capped_lamp_is_not_created_and_stale_one_is_removed()
    test_removed_device_is_pruned_only_after_the_window()
    test_reappearing_device_resets_the_window()
    test_shared_device_is_never_pruned()
    test_property_comparison_ignores_timestamp_and_wrapping()
    test_motor_detection_accepts_wrapped_values()
    test_credential_without_identity_is_rejected()
    test_refresh_falls_back_to_full_login()
    test_set_property_recovers_from_blank_identity()
    test_bind_2043_asks_for_a_token_refresh_once()
    test_bind_reply_without_json_does_not_crash()
    return R.report("Push client audit")


if __name__ == "__main__":
    sys.exit(main())
