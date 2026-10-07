from io import BytesIO
import zipfile

from PIL import Image

from app_graph.device import AndroidDevice, DeviceError


def test_foreground_activity_parsers():
    assert AndroidDevice._foreground_activity(
        "mResumedActivity: ActivityRecord{abc u0 com.example/.Main t2}"
    ) == "com.example/.Main"
    assert AndroidDevice._foreground_activity(
        "topResumedActivity=ActivityRecord{abc u0 com.example/com.example.HomeActivity t2}"
    ) == "com.example/com.example.HomeActivity"
    assert AndroidDevice._foreground_activity("nothing here") == ""


def test_launch_uses_clear_task_flags(monkeypatch):
    device = AndroidDevice(serial="test")
    calls = []
    monkeypatch.setattr(device, "_adb", lambda *args, **kwargs: calls.append(args) or "Status: ok")
    monkeypatch.setattr("app_graph.device.time.sleep", lambda _: None)
    device.launch("com.example")
    assert ("shell", "input", "keyevent", "KEYCODE_WAKEUP") in calls
    start = next(call for call in calls if call[:3] == ("shell", "am", "start"))
    assert start[start.index("-f") + 1] == "0x10008000"
    assert start[start.index("-p") + 1] == "com.example"
    calls.clear()
    device.launch("com.example", clear_task=False)
    start = next(call for call in calls if call[:3] == ("shell", "am", "start"))
    assert start[start.index("-f") + 1] == "0x10000000"


def test_install_existing_apk(monkeypatch, tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"placeholder")
    device = AndroidDevice(serial="test")
    calls = []
    monkeypatch.setattr(device, "_adb", lambda *args, **kwargs: calls.append(args) or "Success")
    device.install(apk)
    assert calls == [("install", "-r", str(apk.resolve()))]


def test_capture_dumps_ui_before_screenshot(monkeypatch, tmp_path):
    image = Image.new("RGB", (12, 24), "white")
    image_buffer = BytesIO()
    image.save(image_buffer, format="PNG")
    calls = []
    device = AndroidDevice(serial="test")

    def fake_adb(*args, binary=False):
        calls.append(args)
        if args[:3] == ("exec-out", "cat", "/sdcard/app_graph_window.xml"):
            return '<hierarchy><node text="Ready" /></hierarchy>'
        if args[:3] == ("exec-out", "screencap", "-p"):
            return image_buffer.getvalue()
        if args[:2] == ("shell", "dumpsys"):
            return "mResumedActivity: ActivityRecord{abc u0 com.example/.Main t2}"
        return ""

    monkeypatch.setattr(device, "_adb", fake_adb)
    capture = device.capture(tmp_path, "screen")

    commands = [call[:3] for call in calls]
    assert commands.index(("exec-out", "cat", "/sdcard/app_graph_window.xml")) < commands.index(
        ("exec-out", "screencap", "-p")
    )
    assert "Ready" in capture.xml.read_text()
    assert capture.activity == "com.example/.Main"
    assert (capture.width, capture.height) == (12, 24)
    assert capture.xml_ok is True


def test_capture_marks_unparsed_hierarchy(monkeypatch, tmp_path):
    image = Image.new("RGB", (12, 24), "white")
    image_buffer = BytesIO()
    image.save(image_buffer, format="PNG")
    device = AndroidDevice(serial="test")

    def fake_adb(*args, binary=False):
        if args[:2] == ("shell", "uiautomator"):
            raise DeviceError("ERROR: could not get idle state")
        if args[:3] == ("exec-out", "screencap", "-p"):
            return image_buffer.getvalue()
        if args[:2] == ("shell", "dumpsys"):
            return "mResumedActivity: ActivityRecord{abc u0 com.example/.Main t2}"
        return ""

    monkeypatch.setattr(device, "_adb", fake_adb)
    capture = device.capture(tmp_path, "screen")

    assert capture.xml_ok is False
    assert capture.xml.read_text(encoding="utf-8") == "<hierarchy />"


def test_launch_component_reports_failure(monkeypatch):
    device = AndroidDevice(serial="test")
    monkeypatch.setattr("app_graph.device.time.sleep", lambda _: None)
    responses = {
        "com.example/.Missing": "Error: Activity class not found",
        "com.example/.Main": "Status: ok",
        "com.example/.Hidden": "Permission Denial: not exported",
    }

    def fake_adb(*args, **kwargs):
        if args[:3] == ("shell", "am", "start"):
            return responses[args[args.index("-n") + 1]]
        return ""

    monkeypatch.setattr(device, "_adb", fake_adb)
    assert device.launch_component("com.example/.Missing") is False
    assert device.launch_component("com.example/.Main") is True
    assert device.launch_component("com.example/.Hidden") is False


def test_list_entry_activities_parses_only_activity_table(monkeypatch):
    device = AndroidDevice(serial="test")
    dumpsys = """
Activity Resolver Table:
  Non-Data Actions:
      android.intent.action.VIEW:
        abc123 com.example/.MainActivity filter 123
          Action: "android.intent.action.VIEW"
        def456 com.other/.OtherActivity filter 456
          Action: "android.intent.action.VIEW"
Receiver Resolver Table:
      android.intent.action.BOOT_COMPLETED:
        aaa111 com.example/.BootReceiver filter 789
"""
    monkeypatch.setattr(device, "_adb", lambda *args, **kwargs: dumpsys)
    assert device.list_entry_activities("com.example") == ["com.example/.MainActivity"]


def test_read_manifest_prefers_device_unzip(monkeypatch, tmp_path):
    device = AndroidDevice(serial="test")
    expected = b"\x03\x00\x08\x00payload"

    def fake_adb(*args, binary=False):
        if args[:2] == ("shell", "pm"):
            return "package:/data/app/com.example/base.apk\n"
        if args[:2] == ("exec-out", "unzip"):
            return expected
        return ""

    monkeypatch.setattr(device, "_adb", fake_adb)
    assert device.read_manifest_axml("com.example", tmp_path / "manifest.apk") == expected


def test_read_manifest_falls_back_to_pull(monkeypatch, tmp_path):
    device = AndroidDevice(serial="test")
    manifest = b"\x03\x00\x08\x00payload"
    apk = tmp_path / "base.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("AndroidManifest.xml", manifest)

    def fake_adb(*args, binary=False):
        if args[:2] == ("shell", "pm"):
            return "package:/data/app/com.example/base.apk"
        if args[:2] == ("exec-out", "unzip"):
            raise DeviceError("unzip: not found")
        if args[0] == "pull":
            import shutil

            shutil.copy(apk, args[2])
            return "1 file pulled"
        return ""

    monkeypatch.setattr(device, "_adb", fake_adb)
    cache = tmp_path / "cache" / "manifest.apk"
    assert device.read_manifest_axml("com.example", cache) == manifest
