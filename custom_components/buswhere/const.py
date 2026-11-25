"""Constants for the BusWhere integration."""

DOMAIN = "buswhere"

CONF_ROUTE_URL = "route_url"
CONF_ORG = "org"
CONF_ROUTE_ID = "route_id"
# The path after the host for this route's page, e.g. "/map/12345" (school,
# behind a parent login) or "/theory/routes/theory_shuttle" (public shuttle).
# The coordinator fetches BASE_URL + this path and parses the embedded
# Maps.data. Stored so both URL styles are supported without an org in the path.
CONF_ROUTE_PATH = "route_path"
CONF_SCAN_INTERVAL = "scan_interval"

CONF_STOP_NAMES = "stop_names"
CONF_ZONE_RADIUS = "zone_radius"
# Whether to create a map zone per stop. Off by default: active stop zones
# feed HA presence and can flip a phone to "at <stop>" near home, which is
# usually unwanted. ETA sensors do not need zones.
CONF_CREATE_STOP_ZONES = "create_stop_zones"
DEFAULT_CREATE_STOP_ZONES = False

# Authentication (optional — public shuttle routes need none; school routes
# that sit behind a parent login do).
CONF_USERNAME = "username"
# We store the *name of a key* in Home Assistant's secrets.yaml, never the
# password itself. The password lives only in secrets.yaml.
CONF_PASSWORD_SECRET = "password_secret"

DEFAULT_SCAN_INTERVAL = 30  # seconds
DEFAULT_ZONE_RADIUS = 80  # meters — matches BusWhere's stop_threshold (~79m)
FULL_REFRESH_INTERVAL = 300  # 5 minutes — re-fetch HTML for stop ETAs

BASE_URL = "https://buswhere.com"

# Web login. The school parent login form lives here and POSTs to the action
# below, which sets the Rails `_buswhere_session` cookie. The route pages the
# integration fetches are web pages authorised by that same cookie, so the
# integration logs in through this form (not the mobile `/api/v1/sessions`
# token API) and reuses the cookie.
LOGIN_PAGE_PATH = "/schools/sessions"
FALLBACK_LOGIN_ACTION = "/sessions"

# The Rails session cookie set on a successful login.
SESSION_COOKIE = "_buswhere_session"

# Home Assistant secrets file (read for the referenced password key).
SECRETS_FILE = "secrets.yaml"

# Default secrets.yaml key suggested in the UI.
DEFAULT_PASSWORD_SECRET = "buswhere_password"

USER_AGENT = "HomeAssistant-BusWhere/1.0"
