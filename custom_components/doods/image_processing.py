"""Support for the DOODS service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import io
import logging
import os
import time
from typing import Any, override

from PIL import Image, ImageDraw, UnidentifiedImageError
from pydoods import PyDOODS
import voluptuous as vol

from homeassistant.components.image_processing import (
    CONF_CONFIDENCE,
    PLATFORM_SCHEMA as IMAGE_PROCESSING_PLATFORM_SCHEMA,
    ImageProcessingEntity,
)
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import (
    CONF_COVERS,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    CONF_URL,
)
from homeassistant.core import HomeAssistant, split_entity_id
from homeassistant.helpers import config_validation as cv, template
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util.pil import draw_box

from . import DoodsConfigEntry
from .const import (
    CONF_AREA,
    CONF_AUTH_KEY,
    CONF_BOTTOM,
    CONF_CAMERAS,
    CONF_DETECTOR,
    CONF_FILE_OUT,
    CONF_LABELS,
    CONF_LEFT,
    CONF_PROFILE_ID,
    CONF_RIGHT,
    CONF_TOP,
    DATA_SEEN_YAML_ENTRIES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _label_info(value: Any) -> dict[str, Any]:
    """Normalize a stored CONF_LABELS entry to {confidence, area} form.

    Profiles saved before per-label areas existed stored a plain float
    (just the confidence) per label instead of a dict. Reading both shapes
    here means already-deployed config entries keep working unmodified.
    Kept identical to (but independent of) config_flow._label_info, since
    platform files don't import from config_flow.
    """
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {CONF_CONFIDENCE: value}


ATTR_MATCHES = "matches"
ATTR_SUMMARY = "summary"
ATTR_TOTAL_MATCHES = "total_matches"
ATTR_PROCESS_TIME = "process_time"

# Schema kept only to parse legacy `image_processing:` YAML entries so they
# can be imported into a config entry. New setups use the config flow.
AREA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_BOTTOM, default=1): cv.small_float,
        vol.Optional(CONF_LEFT, default=0): cv.small_float,
        vol.Optional(CONF_RIGHT, default=1): cv.small_float,
        vol.Optional(CONF_TOP, default=0): cv.small_float,
        vol.Optional(CONF_COVERS, default=True): cv.boolean,
    }
)

LABEL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_AREA): AREA_SCHEMA,
        vol.Optional(CONF_CONFIDENCE): vol.Range(min=0, max=100),
    }
)

PLATFORM_SCHEMA = IMAGE_PROCESSING_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_URL): cv.string,
        vol.Required(CONF_DETECTOR): cv.string,
        vol.Required(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
        vol.Optional(CONF_AUTH_KEY, default=""): cv.string,
        vol.Optional(CONF_FILE_OUT, default=[]): vol.All(
            cv.ensure_list, [cv.template]
        ),
        vol.Optional(CONF_CONFIDENCE, default=0.0): vol.Range(min=0, max=100),
        vol.Optional(CONF_LABELS, default=[]): vol.All(
            cv.ensure_list, [vol.Any(cv.string, LABEL_SCHEMA)]
        ),
        vol.Optional(CONF_AREA): AREA_SCHEMA,
        # Explicitly declared (rather than relying on it being inherited
        # from the base platform schema) so it's guaranteed to be parsed
        # into a timedelta for the import step in config_flow.py.
        vol.Optional(CONF_SCAN_INTERVAL): cv.time_period,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the DOODS platform from YAML.

    DOODS is now configured via the UI. This only exists to import any
    existing YAML configuration into a config entry.
    """
    # Keyed the same way as the config entry's own unique_id (url_detector),
    # so this is the one canonical issue per server+detector regardless of
    # how many YAML blocks (cameras) reference it. Recorded in hass.data so
    # the EVENT_HOMEASSISTANT_STARTED listener registered in
    # __init__.async_setup can tell, once every YAML platform this run has
    # had a chance to run, which of these issues are now stale (i.e. their
    # last YAML block was deleted) and clear them automatically -- see that
    # listener for details.
    issue_key = f"{config[CONF_URL]}_{config[CONF_DETECTOR]}"
    hass.data.setdefault(DATA_SEEN_YAML_ENTRIES, set()).add(issue_key)
    async_create_issue(
        hass,
        DOMAIN,
        f"deprecated_yaml_{issue_key}",
        breaks_in_ha_version="2026.11.0",
        is_fixable=False,
        severity=IssueSeverity.WARNING,
        translation_key="deprecated_yaml",
    )
    # Assign each YAML block a stable, deterministic index among blocks that
    # share this server+detector (in YAML order). This runs synchronously,
    # before the import flow task below is even scheduled, so it's safe
    # even though several blocks' flows will then race concurrently -- the
    # config flow uses this index to build a per-block profile id so
    # re-imports on every restart update the right profile instead of
    # duplicating it (see config_flow.async_step_import).
    counters: dict[tuple[str, str], int] = hass.data.setdefault(
        f"{DOMAIN}_import_block_counters", {}
    )
    counter_key = (config[CONF_URL], config[CONF_DETECTOR])
    block_index = counters.get(counter_key, 0)
    counters[counter_key] = block_index + 1
    import_config = {**config, "_block_index": block_index}

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=import_config
        )
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DoodsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DOODS image processing entities from a config entry."""
    data = entry.runtime_data
    cameras = entry.options.get(CONF_CAMERAS, [])
    # Number profiles that share a camera entity (1, 2, ...) so their
    # entity names stay distinguishable, e.g. "Doods front_door" and
    # "Doods front_door 2".
    seen: dict[str, int] = {}
    entities = []
    for camera_config in cameras:
        entity_id = camera_config[CONF_ENTITY_ID]
        seen[entity_id] = seen.get(entity_id, 0) + 1
        entities.append(
            Doods(
                hass,
                data.client,
                data.detector,
                entry.entry_id,
                camera_config,
                seen[entity_id],
            )
        )
    async_add_entities(entities)


class Doods(ImageProcessingEntity):
    """Doods image processing service client."""

    # Each profile can have its own scan_interval (imported from its YAML
    # block, or set in the UI). HA's built-in polling only supports one
    # shared interval per platform/config-entry, so instead of that we turn
    # off default polling and each entity runs its own timer -- see
    # async_added_to_hass below.
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        doods: PyDOODS,
        detector: dict[str, Any],
        entry_id: str,
        camera_config: dict[str, Any],
        profile_index: int = 1,
    ) -> None:
        """Initialize the DOODS entity."""
        camera_entity: str = camera_config[CONF_ENTITY_ID]
        self._attr_camera_entity = camera_entity
        base_name = f"Doods {split_entity_id(camera_entity)[1]}"
        self._attr_name = (
            base_name if profile_index == 1 else f"{base_name} {profile_index}"
        )
        # camera_config.get(CONF_PROFILE_ID) falls back to the camera entity
        # for any pre-existing entry saved before profiles existed.
        profile_id = camera_config.get(CONF_PROFILE_ID) or camera_entity
        self._attr_unique_id = f"{entry_id}_{profile_id}"
        self._doods = doods
        self._detector_name = detector["name"]

        # detector aspect ratio, used to log a hint if the camera looks mismatched
        self._aspect: float | None = None
        if detector.get("width") and detector.get("height"):
            self._aspect = detector["width"] / detector["height"]

        base_confidence: float = camera_config[CONF_CONFIDENCE]
        labels: dict[str, Any] = camera_config.get(CONF_LABELS) or {}

        # dconfig is what's actually sent to the DOODS API: a flat
        # {label: confidence} map (or {"*": confidence} for "any label").
        # Per-label areas are a purely local post-filter below -- DOODS
        # itself has no concept of them -- so they're kept separate from
        # dconfig rather than folded into it.
        dconfig: dict[str, float] = {}
        label_areas: dict[str, list[float]] = {}
        label_covers: dict[str, bool] = {}
        for label, raw_info in labels.items():
            info = _label_info(raw_info)
            dconfig[label] = info.get(CONF_CONFIDENCE, base_confidence)
            if label_area := info.get(CONF_AREA):
                label_areas[label] = [
                    label_area[CONF_TOP],
                    label_area[CONF_LEFT],
                    label_area[CONF_BOTTOM],
                    label_area[CONF_RIGHT],
                ]
                label_covers[label] = label_area[CONF_COVERS]
        self._dconfig: dict[str, float] = dconfig or {"*": base_confidence}
        self._label_areas = label_areas
        self._label_covers = label_covers

        self._area = [0.0, 0.0, 1.0, 1.0]
        self._covers = True
        if area_config := camera_config.get(CONF_AREA):
            self._area = [
                area_config[CONF_TOP],
                area_config[CONF_LEFT],
                area_config[CONF_BOTTOM],
                area_config[CONF_RIGHT],
            ]
            self._covers = area_config[CONF_COVERS]

        file_out = camera_config.get(CONF_FILE_OUT) or ""
        self._file_out: list[template.Template] = (
            [template.Template(file_out, hass)] if file_out else []
        )

        self._scan_interval = camera_config.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        self._remove_interval: Callable[[], None] | None = None

        self._matches: dict[str, list[dict[str, Any]]] = {}
        self._total_matches = 0
        self._last_image: bytes | None = None
        self._process_time = 0.0

    @override
    async def async_added_to_hass(self) -> None:
        """Start this profile's own polling timer."""
        await super().async_added_to_hass()
        self._remove_interval = async_track_time_interval(
            self.hass,
            self._async_scheduled_update,
            timedelta(seconds=self._scan_interval),
        )
        # Poll once immediately rather than waiting a full interval for the
        # first result, matching the old YAML platform's behaviour.
        await self.async_update_ha_state(force_refresh=True)

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Stop this profile's polling timer."""
        if self._remove_interval is not None:
            self._remove_interval()
            self._remove_interval = None

    async def _async_scheduled_update(self, _now: Any) -> None:
        """Poll on this profile's own scan_interval."""
        await self.async_update_ha_state(force_refresh=True)

    @property
    @override
    def state(self) -> int:
        """Return the state of the entity."""
        return self._total_matches

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return device specific state attributes."""
        return {
            ATTR_MATCHES: self._matches,
            ATTR_SUMMARY: {
                label: len(values) for label, values in self._matches.items()
            },
            ATTR_TOTAL_MATCHES: self._total_matches,
            ATTR_PROCESS_TIME: self._process_time,
        }

    def _save_image(
        self,
        image: bytes,
        matches: dict[str, list[dict[str, Any]]],
        paths: list[str],
    ) -> None:
        img = Image.open(io.BytesIO(bytearray(image))).convert("RGB")
        img_width, img_height = img.size
        draw = ImageDraw.Draw(img)

        if self._area != [0, 0, 1, 1]:
            draw_box(
                draw, self._area, img_width, img_height, "Detection Area", (0, 255, 255)
            )

        for label, values in matches.items():
            if label_area := self._label_areas.get(label):
                draw_box(
                    draw,
                    label_area,
                    img_width,
                    img_height,
                    f"{label.capitalize()} Detection Area",
                    (0, 255, 0),
                )
            for instance in values:
                box_label = f"{label} {instance['score']:.1f}%"
                draw_box(
                    draw,
                    instance["box"],
                    img_width,
                    img_height,
                    box_label,
                    (255, 255, 0),
                )

        for path in paths:
            _LOGGER.debug("Saving results image to %s", path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            img.save(path)

    @override
    def process_image(self, image: bytes) -> None:
        """Process the image."""
        try:
            img = Image.open(io.BytesIO(bytearray(image))).convert("RGB")
        except UnidentifiedImageError:
            _LOGGER.warning("Unable to process image, bad data")
            return
        img_width, img_height = img.size

        if self._aspect and abs((img_width / img_height) - self._aspect) > 0.1:
            _LOGGER.debug(
                (
                    "The image aspect: %s and the detector aspect: %s differ by more"
                    " than 0.1"
                ),
                (img_width / img_height),
                self._aspect,
            )

        start = time.monotonic()
        response = self._doods.detect(
            image, dconfig=self._dconfig, detector_name=self._detector_name
        )
        _LOGGER.debug(
            "doods detect: %s response: %s duration: %s",
            self._dconfig,
            response,
            time.monotonic() - start,
        )

        matches: dict[str, list[dict[str, Any]]] = {}
        total_matches = 0

        if not response or "error" in response:
            if response and "error" in response:
                _LOGGER.error(response["error"])
            self._matches = matches
            self._total_matches = total_matches
            self._process_time = time.monotonic() - start
            return

        for detection in response["detections"]:
            score = detection["confidence"]
            boxes = [
                detection["top"],
                detection["left"],
                detection["bottom"],
                detection["right"],
            ]
            label = detection["label"]

            if "*" not in self._dconfig and label not in self._dconfig:
                continue

            if self._covers:
                if (
                    boxes[0] < self._area[0]
                    or boxes[1] < self._area[1]
                    or boxes[2] > self._area[2]
                    or boxes[3] > self._area[3]
                ):
                    continue
            elif (
                boxes[0] > self._area[2]
                or boxes[1] > self._area[3]
                or boxes[2] < self._area[0]
                or boxes[3] < self._area[1]
            ):
                continue

            # Exclude matches outside this label's own area override, if it
            # has one -- applied in addition to (not instead of) the
            # whole-camera area check above.
            if label_area := self._label_areas.get(label):
                if self._label_covers[label]:
                    if (
                        boxes[0] < label_area[0]
                        or boxes[1] < label_area[1]
                        or boxes[2] > label_area[2]
                        or boxes[3] > label_area[3]
                    ):
                        continue
                elif (
                    boxes[0] > label_area[2]
                    or boxes[1] > label_area[3]
                    or boxes[2] < label_area[0]
                    or boxes[3] < label_area[1]
                ):
                    continue

            matches.setdefault(label, []).append(
                {"score": float(score), "box": boxes}
            )
            total_matches += 1

        if total_matches and self._file_out:
            paths = [
                path_template.render(camera_entity=self.camera_entity)
                for path_template in self._file_out
            ]
            self._save_image(image, matches, paths)

        self._matches = matches
        self._total_matches = total_matches
        self._process_time = time.monotonic() - start
