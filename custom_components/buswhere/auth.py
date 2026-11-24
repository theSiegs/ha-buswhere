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

import aiohttp

from homeassistant.core import HomeAssistant

from .const import (
    BASE_URL,
    FALLBACK_LOGIN_ACTION,
    LOGIN_PAGE_PATH,
    SECRETS_FILE,
    SESSION_COOKIE,
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

    @property
    def username(self) -> str:
        return self._username

    async def async_login(self, session: aiohttp.ClientSession) -> None:
        """Log in, populating the session's cookie jar.

        Raises MissingSecret, InvalidAuth or CannotConnect.
        """
        password = await async_read_secret(self._hass, self._password_secret)

        # 1) GET the login page for a fresh CSRF token + session cookie.
        try:
            async with session.get(
                f"{BASE_URL}{LOGIN_PAGE_PATH}",
                headers={"User-Agent": USER_AGENT},
            ) as resp:
                page = await resp.text()
        except aiohttp.ClientError as err:
            raise CannotConnect(f"Could not load BusWhere login page: {err}") from err

        action, fields = _parse_login_form(page)
        fields["email"] = self._username
        fields["password"] = password

        post_headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": f"{BASE_URL}{LOGIN_PAGE_PATH}",
        }
        csrf = _CSRF_META_RE.search(page)
        if csrf:
            post_headers["X-CSRF-Token"] = html.unescape(csrf.group(1))

        post_url = action if action.startswith("http") else f"{BASE_URL}{action}"

        # 2) POST the credentials. Success is a redirect / a page without the
        #    login form; failure re-renders the login form.
        try:
            async with session.post(
                post_url,
                data=fields,
                headers=post_headers,
                allow_redirects=True,
            ) as resp:
                status = resp.status
                text = await resp.text()
        except aiohttp.ClientError as err:
            raise CannotConnect(f"Could not submit BusWhere login: {err}") from err

        if status in (401, 403, 422) or LOGIN_FORM_MARKER in text:
            raise InvalidAuth("BusWhere rejected the email/password")
        if status >= 500:
            raise CannotConnect(f"BusWhere returned HTTP {status} during login")

        has_cookie = any(
            cookie.key == SESSION_COOKIE for cookie in session.cookie_jar
        )
        if not has_cookie:
            # No session cookie means the login did not take, even if the body
            # looked benign.
            raise InvalidAuth("BusWhere did not establish a session")
        _LOGGER.debug("BusWhere login succeeded for %s", self._username)
