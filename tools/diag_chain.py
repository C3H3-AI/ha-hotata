#!/usr/bin/env python3
"""Standalone diagnostic for the Hotata cloud protocol chain.

Reproduces, without Home Assistant, the exact request chain the integration
performs:

    1. vendor account login            saas.keyoo.com/app-api/v2.0/login/password
    2. Aliyun OpenAccount oauth login  sdk.openaccount.aliyun.com/api/prd/loginbyoauth.json
    3. exchange the session for an IoT credential
                                       api.link.aliyun.com/account/createSessionByAuthCode
    4. list the account's devices      /uc/listBindingByAccount
    5. read one device's properties    /thing/properties/get
       (and, with --tsl, its thing model via /thing/tsl/get)

Why it exists: when a login or a poll starts failing in production, this tells
you in five seconds whether the fault is in the integration's code or in the
account/cloud (wrong password, expired registration, 操作过于频繁 rate-limit
penalty). It is also the cross-check that established that this project's
protocol shapes match the other public implementation (chliny/ha-hotata).

Credentials are never read from disk. Pass them as arguments or environment
variables; the password is never printed, and token values are truncated unless
--show-token is given.

Usage
-----
    HOTATA_USERNAME=137xxxxxxxx HOTATA_PASSWORD=... python3 tools/diag_chain.py
    python3 tools/diag_chain.py --username 137xxxxxxxx --password '...' --tsl
    python3 tools/diag_chain.py --iot-id <iot-id> --json out.json

Exit status is 0 only when every step succeeded.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import time
import uuid
from email.utils import formatdate
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - operator feedback
    sys.exit("httpx is required:  pip install httpx")

try:
    from cryptography.hazmat.primitives import hashes, padding, serialization
    from cryptography.hazmat.primitives.asymmetric import (
        padding as asymmetric_padding,
    )
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # pragma: no cover - operator feedback
    sys.exit("cryptography is required:  pip install cryptography")

CONST_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hotata"
    / "const.py"
)


def _load_constants() -> Any:
    """Load const.py by path.

    Importing ``hotata.const`` properly would execute the package __init__,
    which pulls in Home Assistant. const.py itself has no such dependency, so
    load it directly and keep this tool usable on a bare machine.
    """
    if not CONST_PATH.is_file():
        sys.exit(f"cannot find {CONST_PATH}")
    spec = importlib.util.spec_from_file_location("hotata_const", CONST_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover
        sys.exit(f"cannot load {CONST_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


C = _load_constants()

SENSITIVE_KEYS = {
    "accesstoken",
    "authcode",
    "authorization",
    "authtoken",
    "bindsession",
    "clientsecret",
    "devicesecret",
    "identityid",
    "iottoken",
    "iotrefreshtoken",
    "openid",
    "password",
    "refreshtoken",
    "secret",
}


# --------------------------------------------------------------------------- #
# response helpers
# --------------------------------------------------------------------------- #
def _decode_embedded_json(value: Any) -> Any:
    """Decode a value that is JSON carried inside a string."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "[") and text[-1:] in ("}", "]"):
            try:
                return json.loads(text)
            except ValueError:
                return value
    return value


def _unwrap_data(value: Any) -> Any:
    """Unwrap the nested, sometimes JSON-encoded SDK data containers."""
    for _ in range(8):
        value = _decode_embedded_json(value)
        if not isinstance(value, dict):
            return value
        wrapped = next(
            (value[key] for key in ("data", "items", "list") if key in value),
            None,
        )
        if wrapped is None:
            return value
        value = wrapped
    return value


def _find_value(value: Any, *keys: str) -> Any:
    """Depth-first lookup of the first non-empty value among ``keys``."""
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


def _redact_sensitive(value: Any) -> Any:
    """Return a copy with credential-like fields replaced.

    Key names are normalised before comparison (``iot_token``, ``iot-token``
    and ``iotToken`` all match) and JSON hidden inside strings is redacted too,
    because this cloud nests payloads that way.
    """
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            normalised = str(key).lower().replace("_", "").replace("-", "")
            redacted[key] = (
                "<redacted>"
                if normalised in SENSITIVE_KEYS
                else _redact_sensitive(child)
            )
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(child) for child in value]
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        return json.dumps(_redact_sensitive(decoded), ensure_ascii=False)
    return value


def _short(value: Any, keep: int = 10) -> str:
    if value in (None, ""):
        return "NONE"
    text = str(value)
    return text if len(text) <= keep else f"{text[:keep]}…"


# --------------------------------------------------------------------------- #
# request signing
# --------------------------------------------------------------------------- #
def _encrypt_password(password: str) -> str:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(password.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(C.AES_KEY), modes.CBC(C.AES_IV)).encryptor()
    return base64.b64encode(
        encryptor.update(padded) + encryptor.finalize()
    ).decode()


def _account_body(values: dict[str, Any]) -> dict[str, Any]:
    body = {
        **values,
        "appVersion": C.APP_VERSION,
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
        base64.b64decode(C.ACCOUNT_PRIVATE_KEY), password=None
    )
    body["sign"] = base64.b64encode(
        key.sign(plain.encode(), asymmetric_padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    return body


def _open_account_headers(path: str, form: dict[str, str]) -> dict[str, str]:
    headers = {
        "accept": "application/json; charset=utf-8",
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "date": formatdate(usegmt=True),
        "x-ca-key": C.APP_KEY,
        "x-ca-nonce": str(uuid.uuid4()),
        "x-ca-timestamp": str(int(time.time() * 1000)),
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
            C.APP_SECRET.encode(), string_to_sign.encode(), hashlib.sha1
        ).digest()
    ).decode()
    return headers


def _gateway_headers(path: str, body: bytes) -> dict[str, str]:
    content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
    headers = {
        "accept": "application/json",
        "content-md5": content_md5,
        "content-type": "application/octet-stream",
        "date": formatdate(usegmt=True),
        "x-ca-key": C.APP_KEY,
        "x-ca-nonce": str(uuid.uuid4()),
        "x-ca-signaturemethod": "HmacSHA256",
    }
    names = sorted(key for key in headers if key.startswith("x-ca-"))
    canonical = "".join(f"{key}:{headers[key]}\n" for key in names)
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
        + canonical
        + path
    )
    headers["x-ca-signature-headers"] = ",".join(names)
    headers["x-ca-signature"] = base64.b64encode(
        hmac.new(
            C.APP_SECRET.encode(), string_to_sign.encode(), hashlib.sha256
        ).digest()
    ).decode()
    return headers


def _gateway_call(
    client: httpx.Client,
    path: str,
    params: dict[str, Any],
    *,
    token: str | None = None,
    api_version: str = "1.0.2",
) -> tuple[int, dict[str, Any]]:
    request: dict[str, Any] = {
        "apiVer": api_version,
        "language": "zh-CN",
        "appKey": C.APP_KEY,
    }
    if token:
        request["iotToken"] = token
    payload = {
        "id": str(uuid.uuid4()),
        "version": "1.0",
        "params": params,
        "request": request,
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    response = client.post(
        f"https://{C.API_HOST}{path}",
        data=body,
        headers=_gateway_headers(path, body),
    )
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"_body": response.text[:300]}


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def _explain(status: int, payload: dict[str, Any]) -> str:
    """Turn a failure into an operator-facing hint."""
    code = payload.get("code")
    message = str(payload.get("message") or payload.get("msg") or "")
    if status == 403 or "频繁" in message or code in (403, 429):
        return (
            "疑似触发云端风控（操作过于频繁）。这是账号级惩罚，会持续数小时，"
            "不要重试、也不要反复登录。"
        )
    if code in (1005,):
        return "timestamp 失效：本机时钟与服务端偏差过大，检查系统时间。"
    if code in (1007, 1006) or "密码" in message or "账号" in message:
        return "账号/密码被拒或账号被风控，核对凭据或稍后再试。"
    if code == 20050:
        return "缺少 iotId 参数。"
    if code == 29003 or "identityId" in message:
        return "iotToken 过期或未带 identityId：重新跑一次本工具即可。"
    if code == 401 or "token" in message.lower():
        return "授权失效：iotToken/refreshToken 需要重新签发。"
    return ""


def _report(
    label: str,
    status: int,
    payload: dict[str, Any],
    *,
    show_token: bool,
) -> tuple[bool, str]:
    code = payload.get("code")
    ok = status == 200 and code in (None, 200, "000", 0)
    mark = "OK  " if ok else "FAIL"
    detail = f"http={status} code={code}"
    message = payload.get("message") or payload.get("msg")
    if message:
        detail += f" message={message}"
    print(f"  [{mark}] {label}: {detail}")
    if not ok:
        hint = _explain(status, payload)
        if hint:
            print(f"         → {hint}")
    return ok, code  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose the Hotata cloud protocol chain end to end."
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("HOTATA_USERNAME"),
        help="account phone number (or HOTATA_USERNAME)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("HOTATA_PASSWORD"),
        help="account password (or HOTATA_PASSWORD); never echoed",
    )
    parser.add_argument(
        "--iot-id",
        default=None,
        help="device to inspect (default: the first device in the account)",
    )
    parser.add_argument(
        "--tsl",
        action="store_true",
        help="also fetch the device's thing model (TSL)",
    )
    parser.add_argument(
        "--show-token",
        action="store_true",
        help="print token values in full (off by default)",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="write the redacted raw results to this file",
    )
    parser.add_argument(
        "--timeout", type=float, default=25.0, help="per-request timeout"
    )
    args = parser.parse_args()

    if not args.username or not args.password:
        parser.error(
            "credentials required: use --username/--password or "
            "HOTATA_USERNAME/HOTATA_PASSWORD"
        )

    token = "" if args.show_token else "…"
    raw: dict[str, Any] = {}
    failures: list[str] = []
    client = httpx.Client(timeout=args.timeout, verify=False, trust_env=False)

    with client:
        print(f"constants: {CONST_PATH}")
        print(f"account:   {args.username[:3]}****{args.username[-4:]}")

        print("\n[1/5] vendor account login")
        body = _account_body(
            {
                "username": args.username,
                "registeredId": str(uuid.uuid4()),
                "password": _encrypt_password(args.password),
            }
        )
        response = client.post(
            f"https://{C.ACCOUNT_HOST}/app-api/v2.0/login/password", json=body
        )
        payload = response.json()
        raw["login"] = payload
        ok, _ = _report("login/password", response.status_code, payload, show_token=args.show_token)
        if not ok:
            print("\n账号登录就失败了，后面无需继续。")
            return 1
        account = payload.get("data") or {}
        auth_code = _find_value(account, "authCode")
        print(f"        authCode={auth_code if args.show_token else _short(auth_code)}")

        print("\n[2/5] aliyun openaccount oauth login")
        oauth_request = {
            "oauthPlateform": 23,
            "oauthAppKey": C.APP_KEY,
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
        path = "/api/prd/loginbyoauth.json"
        form = {
            "loginByOauthRequest": json.dumps(
                oauth_request, separators=(",", ":"), ensure_ascii=False
            )
        }
        response = client.post(
            f"https://{C.OPEN_ACCOUNT_HOST}{path}",
            headers=_open_account_headers(path, form),
            data=form,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {"_body": response.text[:300]}
        raw["openaccount"] = payload
        session_id = _find_value(payload, "sessionId", "sessionid", "sid")
        _report(
            "loginbyoauth",
            response.status_code,
            {"code": 200 if session_id else None, "message": None},
            show_token=args.show_token,
        )
        if not session_id:
            print("        OpenAccount 未返回 sessionId，后面无需继续。")
            return 1
        print(f"        sessionId={session_id if args.show_token else _short(session_id)}")

        print("\n[3/5] exchange session for an IoT credential")
        status, payload = _gateway_call(
            client,
            "/account/createSessionByAuthCode",
            {
                "request": {
                    "authCode": session_id,
                    "appKey": C.APP_KEY,
                    "accountType": "OA_SESSION",
                }
            },
            api_version="1.0.4",
        )
        raw["credential"] = payload
        iot_token = _find_value(payload, "iotToken")
        ok, _ = _report(
            "createSessionByAuthCode",
            status,
            {"code": payload.get("code"), "message": payload.get("message")},
            show_token=args.show_token,
        )
        if not iot_token or not ok:
            print("        没有拿到 iotToken，后面无需继续。")
            return 1
        print(f"        iotToken={iot_token if args.show_token else _short(iot_token, 12)}")
        print(f"        refreshToken={_short(_find_value(payload, 'refreshToken'), 12)}")
        print(f"        identityId={_short(_find_value(payload, 'identityId', 'identity'), 12)}")

        print("\n[4/5] list bound devices")
        status, payload = _gateway_call(
            client,
            "/uc/listBindingByAccount",
            {"pageNo": 1, "pageSize": 100},
            token=iot_token,
            api_version="1.0.8",
        )
        raw["devices"] = payload
        ok, _ = _report(
            "uc/listBindingByAccount",
            status,
            {"code": payload.get("code"), "message": payload.get("message")},
            show_token=args.show_token,
        )
        devices = _unwrap_data(payload)
        if isinstance(devices, dict):
            devices = devices.get("data") or []
        if not isinstance(devices, list):
            devices = []
        target = args.iot_id
        for item in devices:
            if not isinstance(item, dict):
                continue
            iot_id = item.get("iotId") or item.get("iotid")
            print(
                f"        - {iot_id}  pk={item.get('productKey')}  "
                f"name={item.get('deviceName') or item.get('devicename')}  "
                f"model={item.get('productModel')}  online={item.get('status')}"
            )
            if target is None and iot_id:
                target = iot_id
        if not devices:
            failures.append("设备列表为空：账号下没有已绑定设备？")
        if target is None:
            print("        没有可检查的设备（可用 --iot-id 指定）。")
            _dump(raw, args)
            return 1 if failures else 0
        print(f"        inspecting {target}")

        print("\n[5/5] read reported properties")
        status, payload = _gateway_call(
            client,
            "/thing/properties/get",
            {"iotId": target},
            token=iot_token,
        )
        raw["properties"] = payload
        ok, _ = _report(
            "thing/properties/get",
            status,
            {"code": payload.get("code"), "message": payload.get("message")},
            show_token=args.show_token,
        )
        if ok:
            data = _unwrap_data(payload)
            if not isinstance(data, dict):
                data = {}
            items = data.get("items") if isinstance(data.get("items"), dict) else data
            if isinstance(items, dict):
                print(f"        {len(items)} 项上报。关键能力字段：")
                for key in (
                    "DeviceModelType",
                    "ModelFunctionList",
                    "LightSwitch",
                    "DisinfectionSwitch",
                    "AirDryingSwitch",
                    "DryingSwitch",
                    "IonsSwitch",
                    "Position",
                    "BestPickUpPosition",
                    "BestPickUpPositionSwitch",
                ):
                    if key in items:
                        value = items[key]
                        if isinstance(value, dict) and "value" in value:
                            value = value["value"]
                        print(f"          {key} = {value!r}")
        else:
            failures.append("读取属性失败")

        if args.tsl:
            print("\n[+] thing model (TSL)")
            status, payload = _gateway_call(
                client, "/thing/tsl/get", {"iotId": target}, token=iot_token
            )
            raw["tsl"] = payload
            ok, _ = _report(
                "thing/tsl/get",
                status,
                {"code": payload.get("code"), "message": payload.get("message")},
                show_token=args.show_token,
            )
            tsl = _unwrap_data(payload)
            if ok and isinstance(tsl, dict):
                properties = tsl.get("properties") or []
                print(f"        TSL 声明 {len(properties)} 项属性")

    _dump(raw, args)
    if failures:
        print("\n结论：链路走通但有异常 → " + "；".join(failures))
        return 1
    print("\n结论：整条链路正常（登录 → 凭证 → 设备列表 → 属性读取）。")
    return 0


def _dump(raw: dict[str, Any], args: argparse.Namespace) -> None:
    if not args.json:
        return
    redacted = _redact_sensitive(raw)
    Path(args.json).write_text(
        json.dumps(redacted, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n脱敏后的原始结果已写入 {args.json}")


if __name__ == "__main__":
    sys.exit(main())
