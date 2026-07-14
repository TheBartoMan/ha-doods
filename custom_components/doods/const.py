"""Constants for the DOODS integration."""

DOMAIN = "doods"

CONF_AUTH_KEY = "auth_key"
CONF_DETECTOR = "detector"
CONF_CAMERAS = "cameras"

# Per-camera settings stored inside a CONF_CAMERAS entry's options.
# CONF_PROFILE_ID uniquely identifies one detection *profile*, since a
# single camera entity can have more than one (e.g. a general-purpose
# profile and a separate one cropped to a specific zone). It backs each
# profile's entity unique_id and lets YAML re-imports update the right
# profile across restarts instead of duplicating it.
CONF_PROFILE_ID = "profile_id"
# CONF_LABELS holds a {label: info} mapping (empty means "any label"), where
# info is {CONF_CONFIDENCE: float, CONF_AREA: {...} | omitted}. Profiles
# saved before per-label areas existed stored a plain float per label
# instead of this dict -- both shapes are read transparently wherever
# CONF_LABELS values are consumed (see _label_info() in config_flow.py and
# image_processing.py), so old config entries keep working unmodified.
CONF_LABELS = "labels"
CONF_AREA = "area"
CONF_TOP = "top"
CONF_BOTTOM = "bottom"
CONF_RIGHT = "right"
CONF_LEFT = "left"
CONF_COVERS = "covers"
CONF_FILE_OUT = "file_out"
# Transient form field (config flow only) -- not stored. Toggles whether
# the label_confidence step's area fields should be saved as this label's
# CONF_AREA override.
CONF_RESTRICT_AREA = "restrict_area"

DEFAULT_TIMEOUT = 90
DEFAULT_CONFIDENCE = 50.0
# Matches image_processing's own default poll interval, so a profile with
# no scan_interval set (new, added via the UI) behaves the same as before
# this was configurable.
DEFAULT_SCAN_INTERVAL = 10

# hass.data key: set of "{url}_{detector}" strings that had at least one
# `platform: doods` YAML block seen *this run*. Used to auto-clear a
# server+detector's "remove your YAML" repair issue once none of its
# blocks are left in configuration.yaml -- see __init__.async_setup and
# image_processing.async_setup_platform.
DATA_SEEN_YAML_ENTRIES = f"{DOMAIN}_seen_yaml_entries"
