"""Config flow for Tapo RV30.

Auto-detects which transport the device actually speaks:
  - TPAP/SPAKE2+ (newer RV30 Max Plus / RV20 Max Plus firmware), or
  - AES (older/standard scheme — RV30C, RV30C Mop, and others reporting
    encrypt_type: AES during discovery)

and stores the winning transport in the config entry so __init__.py can
instantiate the right client without re-probing on every restart.
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .aes_client import AesVacuumClient
from .aes_client import AuthError as AesAuthError
from .const import CONF_TRANSPORT, DEFAULT_PORT, DOMAIN, TRANSPORT_AES, TRANSPORT_TPAP
from .tpap import AuthError as TpapAuthError
from .tpap import TapoVacuumClient

STEP_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Required(CONF_USERNAME, default=""): str,
    vol.Required(CONF_PASSWORD, default=""): str,
})


async def _test_connection(hass: HomeAssistant, host: str, user: str, pw: str) -> tuple[str | None, str | None]:
    """Try TPAP first, then AES. Returns (transport, error_key).

    transport is None if both failed; error_key is None on success.
    A confirmed wrong-password result short-circuits (no point trying
    the other transport with the same bad credentials).
    """
    def _try_tpap():
        c = TapoVacuumClient(host, user, pw, DEFAULT_PORT)
        c.authenticate()

    try:
        await hass.async_add_executor_job(_try_tpap)
        return TRANSPORT_TPAP, None
    except TpapAuthError:
        return None, "invalid_auth"
    except Exception:
        pass  # fall through and try AES

    def _try_aes():
        c = AesVacuumClient(host, user, pw, DEFAULT_PORT)
        c.authenticate()
        c.close()

    try:
        await hass.async_add_executor_job(_try_aes)
        return TRANSPORT_AES, None
    except AesAuthError:
        return None, "invalid_auth"
    except Exception:
        return None, "cannot_connect"


class TapoRV30ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            user = user_input[CONF_USERNAME].strip()
            pw   = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            transport, err = await _test_connection(self.hass, host, user, pw)
            if err:
                errors["base"] = err
            else:
                return self.async_create_entry(
                    title=f"Tapo RV30 ({host})",
                    data={
                        CONF_HOST: host,
                        CONF_USERNAME: user,
                        CONF_PASSWORD: pw,
                        CONF_TRANSPORT: transport,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_SCHEMA,
            errors=errors,
        )
