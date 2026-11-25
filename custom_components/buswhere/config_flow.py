"""Config flow for BusWhere integration."""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .auth import BusWhereAuth, CannotConnect, InvalidAuth, MissingSecret
from .const import (
    BASE_URL,
    CONF_CREATE_STOP_ZONES,
    CONF_ORG,
    CONF_PASSWORD_SECRET,
    CONF_ROUTE_ID,
    CONF_ROUTE_URL,
    CONF_SCAN_INTERVAL,
    CONF_STOP_NAMES,
    CONF_USERNAME,
    CONF_ZONE_RADIUS,
    DEFAULT_CREATE_STOP_ZONES,
    DEFAULT_PASSWORD_SECRET,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_ZONE_RADIUS,
    DOMAIN,
    USER_AGENT,
)
from .coordinator import BusWhereCoordinator

_LOGGER = logging.getLogger(__name__)

URL_PATTERN = re.compile(
    r"https?://buswhere\.com/([^/]+)/routes/([^/?#]+)"
)


class BusWhereConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BusWhere."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._org: str = ""
        self._route_id: str = ""
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL
        self._username: str = ""
        self._password_secret: str = ""
        self._stops: list[dict[str, Any]] = []
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step — enter route URL and optional credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url = user_input[CONF_ROUTE_URL].strip().rstrip("/")
            username = user_input.get(CONF_USERNAME, "").strip()
            password_secret = user_input.get(CONF_PASSWORD_SECRET, "").strip()
            match = URL_PATTERN.match(url)
            if not match:
                errors["base"] = "invalid_url"
            elif username and not password_secret:
                errors["base"] = "secret_required"
            else:
                org = match.group(1)
                route_id = match.group(2)

                await self.async_set_unique_id(f"{org}_{route_id}")
                self._abort_if_unique_id_configured()

                stops, error = await self._async_validate(
                    org, route_id, username, password_secret
                )
                if error:
                    errors["base"] = error
                else:
                    self._org = org
                    self._route_id = route_id
                    self._username = username
                    self._password_secret = password_secret
                    self._scan_interval = int(
                        user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                    )
                    self._stops = stops or []
                    return await self.async_step_stops()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ROUTE_URL): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=10,
                            max=300,
                            step=5,
                            unit_of_measurement="seconds",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(CONF_USERNAME): str,
                    vol.Optional(
                        CONF_PASSWORD_SECRET,
                        description={"suggested_value": DEFAULT_PASSWORD_SECRET},
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_stops(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the stop naming step — optionally rename each stop."""
        if not self._stops:
            # No stops available; skip straight to creating the entry
            return self._create_entry({})

        if user_input is not None:
            stop_names: dict[str, str] = {}
            for key, value in user_input.items():
                if key.startswith("stop_order_") and value:
                    order = key[len("stop_order_"):]
                    stop_names[order] = value.strip()
            return self._create_entry(stop_names)

        schema_dict: dict[Any, Any] = {}
        for stop in sorted(self._stops, key=lambda s: s.get("order", 0)):
            order = str(stop.get("order", stop["id"]))
            default_name = stop.get("address") or f"Stop {order}"
            schema_dict[
                vol.Optional(
                    f"stop_order_{order}",
                    description={"suggested_value": default_name},
                )
            ] = TextSelector(TextSelectorConfig())

        return self.async_show_form(
            step_id="stops",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "stop_count": str(len(self._stops)),
            },
        )

    def _create_entry(self, stop_names: dict[str, str]) -> FlowResult:
        """Create the config entry with optional initial stop names."""
        title = f"BusWhere {self._route_id.replace('_', ' ').title()}"
        data: dict[str, Any] = {
            CONF_ORG: self._org,
            CONF_ROUTE_ID: self._route_id,
            CONF_SCAN_INTERVAL: self._scan_interval,
        }
        if self._username:
            data[CONF_USERNAME] = self._username
            data[CONF_PASSWORD_SECRET] = self._password_secret
        return self.async_create_entry(
            title=title,
            data=data,
            options={CONF_STOP_NAMES: stop_names},
        )

    async def _async_validate(
        self,
        org: str,
        route_id: str,
        username: str,
        password_secret: str,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Validate the route (and credentials, if given).

        Returns (stops, None) on success or (None, error_key) on failure.
        """
        session = async_create_clientsession(
            self.hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar()
        )
        try:
            if username:
                auth = BusWhereAuth(self.hass, username, password_secret)
                try:
                    await auth.async_login(session)
                except InvalidAuth:
                    return None, "invalid_auth"
                except MissingSecret:
                    return None, "missing_secret"
                except CannotConnect:
                    return None, "cannot_connect"

            url = f"{BASE_URL}/{org}/routes/{route_id}"
            try:
                async with session.get(
                    url, headers={"User-Agent": USER_AGENT}
                ) as resp:
                    if resp.status != 200:
                        return None, "cannot_connect"
                    html = await resp.text()
            except aiohttp.ClientError:
                _LOGGER.debug("BusWhere route fetch failed during setup", exc_info=True)
                return None, "cannot_connect"

            if "Maps.data" not in html:
                # Still seeing a non-route page: almost certainly not authorised.
                return None, "invalid_auth" if username else "cannot_connect"

            try:
                data = BusWhereCoordinator._parse_maps_data(html)
            except ValueError:
                return None, "no_data"
            return data.get("stops", []), None
        finally:
            await session.close()

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Begin reauth when the stored credentials stop working."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._org = entry_data[CONF_ORG]
        self._route_id = entry_data[CONF_ROUTE_ID]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-collect the username and secret key, then re-validate."""
        assert self._reauth_entry is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password_secret = user_input[CONF_PASSWORD_SECRET].strip()
            _, error = await self._async_validate(
                self._org, self._route_id, username, password_secret
            )
            if error:
                errors["base"] = error
            else:
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={
                        **self._reauth_entry.data,
                        CONF_USERNAME: username,
                        CONF_PASSWORD_SECRET: password_secret,
                    },
                )
                await self.hass.config_entries.async_reload(
                    self._reauth_entry.entry_id
                )
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=self._reauth_entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(
                        CONF_PASSWORD_SECRET,
                        default=self._reauth_entry.data.get(
                            CONF_PASSWORD_SECRET, DEFAULT_PASSWORD_SECRET
                        ),
                    ): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return BusWhereOptionsFlow(config_entry)


class BusWhereOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for BusWhere — rename stops."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__()
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show form with zone radius and current stop names for editing."""
        if user_input is not None:
            zone_radius = int(
                user_input.get(CONF_ZONE_RADIUS, DEFAULT_ZONE_RADIUS)
            )
            stop_names: dict[str, str] = {}
            for key, value in user_input.items():
                if key.startswith("stop_order_") and value:
                    order = key[len("stop_order_"):]
                    stop_names[order] = value.strip()

            return self.async_create_entry(
                title="",
                data={
                    CONF_STOP_NAMES: stop_names,
                    CONF_ZONE_RADIUS: zone_radius,
                    CONF_CREATE_STOP_ZONES: bool(
                        user_input.get(
                            CONF_CREATE_STOP_ZONES, DEFAULT_CREATE_STOP_ZONES
                        )
                    ),
                },
            )

        # Get stops from the coordinator
        entry_data = self.hass.data.get(DOMAIN, {}).get(
            self._config_entry.entry_id, {}
        )
        coordinator = entry_data.get("coordinator")
        stops = []
        if coordinator and coordinator.data:
            stops = coordinator.data.get("stops", [])

        existing_names = self._config_entry.options.get(CONF_STOP_NAMES, {})
        current_radius = self._config_entry.options.get(
            CONF_ZONE_RADIUS, DEFAULT_ZONE_RADIUS
        )
        current_create_zones = self._config_entry.options.get(
            CONF_CREATE_STOP_ZONES, DEFAULT_CREATE_STOP_ZONES
        )

        schema_dict: dict[Any, Any] = {}

        schema_dict[
            vol.Optional(
                CONF_CREATE_STOP_ZONES,
                default=current_create_zones,
            )
        ] = bool

        schema_dict[
            vol.Optional(
                CONF_ZONE_RADIUS,
                default=current_radius,
            )
        ] = NumberSelector(
            NumberSelectorConfig(
                min=10,
                max=500,
                step=5,
                unit_of_measurement="meters",
                mode=NumberSelectorMode.BOX,
            )
        )

        for stop in sorted(stops, key=lambda s: s.get("order", 0)):
            order = str(stop.get("order", stop["id"]))
            default_name = stop.get("address") or f"Stop {order}"
            current_name = existing_names.get(order, default_name)

            schema_dict[
                vol.Optional(
                    f"stop_order_{order}",
                    description={"suggested_value": current_name},
                )
            ] = TextSelector(TextSelectorConfig())

        if len(schema_dict) == 1:
            schema_dict[
                vol.Optional("_no_stops")
            ] = TextSelector(TextSelectorConfig())

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "stop_info": "\n".join(
                    f"Stop {s.get('order', '?')}: {s.get('address', 'Unknown')}"
                    for s in sorted(stops, key=lambda s: s.get("order", 0))
                )
            },
        )
