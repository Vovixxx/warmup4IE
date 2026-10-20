"""Constants for the Warmup integration."""

DOMAIN = "warmup"

CONF_LOCATION = "location"
CONF_LOCATIONS = "locations"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 600

PLATFORMS = ["climate", "sensor"]

ATTR_UNTIL = "until"
ATTR_DURATION = "duration"
ATTR_DAYS = "days"
ATTR_MODE = "mode"

CONST_MODE_PROGRAM = "prog"
CONST_MODE_FIXED = "fixed"
CONST_MODE_AWAY = "away"
CONST_MODE_FROST = "frost"
CONST_MODE_OFF = "off"

BOOST_MINUTES = 60
