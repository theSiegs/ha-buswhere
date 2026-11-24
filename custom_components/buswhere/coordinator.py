"""DataUpdateCoordinator for BusWhere shuttle tracking."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .auth import (
    LOGIN_FORM_MARKER,
    BusWhereAuth,
    CannotConnect,
    InvalidAuth,
    MissingSecret,
)
from .const import BASE_URL, DEFAULT_SCAN_INTERVAL, FULL_REFRESH_INTERVAL, USER_AGENT

_LOGGER = logging.getLogger(__name__)


class BusWhereCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls BusWhere for shuttle data."""

    def __init__(
        self,
        hass: HomeAssistant,
        org: str,
        route_id: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        auth: BusWhereAuth | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"buswhere_{org}_{route_id}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.org = org
        self.route_id = route_id
        self._url = f"{BASE_URL}/{org}/routes/{route_id}"
        self._auth = auth
        self._logged_in = False
        self._full_data: dict[str, Any] | None = None
        self._last_full_fetch: float = 0
        # Own, cookie-isolated session so the auth cookie is not shared with
        # the rest of Home Assistant. Closed on unload via async_shutdown().
        self._session: aiohttp.ClientSession = async_create_clientsession(
            hass,
            auto_cleanup=False,
            cookie_jar=aiohttp.CookieJar(),
        )

    async def async_shutdown(self) -> None:
        """Close the private HTTP session when the entry unloads."""
        await super().async_shutdown()
        if not self._session.closed:
            await self._session.close()

    async def _async_login(self) -> None:
        """Log in via the auth helper, mapping errors to HA exceptions."""
        assert self._auth is not None
        try:
            await self._auth.async_login(self._session)
        except (InvalidAuth, MissingSecret) as err:
            # Credentials/secret problem — ask the user to fix it (reauth).
            raise ConfigEntryAuthFailed(str(err)) from err
        except CannotConnect as err:
            raise UpdateFailed(str(err)) from err
        self._logged_in = True

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from BusWhere."""
        if self._auth is not None and not self._logged_in:
            await self._async_login()

        now = time.monotonic()
        needs_full = (
            self._full_data is None
            or (now - self._last_full_fetch) >= FULL_REFRESH_INTERVAL
        )

        try:
            if needs_full:
                data = await self._fetch_full()
                self._full_data = data
                self._last_full_fetch = now
            else:
                poll = await self._fetch_poll()
                data = {**self._full_data, **poll}
                self._full_data = data
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with BusWhere: {err}") from err
        except (json.JSONDecodeError, ValueError) as err:
            raise UpdateFailed(f"Error parsing BusWhere data: {err}") from err

        return data

    def _looks_like_login(self, status: int, text: str) -> bool:
        """True if the response is an auth challenge rather than route data."""
        return status in (401, 403) or LOGIN_FORM_MARKER in text

    async def _fetch_full(self) -> dict[str, Any]:
        """Fetch the full HTML page and parse Maps.data for complete route info."""
        headers = {"User-Agent": USER_AGENT}
        status, html = await self._get(self._url, headers=headers)
        if status != 200:
            raise UpdateFailed(f"BusWhere returned HTTP {status}")
        return self._parse_maps_data(html)

    async def _fetch_poll(self) -> dict[str, Any]:
        """Fetch the lightweight JSON polling endpoint."""
        params = {
            "t": str(int(time.time() * 1000)),
            "initial": "true",
            "filter": "",
        }
        headers = {
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        status, text = await self._get(self._url, headers=headers, params=params)
        if status != 200:
            raise UpdateFailed(f"BusWhere returned HTTP {status}")
        return json.loads(text)

    async def _get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """GET a URL on the private session; re-login once on an auth challenge.

        When no auth is configured (public routes), this is a plain GET.
        """
        async with self._session.get(url, params=params, headers=headers) as resp:
            status = resp.status
            text = await resp.text()

        if self._auth is None:
            return status, text

        if self._looks_like_login(status, text):
            _LOGGER.debug("BusWhere session looks expired; re-logging in")
            self._logged_in = False
            await self._async_login()
            async with self._session.get(url, params=params, headers=headers) as resp:
                status = resp.status
                text = await resp.text()
            if self._looks_like_login(status, text):
                raise ConfigEntryAuthFailed(
                    "BusWhere still requires login after re-authenticating"
                )
        return status, text

    @staticmethod
    def _parse_maps_data(html: str) -> dict[str, Any]:
        """Extract and parse the Maps.data JSON object from the HTML page."""
        idx = html.find("Maps.data =")
        if idx == -1:
            raise ValueError("Could not find Maps.data in page")

        # Find the opening brace
        brace_start = html.index("{", idx)

        # Walk forward counting braces to find the matching close
        depth = 0
        end = brace_start
        in_string = False
        escape_next = False
        for i in range(brace_start, len(html)):
            ch = html[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        raw = html[brace_start:end]

        # The JS object may contain unquoted keys or URL values that are valid JS
        # but not strict JSON. The BusWhere output is clean JSON though, so try
        # direct parse first, then fall back to a light cleanup.
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Strip JS-style trailing commas before } or ]
            cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
            return json.loads(cleaned)
