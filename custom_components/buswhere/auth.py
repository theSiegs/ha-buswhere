"""Authentication for BusWhere school (parent-login) routes.

BusWhere school accounts log in through the web form at ``/schools/sessions``,
which POSTs to ``/sessions`` and sets the Rails ``_buswhere_session`` cookie.
The integration fetches the (web) route pages, so cookie-based auth is the
natural fit: we log in once into a persistent session's cookie jar and reuse
it for polling, re-logging-in when the cookie goes stale.

The password is never stored by this integration. It is read on demand from
Home Assistant's ``secrets.yaml`` by the key name referenced in the config
entry; only the resulting session cookie lives (in memory) in the coordinator.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant

from .const import (
    API_SESSIONS_PATH,
    BASE_URL,
    FALLBACK_LOGIN_ACTION,
    LOGIN_PAGE_PATH,
    SECRETS_FILE,
    SESSION_COOKIE,
    TOKEN_KEYS,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# The login page re-renders this form id when credentials are rejected, so its
# presence in a response means "still not logged in".
LOGIN_FORM_MARKER = 'id="login-form"'

_FORM_RE = re.compile(
    r'<form\b[^>]*\bid="login-form"[^>]*>(?P<body>.*?)</form>',
    re.IGNORECASE | re.DOTALL,
)
_FORM_ACTION_RE = re.compile(
    r'<form\b[^>]*\bid="login-form"[^>]*\baction="(?P<action>[^"]+)"',
    re.IGNORECASE,
)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
_CSRF_META_RE = re.compile(
    r'<meta\b[^>]*\bname="csrf-token"[^>]*\bcontent="([^"]+)"', re.IGNORECASE
)


class AuthError(Exception):
    """Base authentication error."""


class InvalidAuth(AuthError):
    """The credentials were rejected."""


class MissingSecret(AuthError):
    """The referenced secrets.yaml key could not be read."""


class CannotConnect(AuthError):
    """A network error occurred talking to BusWhere."""


async def async_read_secret(hass: HomeAssistant, key: str) -> str:
    """Read a single key from Home Assistant's secrets.yaml."""
    import yaml  # local import: only needed when auth is configured

    path = hass.config.path(SECRETS_FILE)

    def _read() -> str:
        try:
            with open(path, encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except FileNotFoundError as err:
            raise MissingSecret(f"{SECRETS_FILE} not found at {path}") from err
        except (OSError, yaml.YAMLError) as err:
            raise MissingSecret(f"Could not read {SECRETS_FILE}: {err}") from err
        if not isinstance(data, dict) or key not in data:
            raise MissingSecret(f"Key '{key}' not found in {SECRETS_FILE}")
        value = data[key]
        if not isinstance(value, str) or not value:
            raise MissingSecret(f"Key '{key}' in {SECRETS_FILE} is empty")
        return value

    return await hass.async_add_executor_job(_read)


def _parse_login_form(page: str) -> tuple[str, dict[str, str]]:
    """Return (action, fields) for the login form, mirroring the browser.

    Every hidden input (authenticity_token, school_country_name, …) is carried
    through unchanged so the POST matches what the site expects.
    """
    form = _FORM_RE.search(page)
    if not form:
        raise InvalidAuth("Login form not found on the BusWhere login page")

    action_match = _FORM_ACTION_RE.search(page)
    action = html.unescape(action_match.group("action")) if action_match else FALLBACK_LOGIN_ACTION

    fields: dict[str, str] = {}
    for tag in _INPUT_RE.findall(form.group("body")):
        attrs = dict(_ATTR_RE.findall(tag))
        name = attrs.get("name")
        if name:
            fields[name] = html.unescape(attrs.get("value", ""))
    return action, fields


def _has_session_cookie(session: aiohttp.ClientSession) -> bool:
    """True if the session jar holds a BusWhere session cookie."""
    return any(cookie.key == SESSION_COOKIE for cookie in session.cookie_jar)


def _find_token(payload: Any) -> str | None:
    """Recursively search a JSON payload for a token-ish string value."""
    if isinstance(payload, dict):
        for key in TOKEN_KEYS:
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
        for val in payload.values():
            found = _find_token(val)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_token(item)
            if found:
                return found
    return None


class BusWhereAuth:
    """Logs a persistent aiohttp session into BusWhere and keeps it logged in.

    Holds no password: it reads the secret fresh each time it logs in.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password_secret: str,
    ) -> None:
        self._hass = hass
        self._username = username
        self._password_secret = password_secret
        self._token: str | None = None

    @property
    def username(self) -> str:
        return self._username

    def apply_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Add a bearer token header if the API login returned one."""
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def async_login(self, session: aiohttp.ClientSession) -> None:
        """Log in, populating the session's cookie jar.

        Tries the web sign-in form first (the way the site's data-remote form
        submits over AJAX), then the mobile API as a fallback. Raises
        MissingSecret, InvalidAuth or CannotConnect.
        """
        password = await async_read_secret(self._hass, self._password_secret)

        if await self._try_web_form(session, password):
            return
        if await self._try_api(session, password):
            return
        raise InvalidAuth(
            "BusWhere rejected the sign-in (web form and mobile API both failed)"
        )

    async def _try_web_form(
        self, session: aiohttp.ClientSession, password: str
    ) -> bool:
        """Submit the web sign-in form the way its data-remote form does."""
        try:
            async with session.get(
                f"{BASE_URL}{LOGIN_PAGE_PATH}",
                headers={"User-Agent": USER_AGENT},
            ) as resp:
                page = await resp.text()
                get_status = resp.status
        except aiohttp.ClientError as err:
            raise CannotConnect(f"Could not load BusWhere login page: {err}") from err

        try:
            action, fields = _parse_login_form(page)
        except InvalidAuth:
            _LOGGER.debug(
                "buswhere: no login form at %s (GET status %s) — skipping web form",
                LOGIN_PAGE_PATH,
                get_status,
            )
            return False

        fields["email"] = self._username
        fields["password"] = password

        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            # data-remote form (Rails UJS): submits over AJAX, answered as JS.
            "Accept": "text/javascript, text/html, application/xml, text/xml, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}{LOGIN_PAGE_PATH}",
        }
        csrf = _CSRF_META_RE.search(page)
        if csrf:
            headers["X-CSRF-Token"] = html.unescape(csrf.group(1))

        post_url = action if action.startswith("http") else f"{BASE_URL}{action}"
        try:
            async with session.post(
                post_url, data=fields, headers=headers, allow_redirects=True
            ) as resp:
                status = resp.status
                text = await resp.text()
        except aiohttp.ClientError as err:
            raise CannotConnect(f"Could not submit BusWhere login: {err}") from err

        has_cookie = _has_session_cookie(session)
        form_back = LOGIN_FORM_MARKER in text
        _LOGGER.debug(
            "buswhere web-form login: POST %s -> %s (cookie=%s, login_form_in_response=%s, body=%dB)",
            post_url,
            status,
            has_cookie,
            form_back,
            len(text),
        )
        if status >= 500:
            raise CannotConnect(f"BusWhere returned HTTP {status} during login")
        if status in (401, 403, 422) or form_back:
            return False
        return has_cookie

    async def _try_api(
        self, session: aiohttp.ClientSession, password: str
    ) -> bool:
        """Fallback: the mobile API session endpoint (JSON)."""
        url = f"{BASE_URL}{API_SESSIONS_PATH}"
        creds = {"email": self._username, "password": password}
        for body, label in ((creds, "flat"), ({"session": creds}, "session")):
            try:
                async with session.post(
                    url,
                    json=body,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                ) as resp:
                    status = resp.status
                    try:
                        payload = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        payload = None
            except aiohttp.ClientError as err:
                raise CannotConnect(f"Could not reach BusWhere API: {err}") from err

            has_cookie = _has_session_cookie(session)
            token = _find_token(payload)
            _LOGGER.debug(
                "buswhere api login: POST %s (%s) -> %s (cookie=%s, token=%s)",
                API_SESSIONS_PATH,
                label,
                status,
                has_cookie,
                bool(token),
            )
            if status in (200, 201) and (has_cookie or token):
                if token:
                    self._token = token
                return True
        return False
