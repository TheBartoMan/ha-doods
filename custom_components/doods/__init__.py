"""The DOODS integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from pydoods import PyDOODS

import homeassistant.components.image_processing as ha_image_processing
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_TIMEOUT,
    CONF_URL,
    EVENT_HOMEASSISTANT_STARTED,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import ConfigType

from .const import CONF_AUTH_KEY, CONF_DETECTOR, DATA_SEEN_YAML_ENTRIES, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.IMAGE_PROCESSING]

# `image_processing` predates config entries and was never given the
# generic async_setup_entry/async_unload_entry shim that domains like
# `sensor` or `binary_sensor` have, which is what lets other integrations
# forward config-entry platform setup to them. Without it,
# hass.config_entries.async_forward_entry_setups fails with "module
# 'homeassistant.components.image_processing' has no attribute
# 'async_setup_entry'". A real upstream PR would add this shim to
# homeassistant/components/image_processing/__init__.py directly (the way
# every other config-entry-capable platform domain already has it); since
# this is a custom component we can't edit core files, so we add it here at
# import time instead. This is a no-op if a future HA version adds it
# natively.
#
# Unlike `sensor`/`binary_sensor`, the base `image_processing.async_setup()`
# keeps its EntityComponent as a purely local variable -- it's never stored
# on hass.data, so there's nothing shared for us to reuse. We keep our own
# EntityComponent instance instead, scoped to our own hass.data key. This
# doesn't conflict with the base component's YAML-only EntityComponent:
# `image_processing.py`'s async_setup_platform no longer creates entities
# directly (it only imports YAML into a config entry), so there's no
# duplicate-entity risk between the two.
_IMAGE_PROCESSING_COMPONENT_KEY = f"{DOMAIN}_image_processing_component"


def _get_image_processing_component(hass: HomeAssistant) -> EntityComponent:
    """Get (or lazily create) our own EntityComponent for image_processing."""
    component = hass.data.get(_IMAGE_PROCESSING_COMPONENT_KEY)
    if component is None:
        component = EntityComponent(
            _LOGGER, ha_image_processing.DOMAIN, hass, ha_image_processing.SCAN_INTERVAL
        )
        hass.data[_IMAGE_PROCESSING_COMPONENT_KEY] = component
    return component


if not hasattr(ha_image_processing, "async_setup_entry"):

    async def _image_processing_async_setup_entry(
        hass: HomeAssistant, entry: ConfigEntry
    ) -> bool:
        component = _get_image_processing_component(hass)
        return await component.async_setup_entry(entry)

    async def _image_processing_async_unload_entry(
        hass: HomeAssistant, entry: ConfigEntry
    ) -> bool:
        component = _get_image_processing_component(hass)
        return await component.async_unload_entry(entry)

    ha_image_processing.async_setup_entry = _image_processing_async_setup_entry
    ha_image_processing.async_unload_entry = _image_processing_async_unload_entry


_CLEANUP_LISTENER_REGISTERED_KEY = f"{DOMAIN}_yaml_cleanup_listener_registered"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up housekeeping that doesn't depend on any one config entry.

    Registers a one-time listener that clears stale "please remove your
    YAML" repair issues once their server+detector no longer has any
    `platform: doods` block left in configuration.yaml, so the notice goes
    away by itself instead of lingering forever. image_processing.py's
    async_setup_platform (which runs once per YAML block, during startup,
    before HA fires "started") records each block it sees this run into
    hass.data[DATA_SEEN_YAML_ENTRIES]; anything with an existing issue that
    *isn't* in that set by the time HA has started is stale.
    """
    if hass.data.get(_CLEANUP_LISTENER_REGISTERED_KEY):
        return True
    hass.data[_CLEANUP_LISTENER_REGISTERED_KEY] = True

    @callback
    def _clear_stale_yaml_issues(_event: Event) -> None:
        seen = hass.data.get(DATA_SEEN_YAML_ENTRIES, set())
        registry = ir.async_get(hass)
        prefix = "deprecated_yaml_"
        for issue in list(registry.issues.values()):
            if (
                issue.domain == DOMAIN
                and issue.issue_id.startswith(prefix)
                and issue.issue_id.removeprefix(prefix) not in seen
            ):
                ir.async_delete_issue(hass, DOMAIN, issue.issue_id)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _clear_stale_yaml_issues)
    return True


@dataclass
class DoodsData:
    """Runtime data for a DOODS config entry."""

    client: PyDOODS
    detector: dict[str, Any]


type DoodsConfigEntry = ConfigEntry[DoodsData]


def _connect_sync(url: str, auth_key: str, timeout: int) -> tuple[PyDOODS, Any]:
    """Create a PyDOODS client and fetch its detectors (blocking).

    PyDOODS's constructor itself makes a blocking network call (it calls
    get_detectors() internally), so both the construction and the call must
    happen in the executor.
    """
    client = PyDOODS(url, auth_key, timeout)
    return client, client.get_detectors()


async def async_setup_entry(hass: HomeAssistant, entry: DoodsConfigEntry) -> bool:
    """Set up DOODS from a config entry."""
    client, response = await hass.async_add_executor_job(
        _connect_sync,
        entry.data[CONF_URL],
        entry.data[CONF_AUTH_KEY],
        entry.data[CONF_TIMEOUT],
    )
    if not isinstance(response, dict) or "detectors" not in response:
        raise ConfigEntryNotReady(
            f"Unable to connect to DOODS server at {entry.data[CONF_URL]}"
        )

    detector_name = entry.data[CONF_DETECTOR]
    detector = next(
        (d for d in response["detectors"] if d["name"] == detector_name), None
    )
    if detector is None:
        raise ConfigEntryNotReady(
            f"Detector {detector_name} is no longer available on DOODS server"
            f" {entry.data[CONF_URL]}"
        )

    entry.runtime_data = DoodsData(client=client, detector=detector)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


_RELOAD_DEBOUNCE_SECONDS = 2


async def _async_update_listener(hass: HomeAssistant, entry: DoodsConfigEntry) -> None:
    """Reload the entry when its options are updated.

    Debounced: several YAML blocks sharing a server+detector can each merge
    a camera into this entry's options in quick succession (e.g. during
    startup import), which would otherwise trigger a reload per block and
    risk overlapping unload/setup cycles. This coalesces those into a
    single reload once updates settle down.
    """
    key = f"{DOMAIN}_reload_cancel_{entry.entry_id}"
    if cancel := hass.data.get(key):
        cancel()

    @callback
    def _schedule_reload(_now: Any) -> None:
        hass.data.pop(key, None)
        hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))

    hass.data[key] = async_call_later(
        hass, _RELOAD_DEBOUNCE_SECONDS, _schedule_reload
    )


async def async_unload_entry(hass: HomeAssistant, entry: DoodsConfigEntry) -> bool:
    """Unload a DOODS config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
