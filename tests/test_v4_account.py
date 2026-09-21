"""v4 regression: account failover state machine against a stubbed cloud.

Run from anywhere:  python3 tests/test_v4_account.py
"""
import asyncio
import json
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ha_stub  # noqa: E402
from ha_stub import NOTIFS, TIMERS  # noqa: E402

PKG = Path(__file__).parent.parent / "custom_components"
sys.path.insert(0, str(PKG))
pkg = types.ModuleType("hotata")
pkg.__path__ = [str(PKG / "hotata")]
sys.modules["hotata"] = pkg

assert not (PKG / "hotata" / "util.py").exists(), "util.py should be deleted"

hub_mod = __import__("hotata.hub", fromlist=["HotataAccount"])
HotataAccount = hub_mod.HotataAccount


class FakeEntry:
    entry_id = "v4test"
    version = 4

    def __init__(self, data):
        self.data = dict(data)

    def add_update_listener(self, cb):
        return lambda: None

    def async_on_unload(self, cb):
        pass


class FakeHass:
    def __init__(self):
        self.tasks = []
        self.services = types.SimpleNamespace(
            has_service=lambda *a: True, async_remove=lambda *a: None
        )
        config_entries = types.SimpleNamespace()

        def async_update_entry(entry, data=None, version=None):
            if data is not None:
                entry.data = dict(data)
            if version is not None:
                entry.version = version

        config_entries.async_update_entry = async_update_entry
        self.config_entries = config_entries

    def async_create_task(self, coro):
        self.tasks.append(asyncio.ensure_future(coro))
        return self.tasks[-1]


async def drain(hass):
    for _ in range(10):
        if not hass.tasks:
            break
        tasks, hass.tasks = hass.tasks, []
        await asyncio.gather(*tasks, return_exceptions=True)


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._p = payload

    async def json(self, content_type=None):
        return self._p

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class FakeSession:
    def __init__(self, script):
        self.script = script
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(url)
        status, payload = self.script(url, kwargs)
        return FakeResp(status, payload)


STATE = {"backup_login": "ok", "primary_list": "ok", "last_login": "primary"}


def script(url, kwargs):
    blob = str(kwargs)
    if "login/password" in url:
        if "13800138003" in blob:
            STATE["last_login"] = "backup"
            if STATE["backup_login"] == "rate":
                return (200, {"code": "403", "message": "操作过于频繁"})
            if STATE["backup_login"] == "fail":
                return (200, {"code": "1032", "message": "该手机号尚未注册"})
            return (200, {"code": "000", "data": {"authCode": "ac-backup"}})
        if STATE["primary_list"] == "login_rate":
            return (200, {"code": "403", "message": "操作过于频繁"})
        STATE["last_login"] = "primary"
        return (200, {"code": "000", "data": {"authCode": "ac-primary"}})
    if "loginbyoauth" in url:
        return (200, {"content": {"sessionId": "sid-123"}})
    if "createSessionByAuthCode" in url:
        which = STATE["last_login"]
        return (
            200,
            {
                "code": 200,
                "data": {
                    "iotToken": "iot-" + which,
                    "refreshToken": "rt-" + which,
                    "identityId": "id1",
                },
            },
        )
    if "listBindingByAccount" in url:
        if STATE["primary_list"] == "rate":
            return (403, {"code": "403", "message": "操作过于频繁"})
        if STATE["primary_list"] == "fail":
            return (200, {"code": 500, "message": "server boom"})
        return (200, {"code": 200, "data": []})
    return (200, {"code": 200, "data": {}})


async def main():
    ha_stub._HOLDING["session"] = FakeSession(script)

    entry = FakeEntry(
        {
            "username": "13800138000",
            "password": "pw",
            "backup_username": "13800138003",
            "backup_password": "pw2",
        }
    )
    hass = FakeHass()
    acct = HotataAccount(hass, entry)

    print("== Scenario 1: setup (fresh login) ==", flush=True)
    await acct.async_setup()
    assert acct._primary_api.iot_token == "iot-primary", acct._primary_api.iot_token
    assert not acct.using_backup
    print("  OK - primary logged in via authCode chain", flush=True)

    print("== Scenario 2: primary 403 -> switch to backup ==", flush=True)
    acct.report_rate_limited("dev1")
    await drain(hass)
    assert acct.using_backup, "should be on backup"
    assert acct.api.iot_token == "iot-backup"
    assert "rate_limited" not in acct.active_issues
    assert any(not t["cancelled"] for t in TIMERS), "switch-back timer armed"
    await drain(hass)  # flush fire-and-forget state saves
    store = acct._account_store._disks[acct._account_store.key]
    assert store["using_backup"] is True
    assert store["backup"]["iot_token"] == "iot-backup"
    print("  OK - on backup, timer armed, store persisted", flush=True)

    print("== Scenario 3: backup also 403 (24h, no 5-min loop) ==", flush=True)
    acct._backup_penalty_until = time.time() - 1  # settle window over
    acct.report_rate_limited("dev1")
    await drain(hass)
    remaining = acct._backup_penalty_until - time.time()
    assert remaining > 80000, f"expected ~24h, got {remaining}"
    assert acct.using_backup
    print(f"  OK - backup penalized {remaining/3600:.1f}h", flush=True)

    print("== Scenario 4: switch-back probe OK ==", flush=True)
    acct._primary_penalty_until = time.time() - 1
    acct._switch_back_failures = 0
    ok = await acct._switch_back_to_primary()
    await drain(hass)
    assert ok and not acct.using_backup
    assert acct.api.iot_token == "iot-primary"
    assert entry.data.get("iot_token") == "iot-primary"
    assert acct._unsub_switch_back is None
    assert any("primary_restored" in k for k in NOTIFS)
    print("  OK - back on primary, tokens persisted, timer cleared", flush=True)

    print("== Scenario 5: probe says still limited -> stays on backup ==", flush=True)
    acct._backup_penalty_until = 0.0  # test-time: scenario-3 penalty long past
    acct.report_rate_limited("dev1")  # -> backup again
    await drain(hass)
    assert acct.using_backup
    acct._backup_penalty_until = time.time() - 1
    acct._primary_penalty_until = time.time() - 1
    STATE["primary_list"] = "rate"
    ok = await acct._switch_back_to_primary()
    await drain(hass)
    assert not ok and acct.using_backup
    assert acct.api.iot_token == "iot-backup", "backup token intact"
    assert time.time() < acct._primary_penalty_until, "primary penalty extended"
    assert acct._switch_back_failures == 1
    STATE["primary_list"] = "ok"
    print("  OK - stayed on backup, penalty extended", flush=True)

    print("== Scenario 6: backup login itself rate-limited ==", flush=True)
    acct._backup_penalty_until = 0.0
    STATE["backup_login"] = "rate"
    acct._primary_penalty_until = time.time() - 1
    ok = await acct._switch_back_to_primary()
    await drain(hass)
    assert ok and not acct.using_backup
    acct.report_rate_limited("dev1")  # -> tries backup, which 403s
    await drain(hass)
    assert not acct.using_backup, "must stay on primary when backup login 403s"
    assert time.time() < acct._backup_penalty_until, "backup penalized"
    assert time.time() < acct._primary_penalty_until, "primary still penalized"
    STATE["backup_login"] = "ok"
    print("  OK - double-403: both penalized, silence held", flush=True)

    print("== Scenario 7: restart resume (primary penalty, no backup) ==", flush=True)
    await drain(hass)  # flush pending fire-and-forget saves from scenario 6
    entry2 = FakeEntry(dict(entry.data))
    hass2 = FakeHass()
    acct2 = HotataAccount(hass2, entry2)
    await acct2.async_setup()
    # Scenario 6 ended with BOTH accounts penalized and primary active —
    # the restart must not lose the primary penalty (it would re-hit the 403).
    assert acct2._primary_penalty_until > time.time(), "primary penalty survived restart"
    assert not acct2.using_backup
    print("  OK - primary penalty survived restart", flush=True)

    print("== Scenario 8: straggler success does not clear penalty ==", flush=True)
    acct2.note_cloud_success()
    assert acct2.rate_limited, "penalty survives straggler success"
    print("  OK - penalty intact", flush=True)

    print("\nALL V4 ACCOUNT SCENARIOS PASSED", flush=True)


asyncio.run(main())
