"""好太太长连接推送客户端（阿里云 IoT MQTT，App 侧通道）

链路（与 slac 同一网关 api.link.aliyun.com，实测 bind code=200）：
  1. HotataApi.async_aepauth() → /app/aepauth/handle 换取 MQTT 三元组
     （productKey/deviceName/deviceSecret，每次会话随机生成）
  2. 连接 {pk}.iot-as-mqtt.cn-shanghai.aliyuncs.com:1883（TLS 不校验证书）
     - clientId = "{dn}&{pk}|securemode=2,signmethod=hmacsha1,ext=1|"
     - username = "{dn}&{pk}"
     - password = upper(HmacSHA1(deviceSecret, "clientId..deviceName..productKey.."))
  3. 连上后订阅 /sys/{pk}/{dn}/app/down/#，
     并 publish /sys/{pk}/{dn}/app/up/account/bind（params.iotToken = 当前 iotToken）
  4. 收到 bind_reply code=200 后，设备属性变化时云端实时推送
     /sys/{pk}/{dn}/app/down/thing/properties，payload 结构兼容
     /thing/properties/get 返回的 data（{Key: {time, value}}）。

paho-mqtt 回调在后台线程执行，所有 HA 状态操作统一经
hass.loop.call_soon_threadsafe 回到事件循环。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import ssl
import threading
import time
from functools import partial
import uuid
from collections.abc import Callable

import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger(__name__)

MQTT_PORT = 1883
MQTT_KEEPALIVE = 65
BIND_TIMEOUT = 15  # 等待 bind_reply 的秒数
RECONNECT_DELAY_MIN = 1
RECONNECT_DELAY_MAX = 60


def _mqtt_has_v2_api() -> bool:
    """True when paho exposes the 2.x callback API.

    paho 1.x has no ``CallbackAPIVersion`` (and 1.6.1 is what many systems
    carry, including installs alongside HA's own MQTT integration), so the
    client is built for whichever API is present instead of assuming 2.x.
    """
    return hasattr(mqtt, "CallbackAPIVersion")


def _new_client(client_id: str) -> mqtt.Client:
    """Build a paho client for whichever callback API is installed."""
    if _mqtt_has_v2_api():
        return mqtt.Client(
            client_id=client_id,
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
    return mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)


def _make_password(device_secret: str, product_key: str, device_name: str, base_id: str) -> str:
    """阿里云 IoT MQTT 密码：按 key 排序拼接 clientId/deviceName/productKey 后 HMAC-SHA1（大写 hex）"""
    parts = "".join(
        f"{k}{v}"
        for k, v in sorted(
            {"clientId": base_id, "deviceName": device_name, "productKey": product_key}.items()
        )
    )
    return hmac.new(device_secret.encode(), parts.encode(), hashlib.sha1).hexdigest().upper()


class HotataPushClient:
    """长连接推送：MQTT 连接 + account bind + 属性推送分发"""

    def __init__(
        self,
        get_iot_token: Callable[[], str],
        on_properties_pushed: Callable[[str, dict], None],
        on_bound: Callable[[bool], None] | None = None,
        on_status_pushed: Callable[[str, bool], None] | None = None,
        on_event_pushed: Callable[[str, dict], None] | None = None,
        on_bind_auth_failed: Callable[[], None] | None = None,
    ) -> None:
        self._get_iot_token = get_iot_token
        self._on_properties_pushed = on_properties_pushed
        self._on_bound = on_bound
        self._on_status_pushed = on_status_pushed
        self._on_event_pushed = on_event_pushed
        self._on_bind_auth_failed = on_bind_auth_failed

        self._client: mqtt.Client | None = None
        self._product_key = ""
        self._device_name = ""
        self._base_id = ""
        self._bind_event = threading.Event()
        self._bound = False
        self._bind_retried = False
        self._started = False
        self._hass_loop: asyncio.AbstractEventLoop | None = None
        #: bind 时使用的 iotToken，供 coordinator 判断 token 轮换后是否需要重连
        self.bound_token: str = ""

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected())

    @property
    def bound(self) -> bool:
        return self._bound

    async def async_start(self, hass_loop, aepauth_data: dict) -> bool:
        """启动长连接。aepauth_data 为 HotataApi.async_aepauth() 的返回（data 字段）。

        必须在事件循环中调用（内部用 asyncio 轮询等待 bind 结果）。
        """
        if self._started:
            self.stop()
        pk = aepauth_data.get("productKey", "")
        dn = aepauth_data.get("deviceName", "")
        ds = aepauth_data.get("deviceSecret", "")
        if not (pk and dn and ds):
            # Name only what is missing — never echo the payload: it carries
            # deviceSecret, which is the password used to reach the broker.
            missing = [
                name
                for name, value in (
                    ("productKey", pk),
                    ("deviceName", dn),
                    ("deviceSecret", ds),
                )
                if not value
            ]
            _LOGGER.warning(
                "aepauth 数据不完整（缺 %s），跳过长连接", "、".join(missing) or "全部字段"
            )
            return False

        self._product_key = pk
        self._device_name = dn
        self._base_id = f"{dn}&{pk}"
        password = _make_password(ds, pk, dn, self._base_id)
        client_id = f"{self._base_id}|securemode=2,signmethod=hmacsha1,ext=1|"
        host = f"{pk}.iot-as-mqtt.cn-shanghai.aliyuncs.com"

        self._bind_event = threading.Event()
        self._bound = False
        self._bind_retried = False
        self.bound_token = ""
        self._hass_loop = hass_loop

        client = _new_client(client_id)
        client.username_pw_set(self._base_id, password)
        client.reconnect_delay_set(RECONNECT_DELAY_MIN, RECONNECT_DELAY_MAX)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client

        try:
            # tls_set 会同步加载 CA 证书（context.load_default_certs），HA 会在
            # 事件循环里报 blocking call；connect 也会做 DNS/TCP/CONNACK。两者
            # 都放线程池。
            loop = asyncio.get_running_loop()
            # run_in_executor 只接受位置参数，keyword 要先包成 partial。
            # 与 APP 一致，不校验证书
            await loop.run_in_executor(
                None, partial(client.tls_set, cert_reqs=ssl.CERT_NONE))
            await loop.run_in_executor(
                None, client.connect, host, MQTT_PORT, MQTT_KEEPALIVE)
        except Exception as e:
            _LOGGER.error("长连接 MQTT 连接失败: %s", e)
            self._client = None
            return False

        client.loop_start()  # 后台线程处理网络循环
        self._started = True

        # 等待 bind_reply（事件由后台线程 set，线程安全）
        deadline = time.monotonic() + BIND_TIMEOUT
        while time.monotonic() < deadline:
            if self._bind_event.is_set():
                break
            await asyncio.sleep(0.2)

        if not self._bound:
            _LOGGER.warning(
                "长连接 bind 未确认（%ss 内无 bind_reply），连接保持但推送可能不生效",
                BIND_TIMEOUT,
            )
        else:
            _LOGGER.info("长连接已建立并绑定账号，设备状态将实时推送")
        return self._bound

    def stop(self) -> None:
        """断开并释放资源（可在任意线程调用）"""
        client = self._client
        self._client = None
        self._started = False
        if client is None:
            return
        try:
            if self._product_key and self._device_name and self._bound:
                payload = json.dumps({
                    "id": str(uuid.uuid4().int & 0xFFFFFFFF),
                    "system": {"version": "1.0", "time": str(int(time.time() * 1000))},
                    "request": {"clientId": self._base_id},
                    "params": None,
                }, ensure_ascii=False, separators=(",", ":"))
                client.publish(
                    f"/sys/{self._product_key}/{self._device_name}/app/up/account/unbind",
                    payload, qos=1)
                time.sleep(0.5)  # 给后台线程一点时间发出 unbind
        except Exception:  # noqa: BLE001 - 退出路径尽力而为
            pass
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        self._bound = False
        self.bound_token = ""

    # ---------------- paho 回调（后台线程） ----------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        rc = getattr(reason_code, "value", reason_code)
        if rc != 0:
            _LOGGER.warning("长连接 MQTT on_connect rc=%s", rc)
            return
        _LOGGER.debug("长连接 MQTT 已连接，订阅下行 topic")
        client.subscribe(f"/sys/{self._product_key}/{self._device_name}/app/down/#", qos=1)
        self._bind_retried = False
        self._publish_bind(client)

    def _publish_bind(self, client) -> None:
        """向云端发布账号绑定，携带当前 iotToken。"""
        iot_token = ""
        try:
            iot_token = self._get_iot_token()
        except Exception:  # noqa: BLE001
            pass
        self.bound_token = iot_token
        payload = json.dumps({
            "id": str(uuid.uuid4().int & 0xFFFFFFFF),
            "system": {"version": "1.0", "time": str(int(time.time() * 1000))},
            "request": {"clientId": self._base_id},
            "params": {"iotToken": iot_token},
        }, ensure_ascii=False, separators=(",", ":"))
        client.publish(
            f"/sys/{self._product_key}/{self._device_name}/app/up/account/bind",
            payload, qos=1)

    def request_bind(self) -> None:
        """用最新 iotToken 重新发布一次 bind（token 刷新后由 coordinator 调用）。"""
        client = self._client
        if client is None or not client.is_connected():
            return
        self._bind_event.clear()
        self._publish_bind(client)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        rc = getattr(reason_code, "value", reason_code)
        if rc != 0:
            # paho 自动重连（reconnect_delay_set），重连成功后 on_connect 会重新 bind
            _LOGGER.warning("长连接 MQTT 断开 rc=%s，将自动重连", rc)
            self._bound = False
        else:
            _LOGGER.debug("长连接 MQTT 主动断开")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            body = msg.payload.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return

        if "/account/bind_reply" in topic:
            data: dict = {}
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    data = parsed
            except json.JSONDecodeError:
                pass
            code = data.get("code")
            self._bound = code == 200
            if self._bound:
                _LOGGER.info("长连接账号绑定成功 (bind_reply code=200)")
            else:
                _LOGGER.warning(
                    "长连接账号绑定失败 code=%s message=%s",
                    code,
                    data.get("message") or data.get("msg") or body[:200],
                )
                if code == 2043 and not self._bind_retried:
                    # iotToken 失效：交给 coordinator 刷新 token 后重发一次。
                    # 只重试一次，避免 token 真的不可用时把云端刷爆。
                    self._bind_retried = True
                    _LOGGER.warning(
                        "bind 返回 2043（iotToken 失效），刷新 token 后重试一次"
                    )
                    if self._on_bind_auth_failed is not None:
                        self._call_in_loop(self._on_bind_auth_failed)
            self._bind_event.set()
            if self._on_bound:
                self._call_in_loop(lambda: self._on_bound(self._bound))
            return

        if "/account/unbind_reply" in topic:
            return

        if "/thing/properties" in topic:
            # 属性变化：这条分支以前根本不存在，_parse_properties_push 写好却
            # 没有调用点，于是"绑定成功"等于"属性不再轮询也不走推送" → 实体冻结。
            parsed = _parse_properties_push(body)
            if parsed and self._on_properties_pushed is not None:
                iot_id, items = parsed
                _LOGGER.debug(
                    "收到属性推送 iotId=%s keys=%s", iot_id, ",".join(items)
                )
                self._call_in_loop(
                    lambda: self._on_properties_pushed(iot_id, items)
                )
            return

        if "/thing/status" in topic:
            self._parse_status_push(body)
            return

        if "/thing/events" in topic:
            self._parse_events_push(topic, body)
            return

        _LOGGER.debug("长连接收到其他消息: %s", topic)

    def _parse_status_push(self, body: str) -> None:
        """解析 thing.status 上下线推送 → 通知 coordinator 实时更新在线状态"""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return
        params = data.get("params")
        if not isinstance(params, dict):
            return
        iot_id = params.get("iotId") or data.get("iotId") or ""
        # 厂商下发的是 {time, value} 结构（value: 1=在线 / 0=离线），和
        # /thing/status/get 轮询拿到的是同一形态 —— 之前这里只认
        # params.status 里的 "online"/"offline" 字符串，于是每条推送都被丢弃，
        # 上下线只能等轮询。两种形态都兼容。
        raw = params.get("status")
        if isinstance(raw, dict):
            raw = raw.get("value")
        status = str(raw if raw is not None else "").lower()
        if not iot_id or status not in ("online", "offline", "1", "0", "true", "false"):
            _LOGGER.debug("忽略上下线推送 iotId=%s status=%s", iot_id, raw)
            return
        online = status in ("online", "1", "true")
        _LOGGER.debug("收到上下线推送 iotId=%s online=%s", iot_id, online)
        if self._on_status_pushed is not None:
            self._call_in_loop(lambda: self._on_status_pushed(iot_id, online))

    def _parse_events_push(self, topic: str, body: str) -> None:
        """解析 thing.events 推送 → 通知 coordinator 写入事件流"""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return
        params = data.get("params")
        if not isinstance(params, dict):
            return
        iot_id = params.get("iotId") or data.get("iotId") or ""
        method = params.get("method") or ""
        # method 形如 thing.event.KeyEvent 或从 topic 推断
        identifier = ""
        if "." in str(method):
            identifier = str(method).rsplit(".", 1)[-1]
        elif "/thing/events" in topic:
            # 兜底：topic 形如 /{pk}/{dn}/thing/events → 用 identifier 字段
            identifier = str(params.get("identifier") or "")
        if not iot_id or not identifier:
            _LOGGER.debug("忽略事件推送 iotId=%s method=%s", iot_id, method)
            return
        # 事件数据：outputData/事件值
        event_data = params.get("outputData") or params.get("value") or {}
        if isinstance(event_data, str):
            try:
                event_data = json.loads(event_data)
            except json.JSONDecodeError:
                event_data = {"raw": event_data}
        info = {
            "identifier": identifier,
            "data": event_data,
            "time": params.get("time") or data.get("time"),
            "iotId": iot_id,
        }
        _LOGGER.debug("收到事件推送 iotId=%s event=%s", iot_id, info)
        if self._on_event_pushed is not None:
            self._call_in_loop(lambda: self._on_event_pushed(iot_id, info))

    def _call_in_loop(self, func) -> None:
        """把回调切回 HA 事件循环执行（paho 回调在后台线程）"""
        loop = self._hass_loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(func)
        except RuntimeError:
            pass  # 事件循环已关闭


def _parse_properties_push(body: str) -> tuple[str, dict] | None:
    """解析 thing.properties 推送 → (iot_id, {Key: {time, value}})

    兼容两种实测见到的 payload 形状：
    - 阿里云标准（slac 实测）：
      {"method":"thing.properties","params":{"iotId":"...","items":{
          "PowerSwitch":{"time":1,"value":"{\\"PowerSwitch\\":1}"}}}}
      此时 items.{key}.value 是 JSON 字符串，反序列化后与 /thing/properties/get
      的 data 结构一致；value 也可能已是 dict。
    - 扁平直推：params 里直接是 {Key: {time, value}}，仅多一个 iotId。
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if data.get("method") != "thing.properties":
        return None
    params = data.get("params") or {}
    if not isinstance(params, dict):
        return None
    iot_id = params.get("iotId") or ""
    if not iot_id:
        return None
    raw_items = params.get("items")
    if not isinstance(raw_items, dict):
        # 扁平形状：去掉 iotId 后剩下的就是属性
        raw_items = {k: v for k, v in params.items() if k != "iotId"}
    if not raw_items:
        return None
    parsed: dict[str, dict] = {}
    for key, val in raw_items.items():
        if isinstance(val, dict) and "value" in val:
            inner = val.get("value")
            if isinstance(inner, str):
                try:
                    decoded = json.loads(inner)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, dict):
                    if len(decoded) == 1 and key in decoded:
                        # value 字符串是 {"PowerSwitch": 1} 这种单键包装
                        inner = decoded[key]
                    else:
                        # value 字符串是完整的 data 映射：逐键规范化后并入
                        for k, v in decoded.items():
                            if isinstance(v, dict):
                                parsed[k] = v if "value" in v else {"value": v}
                            else:
                                parsed[k] = {"value": v}
                        continue
            if isinstance(inner, dict):
                # 保证形状与 properties/get 一致（含 value 键）
                if "value" not in inner:
                    inner = {**val, "value": inner}
                parsed[key] = inner
            else:
                parsed[key] = {**val, "value": inner} if isinstance(val, dict) else {"value": inner}
        elif isinstance(val, dict):
            parsed[key] = val
        else:
            parsed[key] = {"value": val}
    if not parsed:
        return None
    return iot_id, parsed
