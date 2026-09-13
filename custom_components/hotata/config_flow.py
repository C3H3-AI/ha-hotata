"""Config flow for Hotata Airer (username/password login, v4 transport).

One config entry per Hotata account. Credentials are entered once; devices
are discovered dynamically from the Aliyun Link IoT gateway. An optional
backup account enables automatic failover when the cloud rate-limits the
primary account (403 操作过于频繁).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from .api import HotataApi
from .const import (
    CONF_BACKUP_PASSWORD,
    CONF_BACKUP_USERNAME,
    CONF_DESCENT_TIME,
    CONF_IOT_REFRESH_TOKEN,
    CONF_IOT_TOKEN,
    CONF_IDENTITY_ID,
    CONF_REGISTERED_ID,
    DEFAULT_DESCENT_TIME,
    DOMAIN,
)
from .exceptions import (
    HotataAuthError,
    HotataConnectionError,
    HotataError,
    HotataRateLimited,
)

_LOGGER = logging.getLogger(__name__)


def _classify_login_error(message: str) -> tuple[str, str]:
    """Map a server login error to a (error_key, user_facing_message) tuple.

    error_key matches a key in the translations ``config.error`` section so
    the config flow can render a localized message. The raw server message is
    always surfaced to the user via description_placeholders.
    """
    msg = (message or "").strip()

    if any(k in msg for k in ("验证码", "图形验证", "滑块")) or "captcha" in msg.lower():
        return "captcha_required", msg or "登录过于频繁，需要验证码"
    if any(k in msg for k in ("锁定", "冻结", "被封")) or "lock" in msg.lower():
        return "account_locked", msg or "账号已被锁定"
    if any(k in msg for k in ("频繁", "稍后", "稍后再试")) or "rate" in msg.lower():
        return "rate_limited", msg or "操作过于频繁，请稍后再试"
    if any(k in msg for k in ("密码错误", "密码不正确", "账号不存在", "用户不存在")):
        return "invalid_auth", msg or "用户名或密码错误"
    if any(k in msg for k in ("未注册", "不存在")):
        return "phone_not_registered", msg or "该手机号尚未注册"
    if not msg:
        return "invalid_auth", "用户名或密码错误"
    # Fallback: surface the raw server message.
    return "server_error", msg


class HotataAirerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Hotata Airer config flow."""

    VERSION = 4

    def __init__(self) -> None:
        """Initialize the flow."""
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    async def _async_login(
        hass, username: str, password: str
    ) -> dict[str, Any]:
        """Login and return the entry-data payload with IoT credentials.

        Raises :class:`HotataLoginError` with (error_key, server_msg) on
        failure so both steps can render a localized message.
        """
        from homeassistant.helpers.aiohttp_client import (
            async_get_clientsession,
        )

        api = HotataApi(
            async_get_clientsession(hass),
            username=username.strip(),
            password=password,
        )
        try:
            await api.async_login()
        except HotataAuthError as err:
            error_key, server_msg = _classify_login_error(str(err))
            raise HotataLoginError(error_key, server_msg) from err
        except HotataRateLimited as err:
            raise HotataLoginError("rate_limited", str(err)) from err
        except HotataConnectionError as err:
            raise HotataLoginError("network_error", str(err)) from err
        except HotataError as err:
            raise HotataLoginError("server_error", str(err)) from err
        return {
            CONF_IOT_TOKEN: api.iot_token,
            CONF_IOT_REFRESH_TOKEN: api.iot_refresh_token,
            CONF_IDENTITY_ID: api.identity_id,
            CONF_REGISTERED_ID: api.registered_id,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: authenticate with username/password."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] | None = None

        if user_input is not None:
            try:
                creds = await self._async_login(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except HotataLoginError as err:
                errors["base"] = err.error_key
                placeholders = {"server_message": err.server_message}
            else:
                await self.async_set_unique_id(
                    f"{user_input[CONF_USERNAME].strip()}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Hotata ({user_input[CONF_USERNAME].strip()})",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_DESCENT_TIME: int(
                            user_input.get(
                                CONF_DESCENT_TIME, DEFAULT_DESCENT_TIME
                            )
                        ),
                        CONF_BACKUP_USERNAME: user_input.get(
                            CONF_BACKUP_USERNAME, ""
                        ).strip(),
                        CONF_BACKUP_PASSWORD: user_input.get(
                            CONF_BACKUP_PASSWORD, ""
                        ),
                        **creds,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(
                    CONF_DESCENT_TIME, default=DEFAULT_DESCENT_TIME
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=20)),
                vol.Optional(CONF_BACKUP_USERNAME): str,
                vol.Optional(CONF_BACKUP_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure: update credentials and/or the backup account."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] | None = None

        if user_input is not None:
            try:
                creds = await self._async_login(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except HotataLoginError as err:
                errors["base"] = err.error_key
                placeholders = {"server_message": err.server_message}
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_BACKUP_USERNAME: user_input.get(
                            CONF_BACKUP_USERNAME, ""
                        ).strip(),
                        CONF_BACKUP_PASSWORD: user_input.get(
                            CONF_BACKUP_PASSWORD, ""
                        )
                        or entry.data.get(CONF_BACKUP_PASSWORD, ""),
                        **creds,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME,
                    default=entry.data.get(CONF_USERNAME, ""),
                ): str,
                # Password is intentionally blank by default — even if we
                # have one stored, we ask the user to re-confirm so they
                # can change it if needed.
                vol.Required(CONF_PASSWORD, default=""): str,
                vol.Optional(
                    CONF_BACKUP_USERNAME,
                    description={
                        "suggested_value": entry.data.get(
                            CONF_BACKUP_USERNAME, ""
                        )
                    },
                ): str,
                vol.Optional(CONF_BACKUP_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )


class HotataLoginError(Exception):
    """Carries (translation error key, raw server message) to the flow."""

    def __init__(self, error_key: str, server_message: str) -> None:
        super().__init__(error_key)
        self.error_key = error_key
        self.server_message = server_message
