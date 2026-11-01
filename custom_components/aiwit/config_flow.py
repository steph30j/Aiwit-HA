"""Config flow for Aiwit / EKEN."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import AiwitClient
from .const import (
    CONF_LOGIN_SIGN_KEY,
    CONF_SOCKET_KEY,
    CONF_THUMBNAIL_PATH,
    CONF_UDID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_LOGIN_SIGN_KEY): str,
        vol.Required(CONF_SOCKET_KEY): str,
        vol.Optional(CONF_UDID): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate credentials by attempting REST login."""
    udid = data.get(CONF_UDID) or str(uuid.uuid4())
    client = AiwitClient(
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        udid=udid,
        login_sign_key=(data.get(CONF_LOGIN_SIGN_KEY) or "").strip(),
        socket_key=(data.get(CONF_SOCKET_KEY) or "").strip(),
    )

    def _try_login() -> dict:
        return client.login()

    try:
        payload = await hass.async_add_executor_job(_try_login)
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Login failed")
        raise CannotConnect from err

    if payload.get("resultCode") != 0:
        raise InvalidAuth

    return {
        "title": f"Aiwit ({data[CONF_USERNAME]})",
        CONF_UDID: udid,
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Aiwit."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_configured()
                data = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_LOGIN_SIGN_KEY: user_input[CONF_LOGIN_SIGN_KEY].strip(),
                    CONF_SOCKET_KEY: user_input[CONF_SOCKET_KEY].strip(),
                    CONF_UDID: info[CONF_UDID],
                }
                return self.async_create_entry(title=info["title"], data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Entry without client keys (created before 0.7.12): ask for them."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            new_data = {
                **entry.data,
                CONF_LOGIN_SIGN_KEY: user_input[CONF_LOGIN_SIGN_KEY].strip(),
                CONF_SOCKET_KEY: user_input[CONF_SOCKET_KEY].strip(),
            }
            try:
                await validate_input(self.hass, new_data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOGIN_SIGN_KEY): str,
                    vol.Required(CONF_SOCKET_KEY): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    """Options: credentials (data) + thumbnail output directory (options).

    Password may be left blank to keep the current one.
    Thumbnail path empty = default <config>/www/aiwit.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        errors: dict[str, str] = {}
        entry = self.config_entry

        current_username = entry.data.get(CONF_USERNAME, "")
        current_password = entry.data.get(CONF_PASSWORD, "")
        current_udid = entry.data.get(CONF_UDID)
        current_login_key = entry.data.get(CONF_LOGIN_SIGN_KEY, "")
        current_socket_key = entry.data.get(CONF_SOCKET_KEY, "")
        current_thumb = (
            entry.options.get(CONF_THUMBNAIL_PATH)
            or entry.data.get(CONF_THUMBNAIL_PATH)
            or ""
        )

        if user_input is not None:
            new_username = (user_input.get(CONF_USERNAME) or "").strip()
            new_password = user_input.get(CONF_PASSWORD) or ""
            new_thumb = (user_input.get(CONF_THUMBNAIL_PATH) or "").strip()
            new_login_key = (
                user_input.get(CONF_LOGIN_SIGN_KEY) or ""
            ).strip() or current_login_key
            new_socket_key = (
                user_input.get(CONF_SOCKET_KEY) or ""
            ).strip() or current_socket_key

            # Leave password blank = keep the existing one
            if not new_password:
                new_password = current_password

            username_changed = new_username.lower() != (current_username or "").lower()
            password_changed = new_password != current_password
            thumb_changed = new_thumb != (current_thumb or "")
            keys_changed = (
                new_login_key != current_login_key
                or new_socket_key != current_socket_key
            )

            # Nothing changed: close without reload
            if (
                not username_changed
                and not password_changed
                and not keys_changed
                and not thumb_changed
            ):
                return self.async_create_entry(title="", data={})

            # Validate credentials only if login/password changed
            need_auth_check = username_changed or password_changed or keys_changed
            info_title = entry.title
            if need_auth_check:
                data_to_validate = {
                    CONF_USERNAME: new_username,
                    CONF_PASSWORD: new_password,
                    CONF_LOGIN_SIGN_KEY: new_login_key,
                    CONF_SOCKET_KEY: new_socket_key,
                    CONF_UDID: current_udid,
                }
                try:
                    info = await validate_input(self.hass, data_to_validate)
                    info_title = info["title"]
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Unexpected exception during options validation")
                    errors["base"] = "unknown"

            if not errors:
                if need_auth_check:
                    new_data = {
                        CONF_USERNAME: new_username,
                        CONF_PASSWORD: new_password,
                        CONF_LOGIN_SIGN_KEY: new_login_key,
                        CONF_SOCKET_KEY: new_socket_key,
                        CONF_UDID: current_udid,
                    }
                    new_unique = new_username.lower()
                    update_kwargs: dict[str, Any] = {
                        "data": new_data,
                        "title": info_title,
                    }
                    if entry.unique_id != new_unique:
                        update_kwargs["unique_id"] = new_unique
                    self.hass.config_entries.async_update_entry(entry, **update_kwargs)

                # Thumbnail path lives in options (not secrets)
                new_options = {**entry.options, CONF_THUMBNAIL_PATH: new_thumb}
                self.hass.config_entries.async_update_entry(entry, options=new_options)

                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(title="", data={})

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=current_username): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.TEXT, autocomplete="username"
                    )
                ),
                vol.Optional(CONF_PASSWORD, default=""): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
                vol.Optional(
                    CONF_LOGIN_SIGN_KEY, default=current_login_key
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(
                    CONF_SOCKET_KEY, default=current_socket_key
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(CONF_THUMBNAIL_PATH, default=current_thumb): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
