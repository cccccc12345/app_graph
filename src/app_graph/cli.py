"""Command line interface for app-graph."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .database import GraphDatabase
from .device import AndroidDevice, DeviceError
from .explorer import Explorer, ExplorerConfig
from .export import export_graph
from .webapp import serve


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app-graph",
        description="Explore an Android app breadth-first and export its UI state graph.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    explore = subparsers.add_parser("explore", help="Explore an installed app on a connected device")
    explore.add_argument("--package", required=True, help="Android application id contained in the APK")
    explore.add_argument("--apk", type=Path, help="Optional APK to install first (otherwise the package must already be installed)")
    explore.add_argument("--output", type=Path, default=Path("app-graph-output"), help="Output directory")
    explore.add_argument("--device", help="ADB device serial (uses the only connected device by default)")
    explore.add_argument("--max-depth", type=int, default=5)
    explore.add_argument("--max-states", type=int, default=100)
    explore.add_argument("--max-actions-per-state", type=int, default=80)
    explore.add_argument("--settle-seconds", type=float, default=0.6)
    explore.add_argument(
        "--ready-timeout",
        type=float,
        default=60.0,
        help="Seconds to wait for a stable clickable screen after launch/replay (default: 60)",
    )
    explore.add_argument("--phash-distance", type=int, default=8)
    explore.add_argument("--ssim-threshold", type=float, default=0.90)
    explore.add_argument(
        "--structure-threshold",
        type=float,
        default=0.90,
        help="Minimum UI-hierarchy structure overlap for a non-visual state match (default: 0.90)",
    )
    explore.add_argument("--keep-app-state", action="store_true", help="Do not force-stop app before replaying each path")
    explore.add_argument(
        "--navigation-only",
        action="store_true",
        help="Skip large content regions and tiny controls (safer, but may reduce coverage)",
    )
    explore.add_argument(
        "--seed-activities",
        action="store_true",
        help="Open every exported activity from the APK manifest as an extra root and explore it",
    )

    export = subparsers.add_parser("export", help="Regenerate graph files from an existing graph.db")
    export.add_argument("--output", type=Path, default=Path("app-graph-output"))

    subparsers.add_parser("devices", help="List ADB devices")
    web = subparsers.add_parser("web", help="Start the local browser UI")
    web.add_argument("--port", type=int, default=8765, help="Port to bind on localhost (default: 8765; use 0 for an available port)")
    return parser


def _select_device(serial: str | None) -> AndroidDevice:
    device = AndroidDevice(serial=serial)
    if serial:
        device.ensure_device()
        return device
    devices = device.list_devices()
    if len(devices) != 1:
        if not devices:
            raise DeviceError("No authorized Android device found. Start an emulator or connect a device, then run `adb devices -l`.")
        listed = ", ".join(item["serial"] for item in devices)
        raise DeviceError(f"Multiple Android devices found ({listed}); specify one with --device.")
    return AndroidDevice(serial=devices[0]["serial"])


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        if args.command == "devices":
            device = AndroidDevice()
            for item in device.list_devices():
                print("\t".join(f"{key}={value}" for key, value in item.items()))
            return 0

        if args.command == "web":
            serve(args.port)
            return 0

        if args.command == "export":
            db_path = args.output / "graph.db"
            if not db_path.exists():
                raise FileNotFoundError(f"Graph database not found: {db_path}")
            db = GraphDatabase(db_path)
            try:
                paths = export_graph(db, args.output)
                states, edges = db.counts()
            finally:
                db.close()
            print(f"Exported {states} states and {edges} transitions:")
            for kind, path in paths.items():
                print(f"  {kind}: {path}")
            return 0

        device = _select_device(args.device)
        if args.apk:
            device.install(args.apk)
        config = ExplorerConfig(
            package=args.package,
            output_dir=args.output,
            max_depth=args.max_depth,
            max_states=args.max_states,
            max_actions_per_state=args.max_actions_per_state,
            settle_seconds=args.settle_seconds,
            ready_timeout_seconds=args.ready_timeout,
            phash_distance=args.phash_distance,
            ssim_threshold=args.ssim_threshold,
            structure_threshold=args.structure_threshold,
            force_stop_before_replay=not args.keep_app_state,
            navigation_only=args.navigation_only,
            seed_activities=args.seed_activities,
        )
        explorer = Explorer(device, config)
        try:
            states, edges = explorer.run()
            paths = export_graph(explorer.db, args.output)
        finally:
            explorer.close()
        print(f"\nExploration complete: {states} states, {edges} transitions.")
        for kind, path in paths.items():
            print(f"  {kind}: {path}")
        print(f"  database: {(args.output / 'graph.db').resolve()}")
        return 0
    except (DeviceError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped by user. Captured data is retained in the output directory.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
