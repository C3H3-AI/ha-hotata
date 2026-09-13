"""Account layer: credentials, 403 failover state machine, notifications.

One :class:`HotataAccount` per config entry owns the cloud client(s). The
primary identity uses the tokens in ``entry.data`` (reference-project style);
the backup identity and failover state live in a HA Store so a restart during
an active failover resumes instead of hammering a penalized account. The
gateway client itself auto-refreshes tokens on auth rejection, so no manual
refresh scheduling is needed here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from homeassistant.components import persistent_notification as _pn

from .api import HotataApi
from .const import (
    CONF_BACKUP_PASSWORD,
    CONF_BACKUP_USERNAME,
    CONF_IOT_REFRESH_TOKEN,
    CONF_IOT_TOKEN,
    CONF_IDENTITY_ID,
    CONF_PASSWORD,
    CONF_REGISTERED_ID,
    CONF_USERNAME,
    DOMAIN,
    FAILOVER_SETTLE_DELAY,
    POLL_ACTIVE_WINDOW,
    RATE_LIMIT_BACKOFF,
    SWITCH_BACK_PROBE_DELAY,
    SWITCH_BACK_PROBE_MAX,
)
from .exceptions import HotataAuthError, HotataError, HotataRateLimited

if TYPE_CHECKING:
    from .coordinator import HotataCoordinator

_LOGGER = logging.getLogger(__name__)

# Runtime issue keys surfaced to the user (persistent notification +
# integration-status sensor).
ISSUE_RATE_LIMITED = "rate_limited"
ISSUE_CONNECTION = "connection_error"


class HotataAccount:
    """Owns cloud identities and the 403 failover state machine."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the account from the config entry."""
        self.hass = hass
        self.entry = entry
        self.coordinator: HotataCoordinator | None = None
        session = async_get_clientsession(hass)

        self._username: str = entry.data.get(CONF_USERNAME, "")
        self._password: str = entry.data.get(CONF_PASSWORD, "")
        self._backup_username: str = entry.data.get(CONF_BACKUP_USERNAME, "")
        self._backup_password: str = entry.data.get(CONF_BACKUP_PASSWORD, "")

        self._primary_api = HotataApi(
            session,
            username=self._username,
            password=self._password,
            iot_token=entry.data.get(CONF_IOT_TOKEN),
            iot_refresh_token=entry.data.get(CONF_IOT_REFRESH_TOKEN),
            identity_id=entry.data.get(CONF_IDENTITY_ID),
            registered_id=entry.data.get(CONF_REGISTERED_ID),
        )
        self._backup_api: HotataApi | None = None

        # Failover state machine. Each identity carries its own 403 penalty
        # deadline; the account is silent only while the *active* identity is
        # penalized. All runtime failover state persists in a Store.
        self._using_backup = False
        self._switch_lock = asyncio.Lock()
        self._primary_penalty_until = 0.0
        self._backup_penalty_until = 0.0
        self._unsub_switch_back: Callable[[], None] | None = None
        self._switch_back_failures = 0
        self._account_store = Store(hass, 1, f"{DOMAIN}.account.{entry.entry_id}")

        # Cloud health tracking.
        self._active_issues: dict[str, str] = {}
        self._conn_failure_count = 0
        # Dynamic polling: fast while a control command was recent.
        self._active_until = 0.0

    # ---- identity management ----

    @property
    def api(self) -> HotataApi:
        """Return the cloud client of the active identity."""
        if self._using_backup and self._backup_api is not None:
            return self._backup_api
        return self._primary_api

    @property
    def using_backup(self) -> bool:
        """True while the backup identity is active."""
        return self._using_backup

    def _backup_available(self) -> bool:
        return bool(self._backup_username and self._backup_password)

    @staticmethod
    def _mask_username(username: str) -> str:
        """Mask a phone-number style username for user-facing messages."""
        if len(username) > 7:
            return f"{username[:3]}****{username[-4:]}"
        return username or "?"

    # ---- setup / persistence ----

    async def async_setup(self) -> None:
        """Load persisted failover state and ensure a usable primary token.

        Raises :class:`HotataAuthError` when no token exists and login fails,
        so setup can convert it into a ConfigEntryAuthFailed.
        """
        data = await self._account_store.async_load() or {}
        self._primary_penalty_until = float(
            data.get("primary_penalty_until", 0) or 0
        )
        self._backup_penalty_until = float(
            data.get("backup_penalty_until", 0) or 0
        )
        backup = data.get("backup") or {}
        if backup.get(CONF_IOT_TOKEN):
            self._backup_api = HotataApi(
                async_get_clientsession(self.hass),
                username=self._backup_username,
                password=self._backup_password,
                iot_token=backup.get(CONF_IOT_TOKEN),
                iot_refresh_token=backup.get(CONF_IOT_REFRESH_TOKEN),
                identity_id=backup.get(CONF_IDENTITY_ID),
                registered_id=backup.get(CONF_REGISTERED_ID),
            )
        if (
            data.get("using_backup")
            and self._backup_api is not None
            and time.time() < self._primary_penalty_until
        ):
            # Still inside the primary penalty window: resume the backup
            # session (its token refreshes itself on first gateway call).
            self._using_backup = True
            self._schedule_switch_back()
            _LOGGER.info(
                "Resuming backup session (primary penalty %.0fs left)",
                self._primary_penalty_until - time.time(),
            )
        elif data.get("using_backup"):
            # Penalty over — start fresh on the primary identity.
            self._using_backup = False
            self._primary_penalty_until = 0.0
        if not self._primary_api.iot_token:
            await self._primary_api.async_login()
            self._persist_primary_tokens()
        # Synchronous save during setup: a restart right after setup must
        # not lose the (possibly resumed) failover state.
        await self._account_store.async_save(self._runtime_state())

    def _persist_primary_tokens(self) -> None:
        """Write refreshed primary tokens back into the config entry.

        No update listener is registered, so this never triggers a reload.
        """
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                CONF_IOT_TOKEN: self._primary_api.iot_token,
                CONF_IOT_REFRESH_TOKEN: self._primary_api.iot_refresh_token,
                CONF_IDENTITY_ID: self._primary_api.identity_id,
                CONF_REGISTERED_ID: self._primary_api.registered_id,
            },
        )

    def maybe_persist_primary_tokens(self) -> None:
        """Persist primary tokens when the gateway client refreshed them."""
        if (
            not self._using_backup
            and self._primary_api.iot_token
            and self._primary_api.iot_token
            != self.entry.data.get(CONF_IOT_TOKEN)
        ):
            self._persist_primary_tokens()

    def _runtime_state(self) -> dict[str, Any]:
        """Build the failover state dict persisted to the Store."""
        backup: dict[str, Any] = {}
        if self._backup_api is not None:
            backup = {
                CONF_IOT_TOKEN: self._backup_api.iot_token,
                CONF_IOT_REFRESH_TOKEN: self._backup_api.iot_refresh_token,
                CONF_IDENTITY_ID: self._backup_api.identity_id,
                CONF_REGISTERED_ID: self._backup_api.registered_id,
            }
        return {
            "backup": backup,
            "using_backup": self._using_backup,
            "primary_penalty_until": self._primary_penalty_until,
            "backup_penalty_until": self._backup_penalty_until,
        }

    def _async_save_runtime_state(self) -> None:
        """Persist failover state to the Store (fire & forget)."""
        self.hass.async_create_task(
            self._account_store.async_save(self._runtime_state())
        )

    # ---- dynamic polling ----

    @property
    def rate_limited(self) -> bool:
        """True while the *active* identity is inside its 403 penalty."""
        if self._using_backup:
            return time.time() < self._backup_penalty_until
        return time.time() < self._primary_penalty_until

    @property
    def poll_active(self) -> bool:
        """True while recent control activity warrants fast polling."""
        return time.time() < self._active_until

    def bump_active_window(self) -> None:
        """Keep polling at the fast rate for a while after a command."""
        self._active_until = time.time() + POLL_ACTIVE_WINDOW

    # ---- cloud health issues ----

    def _issue_notification_id(self, key: str) -> str:
        return f"hotata_issue_{self.entry.entry_id}_{key}"

    def _async_report_issue(self, key: str, title: str, message: str) -> None:
        """Raise a user-visible issue once, with a persistent notification."""
        if self._active_issues.get(key) == message:
            return
        self._active_issues[key] = message
        try:
            _pn.async_create(
                self.hass,
                message=message,
                title=title,
                notification_id=self._issue_notification_id(key),
            )
        except Exception:
            pass
        self._async_push_issue_state()

    def _async_clear_issue(self, key: str) -> bool:
        """Clear an active issue; return True if it was active."""
        if key not in self._active_issues:
            return False
        del self._active_issues[key]
        try:
            _pn.async_dismiss(self.hass, self._issue_notification_id(key))
        except Exception:
            pass
        return True

    def _async_push_issue_state(self) -> None:
        """Refresh listeners so the status sensor reflects new issues."""
        if self.coordinator is not None:
            self.coordinator.async_update_listeners()

    def _async_notify_once(self, key: str, title: str, message: str) -> None:
        """Fire-and-forget persistent notification for state transitions."""
        try:
            _pn.async_create(
                self.hass,
                message=message,
                title=title,
                notification_id=self._issue_notification_id(key),
            )
        except Exception:
            pass

    def _async_dismiss_notification(self, key: str) -> None:
        """Dismiss a transition notification if present."""
        try:
            _pn.async_dismiss(self.hass, self._issue_notification_id(key))
        except Exception:
            pass

    @property
    def active_issues(self) -> dict[str, str]:
        """Return the currently active user-visible issues (key -> detail)."""
        return dict(self._active_issues)

    def report_rate_limited(self, detail: str = "") -> None:
        """The active identity got a 403 (操作过于频繁) from the cloud.

        The penalty is per-account: penalize the active identity and, when a
        backup account is configured, fail over to it instead of going fully
        silent. Straggler 403s are ignored while a penalty is already running
        so concurrent requests cannot extend it forever.
        """
        if self.rate_limited:
            return
        if self._using_backup:
            self._backup_penalty_until = time.time() + RATE_LIMIT_BACKOFF
            self._async_save_runtime_state()
            self._async_report_issue(
                ISSUE_RATE_LIMITED,
                "好太太云端限频（403 操作过于频繁）",
                "备用账号也被好太太云端限频（403 操作过于频繁），集成已暂停该"
                "账号的全部云端请求。\n\n主账号处罚解除后会自动切回主账号并继续"
                "重试；若两条账号均被限频，设备将暂停更新，属正常现象。"
                + (f"\n\n设备：{detail}" if detail else ""),
            )
            self._schedule_switch_back()
            return
        self._primary_penalty_until = time.time() + RATE_LIMIT_BACKOFF
        self._async_save_runtime_state()
        self._async_report_issue(
            ISSUE_RATE_LIMITED,
            "好太太云端限频（403 操作过于频繁）",
            "好太太云端返回「操作过于频繁」，主账号已被限频。\n\n"
            + (
                "集成正在自动切换到备用账号继续控制设备。"
                if self._backup_available()
                else "集成已停止全部云端请求 24 小时（轮询、控制命令均暂停），"
                "以避免处罚延长。期间设备实体将显示为不可用，处罚解除后自动恢复。"
            )
            + (f"\n\n设备：{detail}" if detail else ""),
        )
        if self._backup_available():
            self.hass.async_create_task(self._switch_to_backup())

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
        """A cloud request succeeded — reset failure count, clear issues.

        403 penalties are NOT cleared here: requests run concurrently and a
        straggler success must not end a penalty window early. Penalties end
        only via failover transitions or expiry.
        """
        self._conn_failure_count = 0
        if not self.rate_limited:
            self._async_clear_issue(ISSUE_RATE_LIMITED)
        if self._async_clear_issue(ISSUE_CONNECTION):
            self._async_notify_once(
                "recovery",
                "好太太云端已恢复",
                "好太太云端连接已恢复正常，轮询已继续。",
            )
            self._async_push_issue_state()

    # ---- backup failover ----

    async def _switch_to_backup(self) -> bool:
        """Login with the backup account and make it the active identity."""
        if not self._backup_available() or self._using_backup:
            return False
        if time.time() < self._backup_penalty_until:
            _LOGGER.info("Backup account still penalized, not switching")
            return False
        async with self._switch_lock:
            if self._using_backup:
                return False
            session = async_get_clientsession(self.hass)
            api = self._backup_api or HotataApi(
                session,
                username=self._backup_username,
                password=self._backup_password,
            )
            try:
                await api.async_login()
            except HotataRateLimited as err:
                self._backup_penalty_until = time.time() + RATE_LIMIT_BACKOFF
                self._backup_api = api
                self._async_save_runtime_state()
                self._async_report_issue(
                    ISSUE_RATE_LIMITED,
                    "好太太云端限频（403 操作过于频繁）",
                    "主账号被限频后尝试切换备用账号，但备用账号登录也被限频。"
                    "集成已暂停全部云端请求，主账号处罚解除后自动重试。",
                )
                self._schedule_switch_back()
                _LOGGER.warning("Backup login rate-limited: %s", err)
                return False
            except HotataError as err:
                _LOGGER.error("Backup login failed: %s", err)
                return False
            self._backup_api = api
            self._using_backup = True
            self._token_expiry_notified_reset()
            # Short settle window so in-flight primary requests drain before
            # the next poll goes out on the backup identity.
            self._backup_penalty_until = time.time() + FAILOVER_SETTLE_DELAY
            self._async_save_runtime_state()
            self._async_clear_issue(ISSUE_RATE_LIMITED)
            self._schedule_switch_back()
            self._async_notify_once(
                "backup_active",
                "好太太晾衣机：已切换备用账号",
                f"主账号已被好太太云端限频（403），已自动切换到备用账号"
                f" {self._mask_username(self._backup_username)} 继续控制设备。\n\n"
                "主账号处罚（最长约 24 小时）解除后，集成会自动切回主账号。",
            )
            _LOGGER.warning(
                "Primary rate-limited; switched to backup account %s",
                self._backup_username,
            )
            return True

    def _token_expiry_notified_reset(self) -> None:
        """Reset any one-shot notification guards on identity switch."""
        self._async_dismiss_notification("token_expired")

    def _schedule_switch_back(self) -> None:
        """(Re)arm the one-shot switch-back probe timer."""
        if self._unsub_switch_back is not None:
            self._unsub_switch_back()
            self._unsub_switch_back = None
        if not self._using_backup:
            return
        delay = self._primary_penalty_until - time.time()
        if delay <= 0:
            delay = FAILOVER_SETTLE_DELAY
        # Back off when the primary keeps failing its probe so a broken
        # primary cannot cause a request storm.
        if self._switch_back_failures:
            delay = min(
                delay + SWITCH_BACK_PROBE_DELAY * 2 ** self._switch_back_failures,
                SWITCH_BACK_PROBE_MAX,
            )
        _LOGGER.debug("Switch-back probe scheduled in %.0fs", delay)
        self._unsub_switch_back = async_call_later(
            self.hass, delay, self._switch_back_probe
        )

    async def _switch_back_probe(self, _now: Any = None) -> None:
        """Timer callback: try switching back to the primary identity."""
        self._unsub_switch_back = None
        if not self._using_backup:
            return
        await self._switch_back_to_primary()

    async def _switch_back_to_primary(self) -> bool:
        """Restore the primary identity once its penalty window has passed.

        Probe-first: the primary identity is activated only after a real
        account-level request succeeds, so the integration never runs on a
        still-penalized token (which would re-trigger 403 and loop forever).
        """
        if not self._username or not self._password or not self._using_backup:
            return False
        if time.time() < self._primary_penalty_until:
            self._schedule_switch_back()
            return False
        async with self._switch_lock:
            if not self._using_backup:
                return False
            try:
                # Probe: account-level read (listBindings). A fresh IoT
                # credential does NOT lift a throttled account's penalty,
                # so verify with a real request before switching.
                await self._primary_api.async_list_devices()
            except HotataRateLimited:
                self._primary_penalty_until = (
                    time.time() + SWITCH_BACK_PROBE_DELAY
                )
                self._switch_back_failures += 1
                self._async_save_runtime_state()
                self._schedule_switch_back()
                _LOGGER.info("Primary still rate-limited, staying on backup")
                return False
            except HotataAuthError as err:
                # Credentials rejected — refresh was already attempted inside
                # the client; retry later on the probe schedule.
                self._switch_back_failures += 1
                self._schedule_switch_back()
                _LOGGER.warning("Primary probe auth failed: %s", err)
                return False
            except HotataError as err:
                self._switch_back_failures += 1
                self._schedule_switch_back()
                _LOGGER.warning("Primary probe failed: %s", err)
                return False
            # Probe OK — activate the primary identity.
            self._using_backup = False
            self._primary_penalty_until = 0.0
            self._switch_back_failures = 0
            self._persist_primary_tokens()
            self._async_save_runtime_state()
            self._async_clear_issue(ISSUE_RATE_LIMITED)
            self._async_dismiss_notification("backup_active")
            # Cancel the (now obsolete) switch-back probe timer.
            self._schedule_switch_back()
            self._async_notify_once(
                "primary_restored",
                "好太太晾衣机：已切回主账号",
                "主账号限频期已过，已切回主账号继续控制设备。",
            )
            _LOGGER.info("Switched back to primary account")
            return True

    async def async_unload(self) -> None:
        """Cancel timers and persist final state on unload."""
        if self._unsub_switch_back is not None:
            self._unsub_switch_back()
            self._unsub_switch_back = None
        self._async_save_runtime_state()
        await self._account_store.async_save(
            {
                "backup": (
                    {
                        CONF_IOT_TOKEN: self._backup_api.iot_token,
                        CONF_IOT_REFRESH_TOKEN: self._backup_api.iot_refresh_token,
                        CONF_IDENTITY_ID: self._backup_api.identity_id,
                        CONF_REGISTERED_ID: self._backup_api.registered_id,
                    }
                    if self._backup_api is not None
                    else {}
                ),
                "using_backup": self._using_backup,
                "primary_penalty_until": self._primary_penalty_until,
                "backup_penalty_until": self._backup_penalty_until,
            }
        )
