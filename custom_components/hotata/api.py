"""Async client for the cloud APIs behind 好太太智联 3.5.8.

Two channels are chained during login: the vendor account service hands out an
authCode, which is exchanged for an Aliyun IoT credential. Every later call
(device list, properties, services, thing models) goes to the Aliyun Link IoT
API Gateway and is signed with HmacSHA1/HmacSHA256 plus a content MD5.

Rate limiting is treated as its own failure class: the cloud answers HTTP/app
403 (操作过于频繁) with a long-lived account penalty, so those raise
:class:`HotataRateLimited` and are never retried here — the account layer goes
silent or fails over instead. No device or account credentials are logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from email.utils import formatdate
from typing import Any

from aiohttp import ClientError, ClientSession
from cryptography.hazmat.primitives import hashes, padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import (
    ACCOUNT_HOST,
    API_HOST,
    APP_KEY,
    APP_SECRET,
    APP_VERSION,
    OPEN_ACCOUNT_HOST,
    READ_ONLY_QUERIES,
)
from .exceptions import (
    HotataAuthError,
    HotataConnectionError,
    HotataError,
    HotataRateLimited,
)
from .models import HotataDevice

_LOGGER = logging.getLogger(__name__)

# This is the production request-signing key shipped in version 3.5.8 of the
# public Android application.  It is an application protocol constant, not a
# user's credential.
_ACCOUNT_PRIVATE_KEY = (
    "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCXAsmTBgCKxOZ3"
    "okMNkjw9h6X2BD5CJ8sQhNBGBoTEUf3USNbnLiN9gpYLCziK50M5BsOAIADqxbsN"
    "/K7cYwNMtoKFKiTqTajM4tAJ3LKL1MlpEZM7uPjS1EKi9WNXalnfaI+9VrnuHXiA"
    "Zc9idZdx4oxeD4PwKHjKzqIFNHC9WrvoofUabZkzrfSjygiJKUSeWGHtyPB/YC+r"
    "t1lGFGMZcFY5BX4ww1EquWeulzoWQcOsKwjUDmU5KM5HwUt1z7fFtN1XXM4tTAow"
    "Z08mmMorDQso9icMX0jCbraRX0HL9q6eK8jjeFFhcMYDX2rcM2+8X9Zd/56SRGIm"
    "jP+sdCs7AgMBAAECggEAHb4izZ5lBO/7JJ0E7+tZihTpjycOzCDiUgKWsvQduj0b"
    "7W/bQ/VGcDYEL3CqVlFuYBEA+H9VLuh7Cyo1lpq5z6Yy1t+SHcPl91TE/OxHDlt+"
    "v/8CLMUl3QCJj2cdhd4gjWwew4ANZuTPExr6Wb4ncfrZAr2zkt2lzOwd5UCK5ABp"
    "dKNozwC+Gpt7RV5nFw8dqL1ODH7q6zGVEEQWA9WG9LV6zrv1dfuP4X1X6xl1USdc"
    "WZbJql7Zw1acXC7DSpmd4pqRhp0Dn0iL8x3fRMgXMD1aEc7aTRqgkR02y8CdlX9v"
    "CpW6GYSImn3YbngL/GTzZIEaxnM/ejnd57iaEJ56wQKBgQDNKJUdMOSbpQ1SKMyx"
    "iCvophlF40r1kLPQ+JrIM2V7RuTSxUUJhk9xI5l9+RdpvRb4bhMvBsCwy+vHcDk4"
    "bhv55snkF0+R2wz2UsnvgIPOx8Aju4ojkfgOdW0pQh4sQGykbWjEQ767q7vfhHVC"
    "eHHpylT2RVLkcapLiy8RK+VpIwKBgQC8bwlS/E5DiidNBkLTDEpLtbVqYkq+hRME"
    "yAg2ep2OX1kl6sWwC8Vj2sEqCY/9MplZ3DdcHocjNU2IkksUWgeBVk0ushQsIcWj"
    "OQv+GUhjiuAs28CoP64dvzT1xNV8PIF2HpRv+SheHkuSFOtg3UWc7CmC5Ea/uwgy"
    "V1SxoaCzCQKBgQCvG0FSvgWRt2nMQ1ia+tAHbaXKmfrD6DMiXN63m+61LshmAcww"
    "Gfw6ZBlBhVbvgF5XwpQLImdbP2JKQsYEHS8xuEN/tEnNAztoDzeefYGC/8lGdm6s"
    "d41SwfVfLrjUKlTQbzXptqzYP/dGCyeOiYEo+/JSlM7wfvfMLMsKi/3uIwKBgQCP"
    "myfGANc8jdtpzi27XhB5JqB91S8Vh6F48WGg802EJZJxXT0P78idUygHe4Yq9xb7"
    "7uKZ6AIhiQvv214wwnQZ08W6oqjRAWP4Aw/qtSYABuTWCxwGnZF6xi/8Zeg1aH9Z"
    "n/CMbZygLgJ18E96YOgesbTpNkPc9xNGGlxHi+BG0QKBgC5CtDrJrzqBNlxjRBM9"
    "gKF3b/T2HotQEDOB5V6uwgWUq0m2E2XOPMFe7Qw2jp2Ki+a8Utz+6DRfcpAeFM+D"
    "h0nf8Ue1UxPTYHPPN4pfKdODpcTNn0XIhQS6OwmD5sUApF3D1ew1K1cECU1bjlT2"
    "F1Sws4xEH+OMGrhQadNNtG1z"
)

_AES_KEY = b"SnqUuPDWy5wusGG7"
_AES_IV = b"tvGjXli9WjpfOmNK"
_SENSITIVE_RESPONSE_KEYS = {
    "accesstoken",
    "authcode",
    "devicekey",
    "devicesecret",
    "password",
    "refreshtoken",
    "sessionid",
    "token",
}


def _find_value(value: Any, *keys: str) -> Any:
    """Recursively locate the first named value in a JSON response."""
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) not in (None, ""):
                return value[key]
        for child in value.values():
            found = _find_value(child, *keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_value(child, *keys)
            if found not in (None, ""):
                return found
    return None


def _is_success_code(value: Any) -> bool:
    """Return whether an account response contains a zero success code."""
    code = str(value)
    return bool(code) and not code.strip("0")


def _redact_sensitive(value: Any) -> Any:
    """Return a copy with credential-like response fields redacted."""
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if str(key).lower() in _SENSITIVE_RESPONSE_KEYS
                else _redact_sensitive(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(child) for child in value]
    return value


def _decode_embedded_json(value: Any) -> Any:
    """Decode a JSON-encoded response field with a domain error."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError) as err:
        raise HotataError("Unexpected JSON-encoded cloud response") from err


def _is_rate_limited(status: int | None, result: Any) -> bool:
    """Detect the cloud's 403 操作过于频繁 throttle in any of its shapes."""
    if status == 403:
        return True
    if not isinstance(result, dict):
        return False
    code = str(result.get("code", ""))
    if code == "403":
        return True
    message = str(result.get("message") or result.get("msg") or "")
    return "操作过于频繁" in message or "过于频繁" in message


class HotataApi:
    """Client for account, OpenAccount, and Link IoT API Gateway calls."""

    def __init__(
        self,
        session: ClientSession,
        *,
        username: str | None = None,
        password: str | None = None,
        iot_token: str | None = None,
        iot_refresh_token: str | None = None,
        identity_id: str | None = None,
        registered_id: str | None = None,
    ) -> None:
        self._session = session
        self.username = username
        self.password = password
        self.iot_token = iot_token
        self.iot_refresh_token = iot_refresh_token
        self.identity_id = identity_id
        self.registered_id = registered_id or str(uuid.uuid4())

    @staticmethod
    def _encrypt_password(password: str) -> str:
        padder = padding.PKCS7(128).padder()
        padded = padder.update(password.encode()) + padder.finalize()
        encryptor = Cipher(
            algorithms.AES(_AES_KEY), modes.CBC(_AES_IV)
        ).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(encrypted).decode()

    @staticmethod
    def _account_body(values: dict[str, Any]) -> dict[str, Any]:
        body = {
            **values,
            "appVersion": APP_VERSION,
            "sysVersion": "android_15",
            "traceId": str(uuid.uuid4()),
            "imei": str(uuid.uuid4()),
            "phoneModel": "Home Assistant",
            "timestamp": int(time.time() * 1000),
        }
        plain = "&".join(
            f"{key}={value}"
            for key, value in sorted(body.items())
            if value is not None and not isinstance(value, (list, dict))
        )
        key = serialization.load_der_private_key(
            base64.b64decode(_ACCOUNT_PRIVATE_KEY), password=None
        )
        body["sign"] = base64.b64encode(
            key.sign(plain.encode(), asymmetric_padding.PKCS1v15(), hashes.SHA256())
        ).decode()
        return body

    async def async_login(self) -> None:
        """Exchange app credentials for a renewable Aliyun IoT credential."""
        if not self.username or not self.password:
            raise HotataAuthError("Missing username or password")
        try:
            async with self._session.post(
                f"https://{ACCOUNT_HOST}/app-api/v2.0/login/password",
                json=self._account_body(
                    {
                        "username": self.username,
                        "registeredId": self.registered_id,
                        "password": self._encrypt_password(self.password),
                    }
                ),
            ) as response:
                payload = await response.json(content_type=None)
            if _is_rate_limited(response.status, payload):
                raise HotataRateLimited(
                    str(payload.get("message") or "操作过于频繁")
                )
            if response.status != 200 or not _is_success_code(
                payload.get("code")
            ):
                raise HotataAuthError(
                    payload.get("message") or "Account login failed"
                )
            auth_code = _find_value(payload.get("data"), "authCode")
            if not auth_code:
                raise HotataAuthError("Account response has no authCode")
            session_id = await self._async_open_account_login(str(auth_code))
            credential = await self._async_create_iot_credential(session_id)
            self.iot_token = str(credential["iotToken"])
            self.iot_refresh_token = str(credential.get("refreshToken") or "")
            self.identity_id = str(
                credential.get("identityId") or credential.get("identity") or ""
            )
        except HotataError:
            raise
        except (ClientError, TimeoutError, ValueError, TypeError) as err:
            raise HotataConnectionError(str(err)) from err

    async def _async_open_account_login(self, auth_code: str) -> str:
        request = {
            "oauthPlateform": 23,
            "oauthAppKey": APP_KEY,
            "authCode": auth_code,
            "riskControlInfo": {
                "platformName": "android",
                "platformVersion": "15",
                "appVersion": "53",
                "sdkVersion": "3.4.2",
                "locale": "zh_CN",
                "netType": "wifi",
                "USE_OA_PWD_ENCRYPT": "true",
                "USE_H5_NC": "true",
                "packageName": "com.hotata.keyoolot",
            },
        }
        form = {
            "loginByOauthRequest": json.dumps(
                request, separators=(",", ":"), ensure_ascii=False
            )
        }
        path = "/api/prd/loginbyoauth.json"
        headers = self._open_account_headers(path, form)
        async with self._session.post(
            f"https://{OPEN_ACCOUNT_HOST}{path}", headers=headers, data=form
        ) as response:
            payload = await response.json(content_type=None)
        if response.status != 200:
            raise HotataAuthError("OpenAccount login failed")
        session_id = _find_value(payload, "sessionId", "sessionid", "sid")
        if not session_id:
            raise HotataAuthError(
                _find_value(payload, "message", "msg") or "No OpenAccount session"
            )
        return str(session_id)

    @staticmethod
    def _open_account_headers(
        path: str, form: dict[str, str]
    ) -> dict[str, str]:
        now = str(int(time.time() * 1000))
        headers = {
            "accept": "application/json; charset=utf-8",
            "content-type": "application/x-www-form-urlencoded; charset=utf-8",
            "date": formatdate(usegmt=True),
            "x-ca-key": APP_KEY,
            "x-ca-nonce": str(uuid.uuid4()),
            "x-ca-timestamp": now,
            "x-ca-signature-method": "HmacSHA1",
            "CA_VERSION": "1",
            "user-agent": "ALIYUN-ANDROID-DEMO",
        }
        names = sorted(key for key in headers if key.startswith("x-ca-"))
        resource = path + "?" + "&".join(
            f"{key}={value}" for key, value in sorted(form.items())
        )
        string_to_sign = (
            "POST\n"
            + headers["accept"]
            + "\n\n"
            + headers["content-type"]
            + "\n"
            + headers["date"]
            + "\n"
            + "".join(f"{key}:{headers[key]}\n" for key in names)
            + resource
        )
        headers["x-ca-signature-headers"] = ",".join(names)
        headers["x-ca-signature"] = base64.b64encode(
            hmac.new(
                APP_SECRET.encode(), string_to_sign.encode(), hashlib.sha1
            ).digest()
        ).decode()
        return headers

    async def _async_create_iot_credential(
        self, session_id: str
    ) -> dict[str, Any]:
        payload = await self._async_gateway_call(
            "/account/createSessionByAuthCode",
            {
                "request": {
                    "authCode": session_id,
                    "appKey": APP_KEY,
                    "accountType": "OA_SESSION",
                }
            },
            api_version="1.0.4",
            include_token=False,
        )
        token = _find_value(payload, "iotToken")
        if not token:
            raise HotataAuthError("IoT credential creation failed")
        return {
            "iotToken": token,
            "refreshToken": _find_value(payload, "refreshToken"),
            "identityId": _find_value(payload, "identityId", "identity"),
        }

    async def async_refresh_iot_credential(self) -> None:
        """Refresh an existing IoT token, falling back to full login."""
        if self.iot_refresh_token and self.identity_id:
            try:
                payload = await self._async_gateway_call(
                    "/account/checkOrRefreshSession",
                    {
                        "request": {
                            "refreshToken": self.iot_refresh_token,
                            "identityId": self.identity_id,
                        }
                    },
                    api_version="1.0.4",
                    include_token=False,
                )
                token = _find_value(payload, "iotToken")
                if token:
                    self.iot_token = str(token)
                    self.iot_refresh_token = str(
                        _find_value(payload, "refreshToken")
                        or self.iot_refresh_token
                    )
                    return
            except HotataError:
                pass
        await self.async_login()

    @staticmethod
    def _gateway_headers(path: str, body: bytes) -> dict[str, str]:
        content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        headers = {
            "accept": "application/json",
            "content-md5": content_md5,
            "content-type": "application/octet-stream",
            "date": formatdate(usegmt=True),
            "x-ca-key": APP_KEY,
            "x-ca-nonce": str(uuid.uuid4()),
            "x-ca-signaturemethod": "HmacSHA256",
        }
        signed_names = sorted(
            key for key in headers if key.startswith("x-ca-")
        )
        canonical_headers = "".join(
            f"{key}:{headers[key]}\n" for key in signed_names
        )
        string_to_sign = (
            "POST\n"
            + headers["accept"]
            + "\n"
            + content_md5
            + "\n"
            + headers["content-type"]
            + "\n"
            + headers["date"]
            + "\n"
            + canonical_headers
            + path
        )
        headers["x-ca-signature-headers"] = ",".join(signed_names)
        headers["x-ca-signature"] = base64.b64encode(
            hmac.new(
                APP_SECRET.encode(),
                string_to_sign.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        return headers

    async def _async_gateway_call(
        self,
        path: str,
        params: dict[str, Any],
        *,
        api_version: str = "1.0.2",
        include_token: bool = True,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        if include_token and not self.iot_token:
            await self.async_login()
        request: dict[str, Any] = {
            "apiVer": api_version,
            "language": "zh-CN",
            "appKey": APP_KEY,
        }
        if include_token:
            request["iotToken"] = self.iot_token
        payload = {
            "id": str(uuid.uuid4()),
            "version": "1.0",
            "params": params,
            "request": request,
        }
        body = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False
        ).encode()
        try:
            async with self._session.post(
                f"https://{API_HOST}{path}",
                data=body,
                headers=self._gateway_headers(path, body),
            ) as response:
                result = await response.json(content_type=None)
                status = response.status
        except (ClientError, TimeoutError, ValueError) as err:
            raise HotataConnectionError(str(err)) from err
        # The 403 throttle (操作过于频繁) carries a long-lived account
        # penalty: never retry, never treat it as an auth failure.
        if _is_rate_limited(status, result):
            raise HotataRateLimited(
                str(_find_value(result, "message", "msg") or "操作过于频繁")
            )
        code = result.get("code") if isinstance(result, dict) else None
        if status in (401, 460) or code in (401, 460, 20001):
            if include_token and retry_auth:
                await self.async_refresh_iot_credential()
                return await self._async_gateway_call(
                    path,
                    params,
                    api_version=api_version,
                    retry_auth=False,
                )
            raise HotataAuthError("IoT token rejected")
        if status != 200 or code not in (None, 200):
            message = (
                _find_value(result, "message", "msg")
                if isinstance(result, (dict, list))
                else None
            )
            raise HotataError(str(message or f"Cloud error {code}"))
        return result

    async def async_list_devices(self) -> list[HotataDevice]:
        """Return all devices bound to the account."""
        result = await self._async_gateway_call(
            "/uc/listBindingByAccount",
            {"pageNo": 1, "pageSize": 100},
            api_version="1.0.8",
        )
        data = result.get("data", result)
        if isinstance(data, dict):
            data = data.get("data") or data.get("items") or data.get("list") or []
        data = _decode_embedded_json(data)
        if not isinstance(data, list):
            raise HotataError("Unexpected device-list response")
        return [
            HotataDevice.from_api(item)
            for item in data
            if isinstance(item, dict)
            and (item.get("iotId") or item.get("iotid"))
        ]

    async def async_get_properties(self, iot_id: str) -> dict[str, Any]:
        """Read all reported properties for one device."""
        result = await self._async_gateway_call(
            "/thing/properties/get", {"iotId": iot_id}
        )
        data = result.get("data", result)
        data = _decode_embedded_json(data)
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data["items"]
        if isinstance(data, dict):
            return data
        return {}

    async def async_get_thing_model(self, iot_id: str) -> dict[str, Any]:
        """Return the TSL model used to validate a device's capabilities."""
        result = await self._async_gateway_call(
            "/thing/tsl/get", {"iotId": iot_id}
        )
        data = result.get("data", result)
        data = _decode_embedded_json(data)
        # "schema" is normally the TSL JSON-schema URL (a plain string), not
        # embedded JSON, so only follow it when it carries a nested payload.
        if isinstance(data, dict) and isinstance(data.get("schema"), (dict, list)):
            schema = _decode_embedded_json(data["schema"])
            if isinstance(schema, dict):
                data = schema
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        return data if isinstance(data, dict) else {}

    async def async_list_subdevices(
        self, gateway_iot_id: str
    ) -> list[HotataDevice]:
        """Return devices below one gateway."""
        result = await self._async_gateway_call(
            "/subdevices/list",
            {"iotId": gateway_iot_id, "pageNo": 1, "pageSize": 1000},
        )
        data = result.get("data", result)
        data = _decode_embedded_json(data)
        if isinstance(data, dict):
            data = data.get("data") or data.get("items") or data.get("list") or []
        if not isinstance(data, list):
            return []
        devices = []
        for item in data:
            if not isinstance(item, dict):
                continue
            device = HotataDevice.from_api(item)
            if not device.iot_id:
                continue
            device.parent_iot_id = gateway_iot_id
            devices.append(device)
        return devices

    async def async_get_online(self, iot_id: str) -> bool | None:
        """Return device connectivity."""
        result = await self._async_gateway_call(
            "/thing/status/get", {"iotId": iot_id}
        )
        status = _find_value(result.get("data", result), "status")
        if status is None:
            return None
        return str(status).lower() in ("1", "true", "online")

    async def async_get_latest_event(
        self, iot_id: str, identifier: str
    ) -> dict[str, Any] | None:
        """Return the newest timeline event using the APK request shape."""
        result = await self._async_gateway_call(
            "/thing/event/timeline/get",
            {
                "iotId": iot_id,
                "pageSize": 1,
                "start": 0,
                "end": int(time.time() * 1000) + 2000,
                "ordered": "false",
                "identifier": identifier,
                "eventType": "info",
            },
        )
        data = result.get("data", result)
        data = _decode_embedded_json(data)
        if isinstance(data, dict):
            data = data.get("items") or []
        if not isinstance(data, list) or not data:
            return None
        return data[0] if isinstance(data[0], dict) else {"value": data[0]}

    async def async_query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run one allowlisted read-only API query."""
        try:
            path, api_version = READ_ONLY_QUERIES[query]
        except KeyError as err:
            raise ValueError(f"Unsupported read-only query: {query}") from err
        return _redact_sensitive(
            await self._async_gateway_call(
                path,
                params or {},
                api_version=api_version,
            )
        )

    async def async_set_property(
        self, iot_id: str, identifier: str, value: Any
    ) -> None:
        """Set one property using the standard Link IoT API."""
        await self.async_set_properties(iot_id, {identifier: value})

    async def async_set_properties(
        self, iot_id: str, items: dict[str, Any]
    ) -> None:
        """Set multiple properties in one atomic Link IoT request."""
        await self._async_gateway_call(
            "/thing/properties/set",
            {"iotId": iot_id, "items": items},
        )

    async def async_invoke_service(
        self, iot_id: str, identifier: str, args: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Invoke a thing-model service."""
        return await self._async_gateway_call(
            "/thing/service/invoke",
            {
                "iotId": iot_id,
                "identifier": identifier,
                "args": args or {},
            },
        )
