"""Constants for Power Sense."""

DOMAIN = "power_sense"
SUBENTRY_DEVICE = "device"
SUBENTRY_BULK = "bulk"

CONF_PRICE = "price"
CONF_NAME = "name"
CONF_POWER_SENSORS = "power_sensors"
CONF_ENTITIES = "entities"
CONF_DEFAULT_WATTS = "default_watts"
CONF_SCALE_BRIGHTNESS = "scale_brightness"
CONF_WATTS = "watts"

DEFAULT_PRICE = 0.30
DEFAULT_WATTS = 10.0
MIN_LEVEL = 0.1  # dimmed lamps count at least 10 % of their nominal power

ESTIMATE_DOMAINS = ["light", "switch", "fan", "input_boolean"]

STORAGE_VERSION = 1
SAVE_INTERVAL_S = 300
TICK_INTERVAL_S = 60
