"""Diagnostics support for DOODS.

Complements the in-flow "View/export as YAML" screen (config_flow.py's
async_step_export_yaml) with HA's standard "Download diagnostics" button
on the integration's device/entry page. Unlike the YAML export, the auth
key is redacted here, since diagnostics downloads are meant to be shared
with others (e.g. attached to a bug report) rather than kept as a private
backup.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_AUTH_KEY

TO_REDACT = {CONF_AUTH_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a DOODS config entry."""
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": dict(entry.options),
    }
