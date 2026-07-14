# DOODS for Home Assistant (config-flow fork)

A drop-in replacement for Home Assistant's built-in `doods` (Dedicated Open
Object Detection Service) `image_processing` integration, adding full UI
configuration via a config flow. Set up and manage your DOODS server and
cameras entirely from **Settings → Devices & Services** instead of
`configuration.yaml`.

## Why this exists

The built-in DOODS integration has only ever been configurable via YAML.
This fork adds:

- A config flow for connecting to a DOODS server and picking a detector.
- Add/edit/remove screens for cameras, with per-label confidence overrides,
  a detection area, an optional annotated-snapshot path, and a per-camera
  polling interval.
- Support for multiple independent detection profiles on the *same* camera
  (e.g. one general-purpose profile and a second cropped to a specific
  zone), which a single YAML `platform: doods` block per camera couldn't
  express.
- **Automatic import of your existing YAML.** If you already have
  `platform: doods` entries under `image_processing:`, they're imported
  into the UI automatically on first start after installing -- nothing is
  dropped: cameras, labels, confidence levels, detection areas, snapshot
  paths and scan intervals all carry over exactly as configured.
- A Repair notice reminding you to delete the now-redundant YAML, which
  clears itself automatically once you do.

## Installing

### Via HACS (recommended)

1. In Home Assistant, go to **HACS → Integrations → ⋮ → Custom
   repositories**.
2. Add this repository's URL, category **Integration**.
3. Search for "DOODS" in HACS and install it.
4. Restart Home Assistant.

### Manual

Copy `custom_components/doods` from this repo into your Home Assistant
`config/custom_components/doods` folder, then restart.

## Upgrading from the built-in DOODS integration

This custom component uses the same `doods` domain as the one built into
Home Assistant, so once installed it takes over from the built-in version.
If you already have `image_processing: - platform: doods` entries in
`configuration.yaml`:

1. Install this integration (via HACS or manually, above) and restart.
2. Your existing cameras are imported automatically -- check
   **Settings → Devices & Services → DOODS** to confirm they all came
   through correctly (camera, labels, confidence, area, snapshot path,
   scan interval).
3. A **Repairs** notice will tell you it's now safe to remove the YAML.
   Delete your `platform: doods` blocks from `configuration.yaml` and
   restart once more -- the notice clears itself.

If you're setting DOODS up for the first time, just add the integration
from **Settings → Devices & Services → Add Integration** and skip the YAML
entirely.

## Known limitations

- Not yet submitted upstream to `home-assistant/core`. See
  [`PR_DESCRIPTION.md`](PR_DESCRIPTION.md) for the plan and current gaps if
  you're interested in helping get it there.

## Credit

Based on Home Assistant's built-in `doods` integration
(`homeassistant/components/doods`), licensed under the Apache License 2.0.
