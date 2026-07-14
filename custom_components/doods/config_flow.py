"""Config flow for the DOODS integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
import uuid

from pydoods import PyDOODS
import voluptuous as vol

from homeassistant.components.image_processing import CONF_CONFIDENCE
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE,
    CONF_TIMEOUT,
    CONF_URL,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
)

from .const import (
    CONF_AREA,
    CONF_AUTH_KEY,
    CONF_BOTTOM,
    CONF_CAMERAS,
    CONF_COVERS,
    CONF_DETECTOR,
    CONF_FILE_OUT,
    CONF_LABELS,
    CONF_LEFT,
    CONF_PROFILE_ID,
    CONF_RESTRICT_AREA,
    CONF_RIGHT,
    CONF_TOP,
    DEFAULT_CONFIDENCE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class CannotConnect(Exception):
    """Error to indicate we cannot connect to the DOODS server."""


def _get_detectors_sync(url: str, auth_key: str, timeout: int) -> Any:
    """Create a PyDOODS client and fetch its detectors (blocking)."""
    doods = PyDOODS(url, auth_key, timeout)
    return doods.get_detectors()


async def async_get_detectors(
    hass: HomeAssistant, url: str, auth_key: str, timeout: int
) -> list[dict[str, Any]]:
    """Connect to a DOODS server and return its available detectors.

    PyDOODS's constructor itself makes a blocking network call (it calls
    get_detectors() internally), so both the construction and the call must
    happen in the executor, not just the call.
    """
    response = await hass.async_add_executor_job(
        _get_detectors_sync, url, auth_key, timeout
    )
    if not isinstance(response, dict) or "detectors" not in response:
        raise CannotConnect
    return response["detectors"]


def _label_options(detector: dict[str, Any]) -> list[SelectOptionDict]:
    """Build the list of labels the selected detector supports."""
    return [
        SelectOptionDict(value=label, label=label)
        for label in detector.get("labels", [])
    ]


def _confidence_selector() -> NumberSelector:
    """Selector for a 0-100% confidence value."""
    return NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=100,
            step=1,
            mode=NumberSelectorMode.SLIDER,
            unit_of_measurement="%",
        )
    )


def _fraction_selector() -> NumberSelector:
    """Selector for a 0-1 fraction of the image."""
    return NumberSelector(
        NumberSelectorConfig(min=0, max=1, step=0.01, mode=NumberSelectorMode.BOX)
    )


def _scan_interval_selector() -> NumberSelector:
    """Selector for a per-profile polling interval, in seconds."""
    return NumberSelector(
        NumberSelectorConfig(
            min=1,
            max=86400,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )


def _label_info(value: Any) -> dict[str, Any]:
    """Normalize a stored CONF_LABELS entry to {confidence, area} form.

    Profiles saved before per-label areas existed stored a plain float
    (just the confidence) per label. Reading both shapes here means older
    config entries keep working without needing a re-import.
    """
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {CONF_CONFIDENCE: value}


def _camera_profile_options(cameras: list[dict[str, Any]]) -> list[SelectOptionDict]:
    """Build edit/remove options, one per profile.

    A camera entity can have more than one detection profile (e.g. a
    general-purpose profile and a separate one cropped to just a corner of
    the frame), so profiles are labelled with their camera entity plus a
    "(N)" suffix whenever more than one profile shares that entity.
    """
    seen: dict[str, int] = {}
    options: list[SelectOptionDict] = []
    for camera in cameras:
        entity_id = camera[CONF_ENTITY_ID]
        seen[entity_id] = seen.get(entity_id, 0) + 1
    counters: dict[str, int] = {}
    for camera in cameras:
        entity_id = camera[CONF_ENTITY_ID]
        counters[entity_id] = counters.get(entity_id, 0) + 1
        label = (
            entity_id
            if seen[entity_id] == 1
            else f"{entity_id} ({counters[entity_id]})"
        )
        options.append(
            SelectOptionDict(value=camera[CONF_PROFILE_ID], label=label)
        )
    return options


def _import_lock(hass: HomeAssistant) -> asyncio.Lock:
    """Serialize concurrent YAML imports so merging cameras can't race.

    Multiple `platform: doods` blocks that share a server+detector trigger
    several import flows nearly simultaneously at startup. Without this,
    they can race reading/writing the same config entry's options and lose
    all but one camera.
    """
    key = f"{DOMAIN}_import_lock"
    if key not in hass.data:
        hass.data[key] = asyncio.Lock()
    return hass.data[key]


class _CameraStepsMixin:
    """Shared add/edit/remove-camera steps for the config and options flows.

    Expects the class using this mixin to set ``self._detector`` and
    ``self._cameras`` before entering ``async_step_manage``, and to
    implement ``_async_finish()``.
    """

    hass: HomeAssistant
    _url: str
    _detector: dict[str, Any]
    _cameras: list[dict[str, Any]]
    _camera_draft: dict[str, Any]
    _label_queue: list[str]
    _base_confidence: float
    # Set while editing an existing profile (None when adding a fresh one).
    # Lets add_camera/label_confidence/area pre-fill their forms with the
    # profile's current settings, and tells the area step to replace that
    # profile in place (keeping its id, so its entity stays the same)
    # instead of appending a new one.
    _editing_profile_id: str | None
    # Raw CONF_LABELS values (float or {confidence, area} dict -- see
    # _label_info()) from the profile being edited, keyed by label name.
    _existing_labels: dict[str, Any]
    _existing_area: dict[str, Any] | None
    # Number of labels selected on the add_camera step, used only to show
    # "step X of Y" in later steps' descriptions (every step's button says
    # "Submit" -- HA doesn't support per-step "Next" vs "Submit" labels --
    # so this is the only way to signal "this isn't the last screen yet").
    _label_total: int

    def _async_finish(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def async_step_manage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu: add, edit or remove a camera, or finish."""
        menu_options = ["add_camera"]
        if self._cameras:
            menu_options += ["edit_camera", "remove_camera", "finish"]
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id="manage",
            menu_options=menu_options,
            description_placeholders={
                "server": self._url,
                "detector": self._detector.get("name", ""),
            },
        )

    async def async_step_add_camera(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a camera, which labels to detect, base confidence and snapshot path.

        A camera entity can be added more than once -- each addition is a
        separate detection profile (its own labels/confidence/area/
        file_out), useful for e.g. running one broad profile and one
        cropped to a specific zone off the same feed.
        """
        # If we're editing, self._camera_draft/_existing_labels
        # were pre-loaded by async_step_edit_camera with the profile's
        # current settings, to use as this form's defaults below.
        existing = self._camera_draft

        if user_input is not None:
            self._camera_draft = {
                CONF_PROFILE_ID: self._editing_profile_id or uuid.uuid4().hex,
                CONF_ENTITY_ID: user_input[CONF_ENTITY_ID],
                CONF_CONFIDENCE: user_input[CONF_CONFIDENCE],
                CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                CONF_FILE_OUT: user_input.get(CONF_FILE_OUT, ""),
                CONF_LABELS: {},
            }
            self._base_confidence = user_input[CONF_CONFIDENCE]
            self._label_queue = list(user_input.get(CONF_LABELS, []))
            self._label_total = len(self._label_queue)
            return await self.async_step_label_confidence()

        entity_id_key = (
            vol.Required(CONF_ENTITY_ID, default=existing[CONF_ENTITY_ID])
            if existing.get(CONF_ENTITY_ID)
            else vol.Required(CONF_ENTITY_ID)
        )
        schema = vol.Schema(
            {
                entity_id_key: EntitySelector(EntitySelectorConfig(domain="camera")),
                vol.Optional(
                    CONF_LABELS,
                    default=list(self._existing_labels),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=_label_options(self._detector), multiple=True
                    )
                ),
                vol.Optional(
                    CONF_CONFIDENCE,
                    default=existing.get(CONF_CONFIDENCE, DEFAULT_CONFIDENCE),
                ): _confidence_selector(),
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=existing.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): _scan_interval_selector(),
                vol.Optional(
                    CONF_FILE_OUT, default=existing.get(CONF_FILE_OUT, "")
                ): TextSelector(),
            }
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="add_camera",
            data_schema=schema,
            description_placeholders={
                "action": "Edit" if self._editing_profile_id else "Add",
                "detector": self._detector.get("name", ""),
            },
        )

    async def async_step_label_confidence(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a confidence override, and optional area, one label at a time.

        The per-label area is separate from (and filtered in addition to)
        the whole-camera area asked about in async_step_area -- e.g. count
        "car" only in the driveway corner of the frame, but count "person"
        anywhere within the camera's own overall area.
        """
        if user_input is not None and self._label_queue:
            label = self._label_queue.pop(0)
            label_info: dict[str, Any] = {
                CONF_CONFIDENCE: user_input[CONF_CONFIDENCE]
            }
            if user_input.get(CONF_RESTRICT_AREA):
                label_info[CONF_AREA] = {
                    CONF_TOP: user_input[CONF_TOP],
                    CONF_LEFT: user_input[CONF_LEFT],
                    CONF_BOTTOM: user_input[CONF_BOTTOM],
                    CONF_RIGHT: user_input[CONF_RIGHT],
                    CONF_COVERS: user_input[CONF_COVERS],
                }
            self._camera_draft[CONF_LABELS][label] = label_info

        if self._label_queue:
            label = self._label_queue[0]
            existing = _label_info(self._existing_labels.get(label))
            existing_area = existing.get(CONF_AREA) or {}
            default_confidence = existing.get(CONF_CONFIDENCE, self._base_confidence)
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_CONFIDENCE, default=default_confidence
                    ): _confidence_selector(),
                    vol.Optional(
                        CONF_RESTRICT_AREA,
                        default=bool(existing.get(CONF_AREA)),
                    ): BooleanSelector(),
                    vol.Optional(
                        CONF_TOP, default=existing_area.get(CONF_TOP, 0.0)
                    ): _fraction_selector(),
                    vol.Optional(
                        CONF_LEFT, default=existing_area.get(CONF_LEFT, 0.0)
                    ): _fraction_selector(),
                    vol.Optional(
                        CONF_BOTTOM, default=existing_area.get(CONF_BOTTOM, 1.0)
                    ): _fraction_selector(),
                    vol.Optional(
                        CONF_RIGHT, default=existing_area.get(CONF_RIGHT, 1.0)
                    ): _fraction_selector(),
                    vol.Optional(
                        CONF_COVERS, default=existing_area.get(CONF_COVERS, True)
                    ): BooleanSelector(),
                }
            )
            # Step 1 was add_camera; steps 2..(_label_total+1) are one per
            # label; step (_label_total+2) is area. This label is next in
            # line, i.e. (labels already done) + 2.
            step = self._label_total - len(self._label_queue) + 2
            total = self._label_total + 2
            return self.async_show_form(  # type: ignore[attr-defined]
                step_id="label_confidence",
                data_schema=schema,
                description_placeholders={
                    "label": label,
                    "step": str(step),
                    "total": str(total),
                },
            )

        return await self.async_step_area()

    async def async_step_area(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the detection area to restrict this camera's matches to."""
        if user_input is not None:
            self._camera_draft[CONF_AREA] = {
                CONF_TOP: user_input[CONF_TOP],
                CONF_LEFT: user_input[CONF_LEFT],
                CONF_BOTTOM: user_input[CONF_BOTTOM],
                CONF_RIGHT: user_input[CONF_RIGHT],
                CONF_COVERS: user_input[CONF_COVERS],
            }
            if self._editing_profile_id:
                self._cameras = [
                    c
                    for c in self._cameras
                    if c[CONF_PROFILE_ID] != self._editing_profile_id
                ]
            self._cameras.append(self._camera_draft)
            self._camera_draft = {}
            self._editing_profile_id = None
            self._existing_labels = {}
            self._existing_area = None
            return await self.async_step_manage()

        existing = self._existing_area or {}
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_TOP, default=existing.get(CONF_TOP, 0.0)
                ): _fraction_selector(),
                vol.Optional(
                    CONF_LEFT, default=existing.get(CONF_LEFT, 0.0)
                ): _fraction_selector(),
                vol.Optional(
                    CONF_BOTTOM, default=existing.get(CONF_BOTTOM, 1.0)
                ): _fraction_selector(),
                vol.Optional(
                    CONF_RIGHT, default=existing.get(CONF_RIGHT, 1.0)
                ): _fraction_selector(),
                vol.Optional(
                    CONF_COVERS, default=existing.get(CONF_COVERS, True)
                ): BooleanSelector(),
            }
        )
        total = self._label_total + 2
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="area",
            data_schema=schema,
            description_placeholders={"step": str(total), "total": str(total)},
        )

    async def async_step_edit_camera(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a configured camera profile and load it for editing.

        Pre-fills the add-camera/label-confidence/area forms with this
        profile's current settings; saving replaces it in place (same
        profile id, so its entity is unaffected) instead of adding a new
        one.
        """
        if user_input is not None:
            profile_id = user_input[CONF_PROFILE_ID]
            camera = next(
                c for c in self._cameras if c[CONF_PROFILE_ID] == profile_id
            )
            self._editing_profile_id = profile_id
            self._existing_labels = dict(camera.get(CONF_LABELS) or {})
            self._existing_area = camera.get(CONF_AREA)
            self._camera_draft = {
                CONF_ENTITY_ID: camera[CONF_ENTITY_ID],
                CONF_CONFIDENCE: camera[CONF_CONFIDENCE],
                CONF_SCAN_INTERVAL: camera.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                ),
                CONF_FILE_OUT: camera.get(CONF_FILE_OUT, ""),
            }
            return await self.async_step_add_camera()

        schema = vol.Schema(
            {
                vol.Required(CONF_PROFILE_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=_camera_profile_options(self._cameras)
                    )
                )
            }
        )
        return self.async_show_form(step_id="edit_camera", data_schema=schema)  # type: ignore[attr-defined]

    async def async_step_remove_camera(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a configured camera profile to remove."""
        if user_input is not None:
            profile_id = user_input[CONF_PROFILE_ID]
            self._cameras = [
                c for c in self._cameras if c[CONF_PROFILE_ID] != profile_id
            ]
            return await self.async_step_manage()

        schema = vol.Schema(
            {
                vol.Required(CONF_PROFILE_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=_camera_profile_options(self._cameras)
                    )
                )
            }
        )
        return self.async_show_form(step_id="remove_camera", data_schema=schema)  # type: ignore[attr-defined]

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save the entry."""
        return self._async_finish()


class DoodsConfigFlow(ConfigFlow, _CameraStepsMixin, domain=DOMAIN):
    """Handle a config flow for DOODS."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the DOODS config flow."""
        self._url: str = ""
        self._auth_key: str = ""
        self._timeout: int = DEFAULT_TIMEOUT
        self._detectors: list[dict[str, Any]] = []
        self._detector: dict[str, Any] = {}
        self._cameras: list[dict[str, Any]] = []
        self._camera_draft: dict[str, Any] = {}
        self._label_queue: list[str] = []
        self._base_confidence: float = DEFAULT_CONFIDENCE
        self._editing_profile_id: str | None = None
        self._existing_labels: dict[str, Any] = {}
        self._existing_area: dict[str, Any] | None = None
        self._label_total: int = 0

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the DOODS server connection details."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._url = user_input[CONF_URL]
            self._auth_key = user_input.get(CONF_AUTH_KEY, "")
            self._timeout = user_input[CONF_TIMEOUT]
            try:
                self._detectors = await async_get_detectors(
                    self.hass, self._url, self._auth_key, self._timeout
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                if not self._detectors:
                    errors["base"] = "no_detectors"
                else:
                    return await self.async_step_detector()

        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=self._url): str,
                vol.Optional(CONF_AUTH_KEY, default=self._auth_key): str,
                vol.Required(CONF_TIMEOUT, default=self._timeout): int,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_detector(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick which detector on the server to use."""
        if user_input is not None:
            detector_name = user_input[CONF_DETECTOR]
            await self.async_set_unique_id(f"{self._url}_{detector_name}")
            self._abort_if_unique_id_configured()
            self._detector = next(
                d for d in self._detectors if d["name"] == detector_name
            )
            return await self.async_step_manage()

        options = [
            SelectOptionDict(value=d["name"], label=d["name"])
            for d in self._detectors
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_DETECTOR): SelectSelector(
                    SelectSelectorConfig(options=options)
                )
            }
        )
        return self.async_show_form(step_id="detector", data_schema=schema)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the DOODS server connection (URL, auth key, timeout).

        This is what "Reconfigure" on the integration's own menu runs --
        it's the only place the server connection details (and, read-only,
        which detector this entry uses) are visible or editable after
        initial setup. The detector itself can't be changed here: it's
        baked into which cameras/labels are valid and into how this entry
        merges with re-imported YAML, so switching detectors means
        removing and re-adding the integration instead.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL]
            auth_key = user_input.get(CONF_AUTH_KEY, "")
            timeout = user_input[CONF_TIMEOUT]
            detector_name = entry.data[CONF_DETECTOR]
            try:
                detectors = await async_get_detectors(
                    self.hass, url, auth_key, timeout
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                if not any(d["name"] == detector_name for d in detectors):
                    errors["base"] = "detector_not_found"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data={
                            CONF_URL: url,
                            CONF_AUTH_KEY: auth_key,
                            CONF_TIMEOUT: timeout,
                            CONF_DETECTOR: detector_name,
                        },
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=entry.data[CONF_URL]): str,
                vol.Optional(
                    CONF_AUTH_KEY, default=entry.data.get(CONF_AUTH_KEY, "")
                ): str,
                vol.Required(CONF_TIMEOUT, default=entry.data[CONF_TIMEOUT]): int,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
            description_placeholders={"detector": entry.data[CONF_DETECTOR]},
        )

    async def async_step_import(
        self, import_config: dict[str, Any]
    ) -> ConfigFlowResult:
        """Import a legacy YAML `image_processing: - platform: doods` entry.

        Each YAML block becomes one camera. Multiple YAML blocks that share
        the same server URL and detector are merged into a single config
        entry, matching how one DOODS server/detector pair is now
        represented in the UI.
        """
        url = import_config[CONF_URL]
        auth_key = import_config.get(CONF_AUTH_KEY, "")
        timeout = import_config.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        detector_name = import_config[CONF_DETECTOR]

        sources = import_config.get(CONF_SOURCE, [])
        if not sources:
            return self.async_abort(reason="no_cameras")

        try:
            detectors = await async_get_detectors(self.hass, url, auth_key, timeout)
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")
        if not any(d["name"] == detector_name for d in detectors):
            return self.async_abort(reason="detector_not_found")

        base_confidence = import_config.get(CONF_CONFIDENCE, DEFAULT_CONFIDENCE)
        label_confidences: dict[str, dict[str, Any]] = {}
        for label in import_config.get(CONF_LABELS, []):
            if isinstance(label, str):
                label_confidences[label] = {CONF_CONFIDENCE: base_confidence}
                continue
            label_info: dict[str, Any] = {
                CONF_CONFIDENCE: label.get(CONF_CONFIDENCE, base_confidence)
            }
            # Per-label area: e.g. only count "car" within a specific
            # corner of the frame, separate from (and in addition to) the
            # camera's own overall area below.
            if label_area_cfg := label.get(CONF_AREA):
                label_info[CONF_AREA] = {
                    CONF_TOP: label_area_cfg[CONF_TOP],
                    CONF_LEFT: label_area_cfg[CONF_LEFT],
                    CONF_BOTTOM: label_area_cfg[CONF_BOTTOM],
                    CONF_RIGHT: label_area_cfg[CONF_RIGHT],
                    CONF_COVERS: label_area_cfg[CONF_COVERS],
                }
            label_confidences[label[CONF_NAME]] = label_info

        area = None
        if area_cfg := import_config.get(CONF_AREA):
            area = {
                CONF_TOP: area_cfg[CONF_TOP],
                CONF_LEFT: area_cfg[CONF_LEFT],
                CONF_BOTTOM: area_cfg[CONF_BOTTOM],
                CONF_RIGHT: area_cfg[CONF_RIGHT],
                CONF_COVERS: area_cfg[CONF_COVERS],
            }

        # cv.template (used by image_processing's legacy PLATFORM_SCHEMA)
        # parses file_out into template.Template objects, not plain
        # strings -- str() on one gives its debug repr
        # ("Template<template=(...) renders=0>"), not the path. Use
        # `.template` to get the original source string back.
        file_outs = import_config.get(CONF_FILE_OUT) or []
        file_out = file_outs[0].template if file_outs else ""

        # image_processing.PLATFORM_SCHEMA parses YAML's scan_interval (if
        # set) into a timedelta via cv.time_period. Store it in the same
        # plain-seconds-int form the UI's scan_interval field uses, falling
        # back to DEFAULT_SCAN_INTERVAL for blocks that didn't set one --
        # matching image_processing's own default poll interval, so
        # behaviour is unchanged for anyone who didn't customise it.
        raw_scan_interval = import_config.get(CONF_SCAN_INTERVAL)
        scan_interval = (
            int(raw_scan_interval.total_seconds())
            if raw_scan_interval
            else DEFAULT_SCAN_INTERVAL
        )

        # A stable (not random) profile id per YAML block+source, derived
        # from the block's position among blocks sharing this server and
        # detector (assigned in image_processing.async_setup_platform,
        # before any of these import flows race off). This lets re-imports
        # on every restart update the *same* profile instead of duplicating
        # it, even when several blocks share one camera entity (e.g. one
        # general-purpose profile and one cropped to a specific zone).
        block_index = import_config.get("_block_index", 0)
        new_cameras = [
            {
                CONF_PROFILE_ID: f"yaml_{source[CONF_ENTITY_ID]}_{block_index}_{i}",
                CONF_ENTITY_ID: source[CONF_ENTITY_ID],
                CONF_CONFIDENCE: base_confidence,
                CONF_SCAN_INTERVAL: scan_interval,
                CONF_LABELS: label_confidences,
                CONF_AREA: area,
                CONF_FILE_OUT: file_out,
            }
            for i, source in enumerate(sources)
        ]
        new_profile_ids = {c[CONF_PROFILE_ID] for c in new_cameras}

        unique_id = f"{url}_{detector_name}"
        async with _import_lock(self.hass):
            # raise_on_progress=False: several `platform: doods` blocks that
            # share this server+detector are importing concurrently right
            # now (one flow per YAML block). We don't want HA to abort us
            # just because a sibling import flow is also in progress with
            # the same unique_id -- we want to merge with it below instead.
            existing_entry = await self.async_set_unique_id(
                unique_id, raise_on_progress=False
            )
            if existing_entry is not None:
                merged = [
                    c
                    for c in existing_entry.options.get(CONF_CAMERAS, [])
                    if c.get(CONF_PROFILE_ID) not in new_profile_ids
                ] + new_cameras
                self.hass.config_entries.async_update_entry(
                    existing_entry,
                    options={**existing_entry.options, CONF_CAMERAS: merged},
                )
                return self.async_abort(reason="already_configured")

            return self.async_create_entry(
                title=f"DOODS ({detector_name})",
                data={
                    CONF_URL: url,
                    CONF_AUTH_KEY: auth_key,
                    CONF_TIMEOUT: timeout,
                    CONF_DETECTOR: detector_name,
                },
                options={CONF_CAMERAS: new_cameras},
            )

    def _async_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=f"DOODS ({self._detector['name']})",
            data={
                CONF_URL: self._url,
                CONF_AUTH_KEY: self._auth_key,
                CONF_TIMEOUT: self._timeout,
                CONF_DETECTOR: self._detector["name"],
            },
            options={CONF_CAMERAS: self._cameras},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DoodsOptionsFlow:
        """Get the options flow for this handler."""
        return DoodsOptionsFlow()


class DoodsOptionsFlow(OptionsFlow, _CameraStepsMixin):
    """Manage DOODS cameras: add, edit, remove, with per-camera settings."""

    def __init__(self) -> None:
        """Initialize the DOODS options flow."""
        self._url: str = ""
        self._detector: dict[str, Any] = {}
        self._cameras: list[dict[str, Any]] = []
        self._camera_draft: dict[str, Any] = {}
        self._label_queue: list[str] = []
        self._base_confidence: float = DEFAULT_CONFIDENCE
        self._editing_profile_id: str | None = None
        self._existing_labels: dict[str, Any] = {}
        self._existing_area: dict[str, Any] | None = None
        self._label_total: int = 0

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Load the current cameras and detector, then enter the manage menu."""
        entry = self.config_entry
        self._url = entry.data[CONF_URL]
        try:
            detectors = await async_get_detectors(
                self.hass,
                entry.data[CONF_URL],
                entry.data[CONF_AUTH_KEY],
                entry.data[CONF_TIMEOUT],
            )
        except CannotConnect:
            detectors = []

        self._detector = next(
            (d for d in detectors if d["name"] == entry.data[CONF_DETECTOR]),
            {"name": entry.data[CONF_DETECTOR], "labels": []},
        )
        self._cameras = [dict(c) for c in entry.options.get(CONF_CAMERAS, [])]
        return await self.async_step_manage()

    def _async_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(data={CONF_CAMERAS: self._cameras})
