# Android App Graph

A Python CLI that explores an Android app breadth-first and builds a visual state-transition graph. It combines ADB, UIAutomator XML, screenshot pHash + SSIM state matching, and SQLite persistence.

> **Safety:** Run against a dedicated emulator and disposable test account. This tool taps visible controls, scrolls, and presses Back. It filters common destructive/financial labels, but text-based filtering cannot recognize every icon or workflow. Never use it against production accounts or apps where actions can have irreversible side effects.

## Requirements

- Python 3.10+
- ADB / Android SDK platform-tools
- An authorized Android emulator or device
- The target app already installed, or an APK to install

Install dependencies and the CLI:

```bash
uv sync --extra dev
source .venv/bin/activate
```

List connected devices:

```bash
adb devices -l
app-graph devices
```

## Explore an app

For an APK:

```bash
app-graph explore --package com.example.app --apk ./app.apk \
  --output ./runs/example --max-depth 5 --max-states 100
```

For an app already installed on the device, omit `--apk`:

```bash
app-graph explore --package com.example.app --output ./runs/example
```

The package ID is required either way. Use `--device SERIAL` if multiple ADB devices are connected. The explorer installs an optional APK, resets the app task to its launcher/root page, captures the root, and explores clickable UIAutomator nodes, one swipe up/down, and Back per state. Each action branch is replayed from a cleared app task and verified against the known BFS path before interaction.

Main options:

- `--max-depth 5`: maximum BFS depth. Actions are collected at the limit but not executed.
- `--max-states 100`: hard node cap; known states can still be connected, but transitions to unseen pages are omitted after the cap.
- `--max-actions-per-state 80`: maximum attempted controls/gestures per state.
- `--settle-seconds 0.6`: wait after each gesture; increase for slow/network-backed apps.
- `--ready-timeout 60`: seconds to wait for a usable screen after launch/replay. Increase for long startup ads; dynamic home pages are accepted after a short settle once clickable controls are visible.
- `--phash-distance 8 --ssim-threshold 0.90`: visual-clustering tuning. A state match must pass both; a higher SSIM threshold merges fewer pages.
- `--structure-threshold 0.90`: fallback state matching by UI-hierarchy skeleton (resource-id/class node paths, ignoring text/bounds), used when pixels differ because a page rotates banners/search hints/lists on every visit.
- `--keep-app-state`: skip `am force-stop` between branches while still clearing the task stack back to the launcher. This can preserve process/session memory but is less deterministic.
- `--navigation-only`: conservative smoke-test profile that taps only labeled navigation/dismiss controls and skips media/content controls. It reduces coverage and is still not a guarantee of harmless behavior.
- `--seed-activities`: parse the installed APK's manifest, open every exported activity directly as an extra depth-0 root, and explore it. This reaches deep-link/entry pages that tapping from the home page cannot. Activities that need extras may show a blank/error page and are skipped when they leave the app or fail to launch.

## Outputs

```text
runs/example/
├── graph.db                 # SQLite source of truth
├── graph.json               # States, transitions, and visual variants
├── graph.dot                # Graphviz source
├── graph.html               # Offline graph viewer
└── states/
    ├── 0001/
    │   ├── screenshot.png   # Canonical image for State 1
    │   ├── ui.xml           # Canonical UIAutomator hierarchy
    │   └── variants/        # Images/XML clustered into this state
    └── .working/            # Temporary capture area; files are removed as processed
```

Open `graph.html` in a browser to view the force-directed screenshot graph: drag to pan, wheel/buttons to zoom, and click any node or edge for details (activity, visits, variants, transitions, action fields). The HTML is offline-capable; keep the sibling `states/` directory next to it so screenshots load. JSON paths are relative to the output directory. Regenerate exports from an existing database with:

```bash
app-graph export --output ./runs/example
```

The database is bound to the requested package; use a different output directory when exploring a different app. Existing graph data is retained and unattempted actions can be resumed after interruption. SQLite is the source of truth; on Ctrl-C run `app-graph export` to regenerate JSON, DOT, and HTML.

## How state matching works

A state is a visual cluster, not an Activity or exact screenshot. The matcher crops status/navigation bars, computes a 64-bit DCT pHash as a fast candidate filter, then uses local-window SSIM to decide if two screenshots match. Every matched screenshot and XML hierarchy is saved as a state variant; visit counts, actions, depths, and graph edges are kept in SQLite.

## Limitations

- Route replay assumes navigation is deterministic from a fresh launch. Login gates, random content, permissions, session state, deep-link-only pages, or backend changes can reduce coverage.
- The explorer does not type into fields, long-press, grant permissions, or use OCR/LLMs.
- System/app-switch transitions are treated as self-loops rather than included as app states.
- Text safety filtering is best-effort. Unlabeled/icon-only controls are still tapped, which is why an isolated emulator is essential.
- pHash and SSIM thresholds need tuning per app. Visual-only clustering can merge pages that are structurally different but look alike, or split pages whose data changes substantially.

## Development

```bash
uv run pytest
```

## Browser UI

Start the local control panel:

```bash
app-graph web
```

Then open `http://127.0.0.1:8765` (or the port you pass with `--port`). The page lets you configure the package/APK, select an authorized device, set BFS limits and matcher thresholds, and view live logs, the screenshot-backed graph, and labeled transitions. It binds to localhost only. Do not expose it through a network proxy: its API can install APKs and control the connected Android device.
