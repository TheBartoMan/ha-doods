# Add config flow to DOODS integration

## Summary

DOODS (`homeassistant/components/doods`) has only ever been configurable via
`image_processing:` YAML. This adds a UI config flow so it can be set up and
managed entirely from **Settings → Devices & Services**, with existing YAML
imported automatically (once) and full parity with what YAML could already
do: per-camera detection area cropping, per-label confidence overrides, and
annotated snapshot ("file_out") saving.

## Architecture

One config entry represents one DOODS **server + detector** pair (URL, auth
key, timeout, detector name). Each entry holds a list of **camera configs**
in its options — one per analyzed camera — each with its own:

- base confidence, and optional per-label confidence overrides
- a detection area (top/left/bottom/right, and whether a match must be fully
  inside it or just overlap it)
- an optional path to save an annotated snapshot to on each detection

This mirrors how YAML worked (`- platform: doods` blocks were effectively
"per camera" configs sharing a server), and lets one entry own many cameras,
which real-world setups with several cameras per DOODS server need.

## What's new

- **`config_flow.py`**:
  - Initial setup: connect to a server → pick a detector → a repeatable
    "manage cameras" menu (add / edit / remove / finish) where each "add"
    walks through camera → labels → per-label confidence → detection area.
  - **Options flow** reuses the exact same camera-management menu to add,
    edit, or remove cameras on an existing entry later.
  - **YAML import**: each `platform: doods` block imports as one camera.
    Multiple blocks that share the same server URL and detector are merged
    into a single config entry (matching multi-camera setups). A repair
    issue points at removing the YAML; per Home Assistant's deprecation
    policy the `PLATFORM_SCHEMA`/`async_setup_platform` import path is
    scheduled for removal in `2026.11.0`.
- **`__init__.py`** sets up a config entry (`async_setup_entry` /
  `async_unload_entry`), stores the `PyDOODS` client + resolved detector on
  `entry.runtime_data`, and forwards to the `image_processing` platform.
- **`image_processing.py`**: `async_setup_entry` builds one `Doods` entity
  per camera config. The entity logic (area filtering, per-label confidence,
  annotated-image saving via PIL) is carried over from the original
  YAML-based implementation essentially unchanged, just reading from a
  per-camera dict instead of the merged YAML config.
- **`manifest.json`** — `"config_flow": true`; dropped the `legacy`
  `quality_scale` marker since that's reserved for YAML-only integrations.

## UI trade-off worth knowing about

Editing a camera in the UI (config flow or options flow) removes it and
re-enters the "add camera" flow from scratch — it does **not** pre-fill the
form with the camera's current labels/confidence/area/file_out. This keeps
the flow implementation a manageable size. Nothing is silently dropped
(all settings are always explicit, editable, and stored), it's just not a
prefill-and-tweak experience yet. A follow-up could add pre-filled editing.

## Files

```
homeassistant/components/doods/
  __init__.py          (rewritten: config entry setup/unload)
  config_flow.py        (new)
  const.py               (new)
  image_processing.py   (rewritten: async_setup_entry + per-camera entity)
  manifest.json           (config_flow: true)
  strings.json            (new)
  translations/en.json    (new)
tests/components/doods/
  __init__.py
  conftest.py
  test_config_flow.py
  test_init.py
```

## Testing

`tests/components/doods/test_config_flow.py` covers: the full add-camera
flow (labels, per-label confidence loop, area, menu, finish), connection
errors, duplicate-camera rejection, remove/edit camera, duplicate
server+detector abort, YAML import (including multi-block merging into one
entry), and the options flow adding a second camera to an existing entry.
`test_init.py` covers entry setup/unload/retry.

**Not yet run against the real Home Assistant test harness** — this was
built outside a home-assistant/core checkout (no `tests.common` / `hass`
fixtures available in this environment). Before opening the PR, drop these
files into a `home-assistant/core` clone and run:

```bash
python3 -m script.hassfest
pytest tests/components/doods --cov=homeassistant.components.doods --cov-report=term-missing
ruff check homeassistant/components/doods tests/components/doods
```

Config flow files need 100% test coverage to be merged into core — the
included tests aim for that but should be checked against the coverage
report above.

## Also worth doing before submitting upstream

- Add yourself to `codeowners` in `manifest.json` (currently empty) — HA
  generally wants an active maintainer once a component gains a config flow.
- Confirm the `breaks_in_ha_version: "2026.11.0"` deprecation target against
  whatever the current release schedule is when you open the PR.
- Consider whether `quality_scale.yaml` (the newer structured quality-scale
  system) should be filled in now or left for a later PR.
- Consider adding pre-filled camera editing (see trade-off above) as a
  follow-up.
