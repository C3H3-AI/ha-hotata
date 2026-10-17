"""Polling coordinator for Hotata cloud devices.

Polls with a dynamic interval: fast (5s) while a motor runs or a command was
just sent, slow (30s) otherwise. Any offline device pins the cadence at 30s
and only its connectivity is probed — property and TSL reads are skipped for
it, because an offline device rejects them and the attempts only feed the
cloud's throttle. The first poll that sees the device back online restores the
normal fast/slow choice.

A cloud 403 (操作过于频繁) penalizes the active identity through the account's
failover state machine instead of being retried, so throttling never amplifies
itself.

Devices are discovered recursively: gateways are expanded into their
sub-devices, and each device's thing model is fetched to decide which
capabilities it really exposes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DESCENT_TIME,
    CONF_ENABLE_PUSH,
    DEFAULT_DESCENT_TIME,
    DOMAIN,
    EVENT_BUTTON_PRODUCT_KEYS,
    GATEWAY_PRODUCT_KEYS,
    POLL_INTERVAL_FAST,
    POLL_INTERVAL_OFFLINE,
    POLL_INTERVAL_PUSH_FALLBACK,
    POLL_INTERVAL_SLOW,
    PUSH_RETRY_INTERVAL,
)
from .exceptions import HotataAuthError, HotataError, HotataRateLimited
from .hub import HotataAccount
from .models import HotataDevice
from .mqtt_push import HotataPushClient

_LOGGER = logging.getLogger(__name__)


#: Bumped when the meaning of ``invert_direction`` changes, so stored choices
#: can be migrated instead of silently flipping the user's direction.
DIRECTION_SEMANTICS_VERSION = 2


@dataclass
class DeviceRuntime:
    """Per-device client-side state: config + cover position simulation."""

    hass: HomeAssistant
    iot_id: str
    store: Store = None  # type: ignore[assignment]
    descent_time: int = DEFAULT_DESCENT_TIME
    #: Local-only preference (issue #15). Off means the 4.0.14 default — open
    #: (展开) lowers the rail; on means the reverse, open raises it. Nothing is
    #: ever sent to the cloud.
    invert_direction: bool = False
    # Shared position-simulation state (written by the cover, the reset
    # button and the descent-time number).
    simulated_position: int = 100
    closing_start: float | None = None
    target_position: int | None = None

    async def async_load(self) -> None:
        """Load the persisted descent time."""
        self.store = Store(self.hass, 1, f"{DOMAIN}.descent_time.{self.iot_id}")
        data = await self.store.async_load() or {}
        if "descent_time" in data:
            self.descent_time = int(data["descent_time"])
        if "invert_direction" in data:
            self.invert_direction = bool(data["invert_direction"])
            if data.get("direction_semantics") != DIRECTION_SEMANTICS_VERSION:
                # 4.0.14 made "off" mean 打开=放下. Whoever had the switch on
                # asked for exactly that, and whoever left it alone was on the
                # old default — so both migrate to the new default: off. Write
                # it back so the stored value stops disagreeing with the live
                # one (and so the migration cannot run twice).
                self.invert_direction = False
                await self._async_save()

    async def async_set_descent_time(self, value: int) -> None:
        """Persist a new descent time."""
        self.descent_time = value
        await self._async_save()

    async def async_set_invert_direction(self, value: bool) -> None:
        """Persist the direction preference (local only, no cloud call)."""
        self.invert_direction = bool(value)
        await self._async_save()

    async def _async_save(self) -> None:
        """Write every per-device preference, so one never drops another."""
        await self.store.async_save(
            {
                "descent_time": self.descent_time,
                "invert_direction": self.invert_direction,
                "direction_semantics": DIRECTION_SEMANTICS_VERSION,
            }
        )


class HotataCoordinator(DataUpdateCoordinator[dict[str, HotataDevice]]):
    """Keep device metadata, online state and properties fresh."""

    # 长连接推送（MQTT）的默认状态，__init__ 会按配置条目覆盖 push_enabled。
    # 声明在类上，使绕过 __init__ 构造的协程（测试桩）不会缺字段。
    push_enabled: bool = False
    push_client: HotataPushClient | None = None
    push_bound: bool = False
    _push_starting: bool = False
    _last_push_retry: float = 0.0

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        account: HotataAccount,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL_SLOW),
        )
        self.entry = entry
        self.account = account
        self.thing_models: dict[str, dict] = {}
        self.runtimes: dict[str, DeviceRuntime] = {}

        # 长连接推送（MQTT）
        self.push_enabled: bool = bool(
            entry.data.get(CONF_ENABLE_PUSH, True)
        )
        self.push_client: HotataPushClient | None = None
        self.push_bound: bool = False
        self._push_starting = False
        self._last_push_retry = 0.0

    def runtime(self, iot_id: str) -> DeviceRuntime:
        """Return (and lazily create) the runtime state for one device."""
        runtime = self.runtimes.get(iot_id)
        if runtime is None:
            runtime = DeviceRuntime(
                hass=self.hass,
                iot_id=iot_id,
                descent_time=int(
                    self.entry.data.get(CONF_DESCENT_TIME, DEFAULT_DESCENT_TIME)
                ),
            )
            self.runtimes[iot_id] = runtime
            self.hass.async_create_task(runtime.async_load())
        return runtime

    @property
    def first_device_id(self) -> str | None:
        """The first discovered device id, for probing and status display."""
        data = self.data or {}
        return next(iter(data), None)

    # ---------------- 长连接推送 ----------------

    async def async_setup_push(self) -> None:
        """启动长连接推送；失败则保持轮询并在后续轮询中按间隔重试"""
        if not self.push_enabled or self._push_starting:
            return
        if self.push_client is not None and self.push_client.connected:
            return
        self._push_starting = True
        try:
            await self._async_setup_push_inner()
        finally:
            self._push_starting = False

    async def _async_rebuild_push_if_stale(self) -> None:
        """Rebuild the long connection when it is missing or its token is stale.

        A bound connection keeps its account binding only for the iotToken it
        was bound with: once the token rotates, pushes silently stop arriving
        while the socket still looks healthy. Comparing the token we bound with
        against the current one turns that silent degradation into a rebuild.
        """
        if not self.push_enabled:
            return
        client = self.push_client
        rotated = bool(
            client is not None
            and client.bound_token
            and self.account.api.iot_token
            and client.bound_token != self.account.api.iot_token
        )
        if rotated:
            _LOGGER.info("iotToken 已轮换，重建长连接以重新绑定账号")
            self.async_stop_push()
            client = None
        if (client is None or not client.connected) and (
            time.time() - self._last_push_retry >= PUSH_RETRY_INTERVAL
        ):
            await self.async_setup_push()

    async def _async_setup_push_inner(self) -> None:
        # 确保有可用 iotToken（bind 需要）
        if not self.account.api.iot_token:
            try:
                await self.account.api.async_refresh_iot_credential()
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("推送启动前刷新 token 失败: %s", e)
        if not self.account.api.iot_token:
            _LOGGER.warning("无可用 iotToken，跳过长连接推送启动")
            return

        try:
            aepauth = await self.account.api.async_aepauth()
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning(
                "aepauth 获取 MQTT 三元组失败（将保持轮询）: %s", e
            )
            self._last_push_retry = time.time()
            return

        client = HotataPushClient(
            get_iot_token=lambda: self.account.api.iot_token or "",
            on_properties_pushed=self._handle_properties_push,
            on_bound=self._handle_push_bound,
            on_status_pushed=self._handle_status_push,
            on_event_pushed=self._handle_event_push,
        )
        try:
            bound = await client.async_start(self.hass.loop, aepauth)
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("长连接推送启动失败（将保持轮询）: %s", e)
            self._last_push_retry = time.time()
            return

        self.push_client = client
        self._last_push_retry = time.time()
        self.push_bound = bound
        if not bound:
            _LOGGER.info("长连接已连接但未绑定，将保持轮询等待重试")

    def _handle_push_bound(self, bound: bool) -> None:
        """bind/unbind 状态变化（事件循环线程回调）"""
        if bound == self.push_bound:
            return
        self.push_bound = bound
        _LOGGER.info("长连接绑定状态 -> %s", "已绑定" if bound else "未绑定")
        self._apply_push_interval()

    def _apply_push_interval(self) -> None:
        """按推送状态与活动窗口选择轮询节奏"""
        if self.push_bound:
            # 属性变化走推送：非活动期用兜底间隔，活动期仍走 fast
            if not self.account.poll_active and self.update_interval != timedelta(
                seconds=POLL_INTERVAL_PUSH_FALLBACK
            ):
                self.update_interval = timedelta(
                    seconds=POLL_INTERVAL_PUSH_FALLBACK
                )
        # 未绑定时由 _adjust_poll_interval 的常规逻辑接管

    def _handle_properties_push(self, iot_id: str, items: dict) -> None:
        """收到设备属性推送（事件循环线程回调）：合并进缓存并立即通知实体"""
        if not isinstance(items, dict) or not items:
            return
        data = self.data
        if not data or iot_id not in data:
            # 未知设备（或首轮前到达）：丢弃，交给下一次轮询发现
            _LOGGER.debug("推送到来但设备未知，忽略: %s", iot_id)
            return
        device = data[iot_id]
        merged = dict(device.properties or {})
        for key, val in items.items():
            merged[key] = val
        device.properties = merged
        # 同步写回 coordinator.data（同一对象引用，实体通过 self.coordinator.data 读取）
        self.async_set_updated_data(data)

    def _handle_status_push(self, iot_id: str, online: bool) -> None:
        """收到设备上下线推送（事件循环线程回调）：实时更新在线状态"""
        data = self.data
        if not data or iot_id not in data:
            _LOGGER.debug("上下线推送到来但设备未知，忽略: %s", iot_id)
            return
        device = data[iot_id]
        if device.online == online:
            return
        device.online = online
        _LOGGER.debug("设备在线状态推送更新 iot_id=%s online=%s", iot_id, online)
        self.async_set_updated_data(data)
        # 上线瞬间补齐属性（离线期跳过的轮询），离线则无需拉取
        if online:
            self.hass.async_create_task(self._async_refresh_device(iot_id))

    def _handle_event_push(self, iot_id: str, info: dict) -> None:
        """收到设备事件推送（事件循环线程回调）：写入事件流供 Event 实体触发"""
        data = self.data
        if not data or iot_id not in data:
            _LOGGER.debug("事件推送到来但设备未知，忽略: %s", iot_id)
            return
        device = data[iot_id]
        identifier = info.get("identifier")
        if not identifier:
            return
        event_data = info.get("data")
        if not isinstance(event_data, dict):
            event_data = {"value": event_data}
        merged = dict(device.properties or {})
        merged[identifier] = {
            "value": event_data,
            "time": info.get("time"),
        }
        device.properties = merged
        _LOGGER.debug(
            "设备事件推送写入 iot_id=%s identifier=%s", iot_id, identifier
        )
        self.async_set_updated_data(data)

    async def _async_refresh_device(self, iot_id: str) -> None:
        """单设备属性即时刷新（上下线推送触发，避免全量轮询）"""
        data = self.data
        if not data or iot_id not in data:
            return
        device = data[iot_id]
        try:
            props = await self.account.api.async_get_properties(iot_id)
        except HotataError as err:
            _LOGGER.debug("即时属性刷新失败 %s: %s", iot_id, err)
            return
        if props:
            device.properties = props
            self.async_set_updated_data(data)

    def async_stop_push(self) -> None:
        """停掉长连接（可在任意线程调用，unload 时经 executor 执行）"""
        client = self.push_client
        self.push_client = None
        self.push_bound = False
        if client is not None:
            client.stop()

    async def _async_discover_devices(self) -> list[HotataDevice]:
        """Discover direct bindings and recursively exposed gateway children."""
        devices = await self.account.api.async_list_devices()
        by_id = {device.iot_id: device for device in devices}
        queue = [
            device
            for device in devices
            if device.product_key in GATEWAY_PRODUCT_KEYS
        ]
        visited: set[str] = set()
        while queue:
            gateway = queue.pop(0)
            if gateway.iot_id in visited:
                continue
            visited.add(gateway.iot_id)
            try:
                children = await self.account.api.async_list_subdevices(
                    gateway.iot_id
                )
            except HotataError as err:
                _LOGGER.debug(
                    "Subdevice discovery failed for %s: %s",
                    gateway.iot_id,
                    err,
                )
                continue
            for child in children:
                child.parent_iot_id = gateway.iot_id
                if child.iot_id not in by_id:
                    by_id[child.iot_id] = child
                elif not by_id[child.iot_id].parent_iot_id:
                    by_id[child.iot_id].parent_iot_id = gateway.iot_id
                if child.product_key in GATEWAY_PRODUCT_KEYS:
                    queue.append(child)
        return list(by_id.values())

    async def _async_update_data(self) -> dict[str, HotataDevice]:
        # 长连接未建立/已失效：按间隔重建（有存活客户端时交给 paho 自动重连）
        await self._async_rebuild_push_if_stale()
        if self.account.rate_limited:
            # Silence window: issue zero requests, keep last known data.
            _LOGGER.debug("Rate limited, skipping poll entirely")
            return self.data or {}
        try:
            devices = await self._async_discover_devices()
            for device in devices:
                # Connectivity first: a device that is down should not be
                # polled hard, and the cadence drops to the offline interval
                # below.
                try:
                    device.online = await self.account.api.async_get_online(
                        device.iot_id
                    )
                except HotataError as err:
                    _LOGGER.debug(
                        "Status update failed for %s: %s",
                        device.iot_id,
                        err,
                    )
                # Warm-up: the first poll has to establish what the device *is*
                # — its thing model and its reported capabilities — even while
                # it is offline. Measured on a device the cloud reports as
                # offline (status 3): /thing/properties/get still answers with
                # the full shadow (DeviceModelType included) and
                # /thing/tsl/get with the whole declaration. Skipping both
                # leaves has_property() with nothing to stand on, so none of
                # that device's entities are ever created and automations lose
                # their entity ids on every restart. Only a device that is both
                # offline *and* already known is skipped.
                cache_key = device.product_key or device.iot_id
                previous = (self.data or {}).get(device.iot_id)
                warm = (
                    cache_key in self.thing_models
                    and previous is not None
                    and bool(previous.properties)
                )
                if device.online is False and warm:
                    # Keep the last known properties so entities stay put
                    # (they go unavailable via HotataEntity.available) and we
                    # can tell the moment it comes back. Device objects are
                    # rebuilt from the device list on every poll, so this has
                    # to be carried over explicitly.
                    device.properties = previous.properties
                    device.thing_model = self.thing_models[cache_key]
                    _LOGGER.debug(
                        "Device %s offline, skipping property poll",
                        device.iot_id,
                    )
                    continue
                if self.push_bound and not self.account.poll_active and warm:
                    # 长连接已绑定且不在活动窗口：属性有 /thing/properties 推送、
                    # 事件有 /thing/events 推送、上下线有 /thing/status 推送，
                    # 这一轮兜底就只留在线探测。这正是长连接省下的那部分请求——
                    # 账号曾因轮询过密被云端 403，绑定后每轮每设备从 2 次降到 1 次。
                    # 这里把上一轮属性连同 TSL 一起带过来（对象每轮都会重建），
                    # 推送写进去的值因此不会丢。
                    device.properties = previous.properties
                    device.thing_model = self.thing_models[cache_key]
                    _LOGGER.debug(
                        "Device %s push-bound and idle, skipping property poll",
                        device.iot_id,
                    )
                    continue
                try:
                    device.properties = await self.account.api.async_get_properties(
                        device.iot_id
                    )
                except HotataError as err:
                    _LOGGER.debug(
                        "Property update failed for %s: %s",
                        device.iot_id,
                        err,
                    )
                if device.product_key in EVENT_BUTTON_PRODUCT_KEYS:
                    try:
                        event = await self.account.api.async_get_latest_event(
                            device.iot_id, "KeyEvent"
                        )
                        if event is not None:
                            device.properties["KeyEvent"] = {"value": event}
                    except HotataError as err:
                        _LOGGER.debug(
                            "Event update failed for %s: %s",
                            device.iot_id,
                            err,
                        )
                try:
                    if cache_key not in self.thing_models:
                        self.thing_models[cache_key] = (
                            await self.account.api.async_get_thing_model(
                                device.iot_id
                            )
                        )
                    device.thing_model = self.thing_models[cache_key]
                except HotataError as err:
                    _LOGGER.debug(
                        "Thing-model update failed for %s: %s",
                        device.iot_id,
                        err,
                    )
            self.account.note_cloud_success()
            self.account.maybe_persist_primary_tokens()
            self._adjust_poll_interval(devices)
            return {device.iot_id: device for device in devices}
        except HotataRateLimited as err:
            self.account.report_rate_limited()
            raise UpdateFailed(f"Cloud rate limit: {err}") from err
        except HotataAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HotataError as err:
            self.account.note_cloud_failure(err)
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            self.account.note_cloud_failure(err)
            raise UpdateFailed(str(err)) from err

    def _adjust_poll_interval(self, devices: list[HotataDevice]) -> None:
        """Pick the poll cadence from device state and recent activity.

        - any device offline -> offline interval (connectivity probe only,
          see _async_update_data), so we notice recovery without hammering
          a device that cannot answer;
        - a motor moving or a command just sent -> fast;
        - otherwise -> slow.
        """
        offline = any(device.online is False for device in devices)
        moving = any(
            _is_motor_running(device.properties) for device in devices
        )
        fast = timedelta(seconds=POLL_INTERVAL_FAST)
        slow = timedelta(seconds=POLL_INTERVAL_SLOW)
        offline_interval = timedelta(seconds=POLL_INTERVAL_OFFLINE)
        if offline:
            # Offline wins over "active": a probe cadence is all that is
            # useful, and the next successful poll re-enters the normal
            # fast/slow choice because device.online flips back to True.
            wanted = offline_interval
        else:
            active = moving or self.account.poll_active
            if active:
                wanted = fast
            elif self.push_bound:
                # 属性/事件/上下线都走长连接推送：非活动期只需低频在线探测
                wanted = timedelta(seconds=POLL_INTERVAL_PUSH_FALLBACK)
            else:
                wanted = slow
        if self.update_interval != wanted:
            self.update_interval = wanted
            _LOGGER.debug(
                "Poll interval -> %ss (offline=%s moving=%s)",
                wanted.total_seconds(),
                offline,
                moving,
            )


def _is_motor_running(properties: dict[str, Any]) -> bool:
    """True while any rail motor is reported as opening/closing."""
    for identifier in (
        "MotorControlMode",
        "ApoleMotorControlMode",
        "BpoleMotorControlMode",
    ):
        if properties.get(identifier) in (1, 2):
            return True
    return False
