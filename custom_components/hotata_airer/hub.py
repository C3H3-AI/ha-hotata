"""Core account + device hub for Hotata Airer API communication.

Design (Xiaomi-style): one account-level entry owns the credentials
(refresh_token / access_token / user_id) and a single shared
:class:`HotataAccount` performs token refresh. Each physical airer is a device
record inside that account entry's data and gets its own :class:`HotataHub`
(device state + control). Device hubs proxy all token operations to the shared
account, so the token is refreshed exactly once per account regardless of how
many devices are connected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import httpx
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import httpx_client
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    API_INVOKE2,
    API_LOGIN_PASSWORD,
    API_ONLINE_STATUS,
    API_PROPERTY_GET,
    API_PROPERTY_SET,
    API_REFRESH_TOKEN,
    APP_KEY,
    APP_VERSION,
    CONF_ACCESS_TOKEN,
    CONF_DESCENT_TIME,
    CONF_IOT_ID,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    CONF_USERNAME,
    DEFAULT_DESCENT_TIME,
    DEFAULT_NAME,
    DOMAIN,
    IMEI,
    ONLINE_CHECK_INTERVAL,
    PHONE_MODEL,
    POLL_ACTIVE_WINDOW,
    POLL_INTERVAL_FAST,
    POLL_INTERVAL_SLOW,
    RATE_LIMIT_BACKOFF,
    SYS_VERSION,
)
from .util import build_login_body, encrypt_password, generate_sign

_LOGGER = logging.getLogger(__name__)

# Runtime issue keys surfaced to the user (persistent notification +
# error-state sensor). Cloud-recoverable ones are cleared on success.
ISSUE_RATE_LIMITED = "rate_limited"
ISSUE_CONNECTION = "connection_error"


def build_base_payload(
    user_id: str,
    iot_id: str | None = None,
) -> dict[str, Any]:
    """Build common payload fields."""
    ts = int(time.time() * 1000)
    payload: dict[str, Any] = {
        "userid": user_id,
        "userId": user_id,
        "appKey": APP_KEY,
        "appVersion": APP_VERSION,
        "timestamp": ts,
        "traceId": f"ha_{ts}",
        "sysVersion": SYS_VERSION,
        "phoneModel": PHONE_MODEL,
        "imei": IMEI,
    }
    if iot_id:
        payload["iotId"] = iot_id
    return payload


@dataclass
class HotataState:
    """Represents the current state of the airer."""

    online: bool = False
    power_on: bool | None = None
    light_on: bool | None = None
    light_brightness: int | None = None
    drying_on: bool | None = None
    air_drying_on: bool | None = None
    disinfection_on: bool | None = None
    ions_on: bool | None = None
    position: int | None = None
    simulated_position: int = 100
    light_remaining_time: int | None = None
    drying_remaining_time: int | None = None
    air_drying_remaining_time: int | None = None
    ions_remaining_time: int | None = None
    disinfection_remaining_time: int | None = None
    motor_control_mode: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Listener:
    """Callback listener for state updates."""

    async_callback: Callable[[], Any]


class HotataAccount:
    """Owns account credentials and performs token refresh.

    Exactly one instance exists per config entry (the account). All device hubs
    share this instance so the refresh token is only exchanged once.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the account from the main config entry data."""
        self.hass = hass
        self.entry = entry
        self.user_id: str = entry.data.get(CONF_USER_ID, "")
        self._access_token: str = entry.data.get(CONF_ACCESS_TOKEN, "")
        self._refresh_token: str = entry.data.get(CONF_REFRESH_TOKEN, "")
        self._username: str = entry.data.get(CONF_USERNAME, "")
        self._password: str = entry.data.get(CONF_PASSWORD, "")

        self._expire_at: float = 0

        self._token_expired: bool = False
        self._token_permanently_invalid: bool = False  # True when 1073 received
        # Machine-readable error key ("normal"/"auth_expired"/"too_many_requests"/
        # "server_error") matched by translations; raw text kept in detail.
        self._last_error: str = ""
        self._last_error_detail: str = ""
        self._refresh_in_progress: bool = False
        self._last_refresh_attempt: float = 0
        # Cloud health tracking: active user-visible issues (key -> detail) and
        # consecutive request failures for the connection-loss alert.
        self._active_issues: dict[str, str] = {}
        self._conn_failure_count: int = 0
        # Account-wide 403 silence deadline (epoch); all cloud requests stop.
        self._rate_limited_until: float = 0.0
        self._unsub_token_refresh: Callable[[], None] | None = None
        # iot_id -> HotataHub, populated by async_setup_entry
        self.device_hubs: dict[str, HotataHub] = {}
        # Guard so the token-expiry notification fires only once per expiry.
        self._token_expiry_notified: bool = False

        _LOGGER.debug(
            "Account init: user_id=%s, access_token=%s...",
            self.user_id,
            str(self._access_token)[:20],
        )

    # ---- public accessors (proxied by device hubs) ----

    @property
    def access_token(self) -> str:
        """Return current access token."""
        return self._access_token

    @property
    def token_expired(self) -> bool:
        """Return True if token is expired or permanently invalid."""
        return self._token_expired or self._token_permanently_invalid

    @property
    def token_permanently_invalid(self) -> bool:
        """Return True if token is permanently invalid (1073)."""
        return self._token_permanently_invalid

    @property
    def last_error(self) -> str:
        """Return the machine-readable error key (translated by HA)."""
        return self._last_error

    @property
    def last_error_detail(self) -> str:
        """Return the raw server error text for diagnostics."""
        return self._last_error_detail

    @property
    def active_issues(self) -> dict[str, str]:
        """Return the currently active user-visible issues (key -> detail)."""
        return dict(self._active_issues)

    @property
    def rate_limited(self) -> bool:
        """True while the account is in the 403 silence window."""
        return time.time() < self._rate_limited_until

    # ---- cloud health issues (403 rate limit / connection loss) ----

    def _issue_notification_id(self, key: str) -> str:
        return f"hotata_issue_{self.entry.entry_id}_{key}"

    def _async_report_issue(self, key: str, title: str, message: str) -> None:
        """Raise a user-visible issue once, with a persistent notification."""
        if self._active_issues.get(key) == message:
            return
        self._active_issues[key] = message
        self.hass.components.persistent_notification.async_create(
            message=message,
            title=title,
            notification_id=self._issue_notification_id(key),
        )
        self._async_push_issue_state()

    def _async_clear_issue(self, key: str) -> bool:
        """Clear an active issue; return True if it was active."""
        if key not in self._active_issues:
            return False
        del self._active_issues[key]
        self.hass.components.persistent_notification.async_dismiss(
            self._issue_notification_id(key)
        )
        return True

    def _async_push_issue_state(self) -> None:
        """Refresh entities so the error-state sensor reflects new issues."""
        for hub in self.device_hubs.values():
            self.hass.async_create_task(hub.notify_listeners())

    def report_rate_limited(self, detail: str = "") -> None:
        """User-visible warning while the server throttles us (403).

        Enters an account-wide 24h silence: polling, token refresh and control
        commands all stop, because any request during the penalty may extend it.
        """
        self._rate_limited_until = time.time() + RATE_LIMIT_BACKOFF
        self._async_report_issue(
            ISSUE_RATE_LIMITED,
            "好太太云端限频（403 操作过于频繁）",
            "好太太云端返回「操作过于频繁」，集成已停止全部云端请求 24 小时"
            "（轮询、控制命令、token 刷新均暂停），以避免处罚延长。\n\n"
            "期间晾衣机实体将显示为不可用，属正常现象；24 小时后自动恢复，"
            "恢复时会再发一条通知，无需任何操作。"
            + (f"\n\n设备：{detail}" if detail else ""),
        )

    def note_cloud_failure(self, err: Exception) -> None:
        """Count a failed cloud request; alert after repeated failures."""
        self._conn_failure_count += 1
        if self._conn_failure_count < 2 or ISSUE_CONNECTION in self._active_issues:
            return
        self._async_report_issue(
            ISSUE_CONNECTION,
            "好太太云端连接失败",
            f"连续 {self._conn_failure_count} 次云端请求失败"
            f"（{type(err).__name__}: {err or '连接/响应超时'}）。\n\n"
            "常见原因：本机断网、DNS/代理故障或好太太服务端不可用。"
            "集成会按退避节奏自动重试，恢复后会发送通知。",
        )

    def note_cloud_success(self) -> None:
        """A cloud request succeeded — reset failure count, clear issues."""
        self._conn_failure_count = 0
        self._rate_limited_until = 0.0
        cleared = [
            key
            for key in (ISSUE_RATE_LIMITED, ISSUE_CONNECTION)
            if self._async_clear_issue(key)
        ]
        if cleared:
            self.hass.components.persistent_notification.async_create(
                message="好太太云端连接已恢复正常，轮询已继续。",
                title="好太太云端已恢复",
                notification_id=self._issue_notification_id("recovery"),
            )
            self._async_push_issue_state()

    def register_device(self, hub: HotataHub) -> None:
        """Register a device hub so it can be notified on token changes."""
        self.device_hubs[hub.iot_id] = hub

    def _build_headers(self) -> dict[str, str]:
        """Build request headers with current token."""
        headers = {"content-type": "application/json"}
        if self._access_token:
            headers["authorization"] = self._access_token
        return headers

    # ---- token lifecycle ----

    async def ensure_token_valid(self) -> bool:
        """Check if token is valid, refresh if needed."""
        # Account-wide 403 silence: no requests at all until the deadline.
        if self._rate_limited_until > time.time():
            _LOGGER.debug(
                "Rate limited, skipping token check for another %.0fs",
                self._rate_limited_until - time.time(),
            )
            return False

        # Token permanently invalid (1073) — stop retrying
        if self._token_permanently_invalid:
            return False

        # Rate limit cooldown — don't retry within 60 seconds of a 403
        if self._token_expired:
            cooldown = 60
            elapsed = time.time() - self._last_refresh_attempt
            if elapsed < cooldown:
                _LOGGER.debug(
                    "Token refresh on cooldown (%.0fs left), skipping",
                    cooldown - elapsed,
                )
                return False
            _LOGGER.debug("Token marked as expired, forcing refresh")
            return await self.async_refresh_token()

        # Check if nearing expiry (within 2 minutes)
        if self._expire_at > 0 and time.time() < self._expire_at - 120:
            _LOGGER.debug(
                "Token still valid (expires in %ds), skipping refresh",
                int(self._expire_at - time.time()),
            )
            return True

        # Refresh needed
        return await self.async_refresh_token()

    async def async_refresh_token(self) -> bool:
        """Refresh the access token for the whole account."""
        # Account-wide 403 silence: skip refresh entirely (scheduled ticks too).
        if self._rate_limited_until > time.time():
            _LOGGER.debug("Rate limited, skipping token refresh")
            return False

        # Prevent concurrent refresh
        if self._refresh_in_progress:
            _LOGGER.debug("Token refresh already in progress, waiting")
            for _ in range(30):
                if not self._refresh_in_progress:
                    return not self._token_expired
                await asyncio.sleep(0.1)
            return False

        self._refresh_in_progress = True
        self._last_refresh_attempt = time.time()
        try:
            async with httpx_client.get_async_client(self.hass) as client:
                ts = int(time.time() * 1000)
                payload = {
                    "refreshToken": self._refresh_token,
                    "appKey": APP_KEY,
                    "appVersion": APP_VERSION,
                    "timestamp": ts,
                    "traceId": f"refresh_{ts}",
                    "sysVersion": SYS_VERSION,
                    "phoneModel": PHONE_MODEL,
                    "imei": IMEI,
                }
                payload["sign"] = generate_sign(payload)

                resp = await client.post(
                    API_REFRESH_TOKEN,
                    json=payload,
                    headers={"content-type": "application/json"},
                    timeout=10,
                )
                _LOGGER.debug(
                    "Refresh token response status: %s, text: %s",
                    resp.status_code,
                    resp.text[:200],
                )

                try:
                    data = resp.json()
                except Exception as json_err:
                    _LOGGER.error(
                        "Failed to parse token refresh JSON: %s. Response: %s",
                        json_err,
                        resp.text[:500],
                    )
                    self._token_expired = True
                    return False

                _LOGGER.debug("Refresh API response: code=%s", data.get("code"))

                if data.get("code") == "000":
                    d = data.get("data", {})
                    token_type = d.get("tokenType", "bearer").strip()
                    self._access_token = f"{token_type} {d['accessToken']}"
                    if d.get("refreshToken"):
                        self._refresh_token = d["refreshToken"]
                    expires_in = int(d.get("expiresIn", 2591999))
                    self._expire_at = time.time() + expires_in
                    # Persist new tokens into the (account) config entry
                    self._persist_tokens()
                    self._token_expired = False
                    self._token_permanently_invalid = False
                    self._token_expiry_notified = False
                    self._last_error = ""
                    self._last_error_detail = ""
                    self.note_cloud_success()
                    _LOGGER.info("Token refreshed successfully")
                    return True
                else:
                    code = data.get("code", "")
                    msg = data.get("message", "")
                    # 1073 = login expired, try re-login with username/password
                    if code == "1073":
                        _LOGGER.warning(
                            "Refresh token expired (code=1073), "
                            "attempting re-login with username/password"
                        )
                        if await self.async_login():
                            return True
                        self._token_permanently_invalid = True
                        self._last_error = "auth_expired"
                        self._last_error_detail = msg or "登录已过期，请重新登录"
                        await self._notify_token_expired()
                    elif code == "403":
                        self._last_error = "too_many_requests"
                        self._last_error_detail = (
                            "Too many requests, waiting for cooldown"
                        )
                        self.report_rate_limited()
                    else:
                        self._last_error = "server_error"
                        self._last_error_detail = (
                            f"Token refresh failed: code={code}, msg={msg}"
                        )
                    _LOGGER.warning("Token refresh failed: %s", data)
                    self._token_expired = True
                    return False
        except Exception as err:
            # str(err) is empty for bare timeouts, so always include the type.
            _LOGGER.warning(
                "Token refresh error: %s: %s", type(err).__name__, err,
                exc_info=True,
            )
            self.note_cloud_failure(err)
            self._token_expired = True
            return False
        finally:
            self._refresh_in_progress = False

    async def async_login(self) -> bool:
        """Login with username/password to get fresh tokens."""
        if not self._username or not self._password:
            _LOGGER.warning("No username/password stored, cannot re-login")
            return False
        try:
            async with httpx_client.get_async_client(self.hass) as client:
                body = build_login_body({
                    "username": self._username,
                    "registeredId": str(uuid.uuid4()),
                    "password": encrypt_password(self._password),
                })
                resp = await client.post(
                    API_LOGIN_PASSWORD,
                    json=body,
                    headers={"content-type": "application/json"},
                    timeout=15,
                )
                data = resp.json()
                if data.get("code") != "000":
                    _LOGGER.error("Login failed: code=%s, msg=%s", data.get("code"), data.get("message"))
                    self._last_error = "auth_expired"
                    self._last_error_detail = (
                        f"Login failed: code={data.get('code')}, "
                        f"msg={data.get('message')}"
                    )
                    return False
                d = data.get("data", {})
                token_type = d.get("tokenType", "bearer").strip()
                self._access_token = f"{token_type} {d['accessToken']}"
                if d.get("refreshToken"):
                    self._refresh_token = d["refreshToken"]
                self.user_id = d.get("userId", self.user_id)
                expires_in = int(d.get("expiresIn", 2591999))
                self._expire_at = time.time() + expires_in
                self._persist_tokens()
                self._token_expired = False
                self._token_permanently_invalid = False
                self._token_expiry_notified = False
                self._last_error = ""
                self._last_error_detail = ""
                _LOGGER.info("Login successful, tokens updated")
                return True
        except Exception as err:
            _LOGGER.warning(
                "Login error: %s: %s", type(err).__name__, err, exc_info=True
            )
            return False

    async def _check_new_devices(self) -> None:
        """Silently pull the account device list and auto-add any new airer.

        Runs on the periodic refresh tick. New devices are merged into
        ``entry.data["devices"]`` and the entry is reloaded so their entities
        appear without any user action (Xiaomi-style auto-discovery).
        """
        if self._token_permanently_invalid or self._token_expired:
            # No usable token — skip discovery until the user re-authenticates.
            return
        if not await self.ensure_token_valid():
            return
        try:
            from .config_flow import _device_metadata, _get_device_list, _merge_devices
            fetched = await _get_device_list(
                self.hass, self._access_token, self.user_id,
            )
        except Exception as err:
            _LOGGER.warning("Device list fetch failed during discovery: %s", err)
            return
        if not fetched:
            return
        # Normalize fetched records the same way the config flow does so new
        # devices carry descent_time / product / mac metadata consistently.
        # New devices fall back to the default descent time; the user can tune
        # each one via the number entity afterwards.
        standardized = [
            _device_metadata(d, DEFAULT_DESCENT_TIME)
            for d in fetched
            if (d.get("iotid") or d.get("iotId"))
        ]
        existing = self.entry.data.get("devices", [])
        merged = _merge_devices(existing, standardized)
        new_devices = [d for d in merged if d not in existing]
        if not new_devices:
            return
        # Persist merged list, notify, then reload so new hubs + entities appear.
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, "devices": merged}
        )
        _LOGGER.info(
            "Auto-discovered %d new airer(s), reloading entry", len(new_devices)
        )
        await self._notify_new_devices(new_devices)
        await self.hass.config_entries.async_reload(self.entry.entry_id)

    async def _notify_new_devices(
        self, new_devices: list[dict[str, Any]]
    ) -> None:
        """Notify the user that new airers were auto-added."""
        names = ", ".join(d.get(CONF_NAME, DEFAULT_NAME) for d in new_devices)
        entry_id = self.entry.entry_id
        try:
            self.hass.components.persistent_notification.async_create(
                message=(
                    f"Auto-discovered and added {len(new_devices)} new airer(s): {names}.\n\n"
                    "No reconfiguration required — reload the page to see the new devices."
                ),
                title="Hotata Airer – New devices added",
                notification_id=f"hotata_new_devices_{entry_id}",
            )
        except Exception as err:
            _LOGGER.warning("Failed to send new-device notification: %s", err)

    async def _notify_token_expired(self) -> None:
        """Notify the user once that the token has expired (re-auth needed)."""
        if self._token_expiry_notified:
            return
        self._token_expiry_notified = True
        entry_id = self.entry.entry_id
        try:
            self.hass.components.persistent_notification.async_create(
                message=(
                    "Hotata Airer login has expired and device updates have stopped.\n\n"
                    "Go to **Settings → Devices & Services → Hotata Airer → Configure → Reconfigure** "
                    "and re-enter your username and password."
                ),
                title="Hotata Airer – Authentication expired",
                notification_id=f"hotata_token_expired_{entry_id}",
            )
        except Exception as err:
            _LOGGER.warning("Failed to send token-expired notification: %s", err)

    def _persist_tokens(self) -> None:
        """Write refreshed tokens back into the account config entry."""
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                CONF_ACCESS_TOKEN: self._access_token,
                CONF_REFRESH_TOKEN: self._refresh_token,
            },
        )

    # ---- scheduled refresh ----

    async def start_scheduled_refresh(self) -> None:
        """Start the preventive 6-hourly token refresh."""
        _LOGGER.info("Starting scheduled token refresh (interval=6h)")
        self._unsub_token_refresh = async_track_time_interval(
            self.hass,
            self._token_refresh_callback,
            timedelta(hours=6),
        )

    def stop_scheduled_refresh(self) -> None:
        """Stop the preventive token refresh."""
        if self._unsub_token_refresh is not None:
            self._unsub_token_refresh()
            self._unsub_token_refresh = None

    async def _token_refresh_callback(self, now: Any) -> None:
        """Scheduled tick (every 6h): refresh token if near expiry, then check for new devices."""
        if self._expire_at > 0 and time.time() < self._expire_at - 120:
            _LOGGER.debug(
                "Token still valid (expires in %ds), skipping preventive refresh",
                int(self._expire_at - time.time()),
            )
        else:
            _LOGGER.info("Scheduled token refresh triggered")
            await self.async_refresh_token()
        # Opportunistically auto-discover newly added devices.
        await self._check_new_devices()


class HotataHub:
    """Per-device hub. Holds device state and proxies token ops to the account."""

    def __init__(self, hass: HomeAssistant, device_data: dict[str, Any], account: HotataAccount) -> None:
        """Initialize the device hub from a device metadata dict."""
        self.hass = hass
        self.account = account
        self._device_data = device_data
        self.name = device_data.get(CONF_NAME, DEFAULT_NAME)
        self.iot_id: str = device_data[CONF_IOT_ID]
        self._expire_at: float = 0

        self.state = HotataState()
        self._listeners: list[Listener] = []
        self._unsub_poll: Callable[[], None] | None = None
        self._last_error: str = ""
        self._refresh_in_progress: bool = False
        self._last_refresh_attempt: float = 0
        self._last_state_hash: str = ""
        # Dynamic polling: fast while active (motor moving / recent command).
        self._active_until: float = 0.0
        self._last_online_check: float = 0.0

        self._descent_time: int = int(
            device_data.get(CONF_DESCENT_TIME, DEFAULT_DESCENT_TIME)
        )
        self._store = Store(hass, 1, f"hotata_airer.{self.iot_id}.config")
        self._last_motor_mode: int | None = None

        _LOGGER.debug(
            "Device hub init: iot_id=%s, keys=%s",
            self.iot_id,
            list(device_data.keys()),
        )

    # ---- account-proxied properties (keep public interface stable) ----

    @property
    def access_token(self) -> str:
        """Return current access token (from the shared account)."""
        return self.account.access_token

    @property
    def token_expired(self) -> bool:
        """Return True if the account token is expired or permanently invalid."""
        return self.account.token_expired

    @property
    def token_permanently_invalid(self) -> bool:
        """Return True if the account token is permanently invalid (1073)."""
        return self.account.token_permanently_invalid

    @property
    def last_error(self) -> str:
        """Return the last error message (from the shared account)."""
        return self.account.last_error

    @property
    def last_error_detail(self) -> str:
        """Return the raw error detail (from the shared account)."""
        return self.account.last_error_detail

    @property
    def user_id(self) -> str:
        """Return the account user id."""
        return self.account.user_id

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info for HA device registry."""
        info: dict[str, Any] = {
            "identifiers": {(DOMAIN, self.iot_id)},
            "name": self.name,
            "manufacturer": "Hotata",
        }
        model_parts: list[str] = []
        if self._device_data.get("productname"):
            model_parts.append(str(self._device_data["productname"]))
        dtype = self._device_data.get("devicetype")
        if dtype:
            model_parts.append(f"({dtype})")
        info["model"] = " ".join(model_parts) if model_parts else "Smart Airer"
        mac_raw = self._device_data.get("mac")
        if mac_raw:
            mac = str(mac_raw).replace(":", "").lower()
            if len(mac) == 12 and all(c in "0123456789abcdef" for c in mac):
                info["connections"] = {
                    ("mac", ":".join(mac[i : i + 2] for i in range(0, 12, 2)))
                }
        if self._device_data.get("devicenickname"):
            info["model"] = str(self._device_data["devicenickname"])
        return info

    @property
    def descent_time(self) -> int:
        """Return the configured descent time."""
        return self._descent_time

    async def async_set_descent_time(self, value: int) -> None:
        """Update descent time and persist."""
        self._descent_time = value
        await self._store.async_save({"descent_time": value})

    async def async_load_persisted_config(self) -> None:
        """Load persisted config values from Store."""
        data = await self._store.async_load()
        if data and "descent_time" in data:
            self._descent_time = data["descent_time"]

    # ---- listeners ----

    def add_listener(self, async_callback: Callable[[], Any]) -> Callable[[], None]:
        """Register a listener for state updates."""
        listener = Listener(async_callback=async_callback)
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    async def notify_listeners(self) -> None:
        """Notify all registered listeners of a state change (public)."""
        await self._notify_listeners()

    async def _notify_listeners(self) -> None:
        """Notify all registered listeners of a state change."""
        for listener in self._listeners:
            try:
                await listener.async_callback()
            except Exception:
                _LOGGER.exception("Error notifying listener")

    # ---- token helpers (delegate to account) ----

    def _build_headers(self) -> dict[str, str]:
        """Build request headers with the current account token."""
        return self.account._build_headers()

    async def _ensure_token_valid(self) -> bool:
        """Ensure the shared account token is valid."""
        return await self.account.ensure_token_valid()

    async def async_refresh_token(self) -> bool:
        """Refresh the shared account token."""
        return await self.account.async_refresh_token()

    # ---- queries ----

    async def _query_properties(self) -> HotataState | None:
        """Query device properties from API."""
        if not await self._ensure_token_valid():
            return None

        payload = build_base_payload(self.user_id, self.iot_id)
        payload["sign"] = generate_sign(payload)

        async with httpx_client.get_async_client(self.hass) as client:
            try:
                resp = await client.post(
                    API_PROPERTY_GET,
                    json=payload,
                    headers=self._build_headers(),
                    timeout=10,
                )
                data = resp.json()

                # Handle auth failure — try refresh and retry once
                if data.get("code") == "401":
                    _LOGGER.warning("Got 401, attempting token refresh")
                    self.account._token_expired = True
                    if await self.async_refresh_token():
                        payload = build_base_payload(self.user_id, self.iot_id)
                        payload["sign"] = generate_sign(payload)
                        resp = await client.post(
                            API_PROPERTY_GET,
                            json=payload,
                            headers=self._build_headers(),
                            timeout=10,
                        )
                        data = resp.json()
                    else:
                        return None

                if data.get("code") == "000":
                    self.account._token_expired = False
                    self.account.note_cloud_success()
                    _LOGGER.debug("Property get raw data: %s", str(data)[:500])
                    self._parse_state(data)
                    return self.state
                if data.get("code") == "403":
                    self._report_rate_limited()
                    return None
                _LOGGER.warning("Query failed: %s", data)
                return None
            except httpx.HTTPStatusError as err:
                if err.response.status_code == 401:
                    _LOGGER.warning("Got HTTP 401, attempting token refresh")
                    self.account._token_expired = True
                    if await self.async_refresh_token():
                        payload = build_base_payload(self.user_id, self.iot_id)
                        payload["sign"] = generate_sign(payload)
                        resp = await client.post(
                            API_PROPERTY_GET,
                            json=payload,
                            headers=self._build_headers(),
                            timeout=10,
                        )
                        data = resp.json()
                        if data.get("code") == "000":
                            self.account._token_expired = False
                            self.account.note_cloud_success()
                            _LOGGER.debug(
                                "Property get raw data (retry): %s", str(data)[:500]
                            )
                            self._parse_state(data)
                            return self.state
                        if data.get("code") == "403":
                            self._report_rate_limited()
                        return None
                _LOGGER.error("HTTP error querying device: %s", err)
                self.account.note_cloud_failure(err)
                return None
            except Exception as err:
                _LOGGER.error("Query error (not auth-related): %s", err)
                self.account.note_cloud_failure(err)
                return None

    def _state_hash(self) -> str:
        """Compute hash of all state fields for change detection."""
        s = self.state
        key_fields = (
            f"{s.online}",
            f"{s.power_on}",
            f"{s.light_on}",
            f"{s.light_brightness}",
            f"{s.drying_on}",
            f"{s.air_drying_on}",
            f"{s.disinfection_on}",
            f"{s.ions_on}",
            f"{s.simulated_position}",
            f"{s.motor_control_mode}",
            f"{s.light_remaining_time}",
            f"{s.drying_remaining_time}",
            f"{s.air_drying_remaining_time}",
            f"{s.ions_remaining_time}",
            f"{s.disinfection_remaining_time}",
        )
        return "|".join(str(f) for f in key_fields)

    def _parse_state(self, raw: dict[str, Any]) -> None:
        """Parse API response into state object."""
        data_array = raw.get("data", [])
        state_map: dict[str, Any] = {}

        if isinstance(data_array, list):
            for item in data_array:
                if "attribute" in item:
                    state_map[item["attribute"]] = item["value"]

        self.state.raw = state_map

        def get_value(*keys: str) -> Any:
            for key in keys:
                v = state_map.get(key)
                if v is not None and v != "":
                    return v
            return None

        # Position
        pos = get_value("Position")
        if pos is not None:
            try:
                self.state.position = int(float(pos))
            except (ValueError, TypeError):
                pass

        # Motor Control Mode
        motor_mode = get_value("MotorControlMode")
        if motor_mode is not None:
            try:
                self.state.motor_control_mode = int(float(motor_mode))
            except (ValueError, TypeError):
                pass

        # Auto-sync simulated_position based on MotorControlMode transitions
        current_mode = self.state.motor_control_mode
        if current_mode is not None and self._last_motor_mode is not None:
            if self._last_motor_mode != 0 and current_mode == 0:
                if self._last_motor_mode == 1:
                    self.state.simulated_position = 100
                    _LOGGER.debug("Motor stopped after up, simulated_position → 100")
                elif self._last_motor_mode == 2:
                    self.state.simulated_position = 0
                    _LOGGER.debug("Motor stopped after down, simulated_position → 0")
        if current_mode is not None:
            self._last_motor_mode = current_mode

        # Switch states
        switch_mapping = {
            "PowerSwitch": "power_on",
            "LightSwitch": "light_on",
            "DryingSwitch": "drying_on",
            "AirDryingSwitch": "air_drying_on",
            "DisinfectionSwitch": "disinfection_on",
            "IonsSwitch": "ions_on",
        }
        for attr_name, state_attr in switch_mapping.items():
            val = get_value(attr_name)
            if val is not None:
                setattr(self.state, state_attr, val in (True, 1, "1", "true"))

        # Light brightness
        brightness = get_value("LightBrightness")
        if brightness is not None:
            try:
                self.state.light_brightness = int(float(brightness))
            except (ValueError, TypeError):
                pass

        # Remaining times
        time_mapping = {
            "LightRemainingTime": "light_remaining_time",
            "DryingRemainingTime": "drying_remaining_time",
            "AirDryingRemainingTime": "air_drying_remaining_time",
            "IonsRemainingTime": "ions_remaining_time",
            "DisinfectionRemainingTime": "disinfection_remaining_time",
        }
        for attr, field_name in time_mapping.items():
            val = get_value(attr)
            if val is not None:
                try:
                    setattr(self.state, field_name, int(float(val)))
                except (ValueError, TypeError):
                    pass

    async def async_update(self) -> None:
        """Poll device state and notify listeners only on change."""
        if self.account.rate_limited:
            _LOGGER.debug("Rate limited, skipping poll entirely")
            return
        await self._check_online_status()
        state = await self._query_properties()
        if state is not None:
            current_hash = self._state_hash()
            if current_hash != self._last_state_hash:
                self._last_state_hash = current_hash
                await self._notify_listeners()
        else:
            # Notify listeners on error so error_state sensor updates
            await self._notify_listeners()

    async def _check_online_status(self) -> None:
        """Query device online status from API (throttled)."""
        if time.time() - self._last_online_check < ONLINE_CHECK_INTERVAL:
            return
        self._last_online_check = time.time()
        if not await self._ensure_token_valid():
            return

        payload = build_base_payload(self.user_id, self.iot_id)
        payload["sign"] = generate_sign(payload)

        async with httpx_client.get_async_client(self.hass) as client:
            try:
                resp = await client.post(
                    API_ONLINE_STATUS,
                    json=payload,
                    headers=self._build_headers(),
                    timeout=10,
                )
                data = resp.json()

                if data.get("code") == "000":
                    self.account.note_cloud_success()
                    online = data.get("data", {}).get("onlineStatus", False)
                    if online != self.state.online:
                        self.state.online = online
                        _LOGGER.info("Device online status changed to: %s", online)
                elif data.get("code") == "403":
                    self._report_rate_limited()
            except Exception as err:
                _LOGGER.error("Online status check error: %s", err)
                self.account.note_cloud_failure(err)

    # ---- Control commands ----

    async def control_cover(self, action: str) -> bool:
        """Control the airer motor: up, down, stop."""
        prop_map = {
            "up": {"MotorControlMode": 1},
            "down": {"MotorControlMode": 2},
            "stop": {"MotorControlMode": 0},
        }
        if action not in prop_map:
            _LOGGER.error("Unknown cover action: %s", action)
            return False
        return await self._property_set(prop_map[action])

    async def control_switch(self, property_name: str, turn_on: bool) -> bool:
        """Control any device switch property."""
        return await self._property_set({property_name: 1 if turn_on else 0})

    async def set_brightness(self, brightness: int) -> bool:
        """Set light brightness (1-100) via invoke2."""
        brightness = max(1, min(100, brightness))
        return await self._invoke2(
            "LightBrightnessControl",
            {"Brightness": brightness},
        )

    async def _property_set(self, properties: dict[str, Any]) -> bool:
        """Send property set command via propertySet2 API."""
        if not await self._ensure_token_valid():
            return False

        self._bump_active_window()
        payload = build_base_payload(self.user_id, self.iot_id)
        payload["paramJson"] = json.dumps(properties)
        payload["sign"] = generate_sign(payload)

        return await self._send_request(API_PROPERTY_SET, payload)

    async def _invoke2(self, service_name: str, params: dict[str, Any]) -> bool:
        """Send service invoke command via invoke2 API."""
        if not await self._ensure_token_valid():
            return False

        self._bump_active_window()
        payload = build_base_payload(self.user_id, self.iot_id)
        payload["serviceName"] = service_name
        payload["paramJson"] = json.dumps(params)
        payload["sign"] = generate_sign(payload)

        return await self._send_request(API_INVOKE2, payload)

    async def _send_request(self, url: str, payload: dict[str, Any]) -> bool:
        """Send a POST request and handle response with auto-retry on 401."""
        async with httpx_client.get_async_client(self.hass) as client:
            try:
                resp = await client.post(
                    url,
                    json=payload,
                    headers=self._build_headers(),
                    timeout=10,
                )
                data = resp.json()

                if data.get("code") == "000":
                    _LOGGER.debug("API success on %s... (code=000)", url[-30:])
                    self.account.note_cloud_success()
                    return True

                if data.get("code") == "401":
                    _LOGGER.warning(
                        "Got 401 on %s..., attempting token refresh", url[-30:]
                    )
                    self.account._token_expired = True
                    if await self.async_refresh_token():
                        new_payload = build_base_payload(self.user_id, self.iot_id)
                        if "paramJson" in payload:
                            new_payload["paramJson"] = payload["paramJson"]
                        if "serviceName" in payload:
                            new_payload["serviceName"] = payload["serviceName"]
                        new_payload["sign"] = generate_sign(new_payload)
                        resp = await client.post(
                            url,
                            json=new_payload,
                            headers=self._build_headers(),
                            timeout=10,
                        )
                        data = resp.json()
                        return data.get("code") == "000"
                    _LOGGER.error("Token refresh failed, control command aborted")
                    return False

                if data.get("code") == "403":
                    self._report_rate_limited()
                    return False

                _LOGGER.warning("API error on %s...: %s", url[-30:], data)
                return False
            except Exception as err:
                _LOGGER.error("API request error on %s...: %s", url[-30:], err)
                self.account.note_cloud_failure(err)
                return False

    # ---- Polling management (state only; token refresh lives on account) ----

    def _report_rate_limited(self) -> None:
        """Server 403 — the account enters its 24h silence window."""
        _LOGGER.warning(
            "Server returned 403 (too many requests) for %s; account-wide "
            "silence for %ds",
            self.name,
            RATE_LIMIT_BACKOFF,
        )
        self.account.report_rate_limited(self.name)

    def _bump_active_window(self) -> None:
        """Keep polling at the fast rate for a while after a control command."""
        self._active_until = time.time() + POLL_ACTIVE_WINDOW

    async def start_polling(self) -> None:
        """Start dynamic-interval polling (fast while active, slow when idle)."""
        _LOGGER.info(
            "Starting device polling (idle=%ds, active=%ds)",
            POLL_INTERVAL_SLOW,
            POLL_INTERVAL_FAST,
        )

        # Immediate first update
        await self.async_update()
        self._schedule_next_poll(POLL_INTERVAL_SLOW)

    def _schedule_next_poll(self, delay: float) -> None:
        """Schedule the next poll at a dynamic interval."""
        self._unsub_poll = async_track_point_in_time(
            self.hass,
            self._poll_callback,
            dt_util.utcnow() + timedelta(seconds=delay),
        )

    def stop_polling(self) -> None:
        """Stop periodic state polling."""
        if self._unsub_poll is not None:
            self._unsub_poll()
            self._unsub_poll = None

    async def _poll_callback(self, now: Any = None) -> None:
        """Poll callback that reschedules itself at a dynamic interval."""
        self._unsub_poll = None
        try:
            await self.async_update()
        except Exception as err:
            _LOGGER.exception("Poll callback error: %s", err)
        moving = self.state.motor_control_mode in (1, 2)
        active = time.time() < self._active_until or moving
        self._schedule_next_poll(POLL_INTERVAL_FAST if active else POLL_INTERVAL_SLOW)


def _snake(name: str) -> str:
    """Convert CamelCase to snake_case."""
    mapping = {
        "PowerSwitch": "power_on",
        "LightSwitch": "light_on",
        "DryingSwitch": "drying_on",
        "AirDryingSwitch": "air_drying_on",
        "DisinfectionSwitch": "disinfection_on",
        "IonsSwitch": "ions_on",
    }
    return mapping.get(
        name,
        "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_"),
    )
