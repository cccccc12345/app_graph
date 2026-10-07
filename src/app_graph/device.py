"""ADB-backed Android device capture and input driver."""

from __future__ import annotations

import re
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


class DeviceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Capture:
    screenshot: Path
    xml: Path
    activity: str
    width: int
    height: int
    xml_ok: bool = True


class AndroidDevice:
    def __init__(self, serial: str | None = None, command_timeout: int = 20):
        self.serial = serial
        self.command_timeout = command_timeout

    def _adb(self, *args: str, binary: bool = False) -> bytes | str:
        command = ["adb"]
        if self.serial:
            command += ["-s", self.serial]
        command.extend(args)
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.command_timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise DeviceError(f"ADB command failed: {' '.join(command)} ({exc})") from exc
        if result.returncode:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise DeviceError(f"ADB command failed ({result.returncode}): {' '.join(command)}: {message}")
        return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")

    def ensure_device(self) -> str:
        output = str(self._adb("get-state")).strip()
        if output != "device":
            raise DeviceError(f"ADB device is not ready (state={output!r}). Run `adb devices -l`. ")
        return str(self._adb("shell", "getprop", "ro.product.model")).strip()

    def list_devices(self) -> list[dict[str, str]]:
        output = str(self._adb("devices", "-l"))
        devices: list[dict[str, str]] = []
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                values = {"serial": parts[0]}
                values.update(
                    dict(part.split(":", 1) for part in parts[2:] if ":" in part)
                )
                devices.append(values)
        return devices

    def launch(self, package: str, clear_task: bool = True) -> None:
        # Waking first keeps captures meaningful after the phone has been idle:
        # screencap returns an all-black frame while the display is off.
        self._adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        # NEW_TASK + CLEAR_TASK gives path replay a stable app launcher/root.
        # Omitting CLEAR_TASK preserves a task when the user opts into the
        # explicitly best-effort --keep-app-state mode.
        flags = "0x10008000" if clear_task else "0x10000000"  # NEW_TASK | CLEAR_TASK
        output = str(
            self._adb(
                "shell",
                "am",
                "start",
                "-W",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                "-f",
                flags,
                "-p",
                package,
            )
        )
        if "Error:" in output or "Exception" in output:
            raise DeviceError(f"Could not launch {package}: {output.strip()}")
        time.sleep(0.5)

    def launch_component(self, component: str, clear_task: bool = True) -> bool:
        """Best-effort direct start of an activity component; False on failure."""
        self._adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        flags = "0x10008000" if clear_task else "0x10000000"
        try:
            output = str(self._adb("shell", "am", "start", "-W", "-n", component, "-f", flags))
        except DeviceError:
            return False
        failure = (
            "Error:" in output
            or "Exception" in output
            or "Permission Denial" in output
            or "does not exist" in output
        )
        if not failure:
            time.sleep(0.5)
        return not failure

    def apk_paths(self, package: str) -> list[str]:
        output = str(self._adb("shell", "pm", "path", package))
        return [
            line.split(":", 1)[1].strip()
            for line in output.splitlines()
            if line.startswith("package:")
        ]

    def read_manifest_axml(self, package: str, cache_path: str | Path) -> bytes | None:
        """Return the binary AndroidManifest.xml of the installed package.

        Prefers streaming ``AndroidManifest.xml`` straight out of the APK via
        the device's ``unzip`` (only the manifest crosses ADB), falling back to
        pulling the base APK and reading the zip entry locally.
        """
        try:
            paths = self.apk_paths(package)
        except DeviceError:
            return None
        if not paths:
            return None
        base = next((path for path in paths if path.endswith("base.apk")), paths[0])
        try:
            data = self._adb("exec-out", "unzip", "-p", base, "AndroidManifest.xml", binary=True)
            if isinstance(data, bytes) and data[:2] == b"\x03\x00":
                return data
        except DeviceError:
            pass
        cache = Path(cache_path)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            self._adb("pull", base, str(cache))
            with zipfile.ZipFile(cache) as archive:
                data = archive.read("AndroidManifest.xml")
            return data if data[:2] == b"\x03\x00" else None
        except (DeviceError, OSError, zipfile.BadZipFile, KeyError):
            return None

    def list_entry_activities(self, package: str) -> list[str]:
        """Fallback enumeration: components in the dumpsys Activity Resolver Table."""
        output = str(self._adb("shell", "dumpsys", "package", package))
        components: list[str] = []
        in_activity_table = False
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.endswith("Resolver Table:"):
                in_activity_table = stripped.startswith("Activity Resolver Table:")
                continue
            if not in_activity_table:
                continue
            match = re.match(r"^[0-9a-f]+ (\S+/\S+) filter", stripped)
            if match:
                components.append(match.group(1))
        seen: dict[str, None] = {}
        for component in components:
            if component.split("/", 1)[0] == package:
                seen.setdefault(component)
        return list(seen)

    def install(self, apk_path: str | Path) -> None:
        apk = Path(apk_path).expanduser().resolve()
        if not apk.is_file():
            raise DeviceError(f"APK file does not exist: {apk}")
        output = str(self._adb("install", "-r", str(apk)))
        if "Success" not in output:
            raise DeviceError(f"APK install failed: {output.strip() or 'unknown adb error'}")

    def force_stop(self, package: str) -> None:
        self._adb("shell", "am", "force-stop", package)

    def tap(self, x: int, y: int) -> None:
        self._adb("shell", "input", "tap", str(x), str(y))

    def swipe(self, x: int, y: int, x2: int, y2: int, duration_ms: int) -> None:
        self._adb(
            "shell", "input", "swipe", str(x), str(y), str(x2), str(y2), str(duration_ms)
        )

    def back(self) -> None:
        self._adb("shell", "input", "keyevent", "KEYCODE_BACK")

    @staticmethod
    def _foreground_activity(output: str) -> str:
        patterns = (
            r"mResumedActivity:.*?\s([\w.$]+/[^\s}]+)",
            r"topResumedActivity=ActivityRecord\{[^}]*?\s([\w.$]+/[\w.$]+)\s+t?\d+\}",
            r"ResumedActivity:.*?\s([\w.$]+/[^\s}]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, output)
            if match:
                return match.group(1)
        return ""

    def capture(self, directory: str | Path, name: str) -> Capture:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        screenshot = directory / f"{name}.png"
        xml_path = directory / f"{name}.xml"

        # Dump the hierarchy before taking the screenshot. UIAutomator can
        # take long enough that a launching app finishes its splash/onboarding
        # transition during the dump. Capturing the image first pairs a stale
        # splash frame with the newer, fully-loaded hierarchy.
        try:
            xml_ok = True
            self._adb("shell", "rm", "-f", "/sdcard/app_graph_window.xml")
            self._adb("shell", "uiautomator", "dump", "/sdcard/app_graph_window.xml")
            xml_data = self._adb("exec-out", "cat", "/sdcard/app_graph_window.xml")
            xml_text = str(xml_data)
            ET.fromstring(xml_text)
        except (DeviceError, ET.ParseError):
            # Animating screens (ads, video, heavy transitions) often make
            # UIAutomator fail to reach an idle state. Mark the capture so the
            # explorer can treat it as transient instead of a real page.
            xml_ok = False
            xml_text = "<hierarchy />"
        xml_path.write_text(xml_text, encoding="utf-8")

        # Take the screenshot immediately after the hierarchy dump, so both
        # artifacts describe the post-transition UI when the app was still
        # settling during UIAutomator's capture.
        image_data = self._adb("exec-out", "screencap", "-p", binary=True)
        assert isinstance(image_data, bytes)
        screenshot.write_bytes(image_data)
        try:
            with Image.open(screenshot) as image:
                width, height = image.size
                image.verify()
        except Exception as exc:
            screenshot.unlink(missing_ok=True)
            xml_path.unlink(missing_ok=True)
            raise DeviceError(f"Could not decode device screenshot: {exc}") from exc

        activity_output = str(self._adb("shell", "dumpsys", "activity", "activities"))
        activity = self._foreground_activity(activity_output)
        return Capture(screenshot, xml_path, activity, width, height, xml_ok)
