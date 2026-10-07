from pathlib import Path

from PIL import Image, ImageDraw

from app_graph.device import Capture
from app_graph.explorer import Explorer, ExplorerConfig
from app_graph.matcher import _phash, _prepared


class FakeDevice:
    def __init__(self, fixtures: Path):
        self.fixtures = fixtures
        self.screen = "root"

    def ensure_device(self):
        return "fake-device"

    def launch(self, package, clear_task=True):
        if clear_task:
            self.screen = "root"

    def force_stop(self, package):
        self.screen = "root"

    def tap(self, x, y):
        if self.screen == "root":
            self.screen = "details"
        else:
            self.screen = "root"

    def swipe(self, x, y, x2, y2, duration_ms):
        pass

    def back(self):
        if self.screen == "details":
            self.screen = "root"

    def capture(self, directory, name):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        screenshot, xml = directory / f"{name}.png", directory / f"{name}.xml"
        image = Image.new("RGB", (240, 480), "white" if self.screen == "root" else "black")
        draw = ImageDraw.Draw(image)
        if self.screen == "root":
            draw.rectangle((15, 80, 225, 150), fill="blue")
            xml.write_text(
                '<hierarchy><node text="Open details" clickable="true" enabled="true" bounds="[20,100][220,140]"/></hierarchy>',
                encoding="utf-8",
            )
        else:
            draw.ellipse((25, 100, 215, 300), fill="yellow")
            xml.write_text("<hierarchy />", encoding="utf-8")
        image.save(screenshot)
        return Capture(screenshot, xml, "example.app/.Main", 240, 480)


class OneShotDialogDevice(FakeDevice):
    """A startup dialog appears only on the first launcher invocation."""

    def __init__(self, fixtures: Path):
        super().__init__(fixtures)
        self.launch_count = 0
        self.taps = []

    def launch(self, package, clear_task=True):
        self.launch_count += 1
        if self.launch_count == 1:
            self.screen = "dialog"
        else:
            self.screen = "home"

    def tap(self, x, y):
        self.taps.append((x, y, self.screen))
        self.screen = "home"

    def capture(self, directory, name):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        screenshot, xml = directory / f"{name}.png", directory / f"{name}.xml"
        if self.screen == "dialog":
            image = Image.new("RGB", (240, 480), "white")
            ImageDraw.Draw(image).rectangle((70, 80, 170, 130), fill="red")
            xml.write_text(
                '<hierarchy><node text="关闭" content-desc="关闭" clickable="true" bounds="[90,90][150,120]"/></hierarchy>',
                encoding="utf-8",
            )
        else:
            image = Image.new("RGB", (240, 480), "white")
            ImageDraw.Draw(image).rectangle((70, 250, 170, 300), fill="green")
            xml.write_text(
                '<hierarchy><node text="首页" content-desc="首页" clickable="true" bounds="[90,250][150,280]"/></hierarchy>',
                encoding="utf-8",
            )
        image.save(screenshot)
        return Capture(screenshot, xml, "example.app/.Main", 240, 480)


def test_explorer_builds_bfs_graph_and_saves_state_assets(tmp_path):
    explorer = Explorer(
        FakeDevice(tmp_path),
        ExplorerConfig(
            package="example.app",
            output_dir=tmp_path / "run",
            max_depth=1,
            max_states=10,
            settle_seconds=0,
        ),
        progress=lambda _: None,
    )
    try:
        states, edges = explorer.run()
        assert states == 2
        assert edges >= 1
        graph = explorer.db.graph_data()
        assert any(edge["from"] == 1 and edge["to"] == 2 for edge in graph["edges"])
        assert (tmp_path / "run/states/0001/screenshot.png").exists()
        assert (tmp_path / "run/states/0002/ui.xml").exists()
    finally:
        explorer.close()


def test_first_root_action_dismisses_one_shot_startup_dialog(tmp_path):
    device = OneShotDialogDevice(tmp_path)
    explorer = Explorer(
        device,
        ExplorerConfig(
            package="example.app",
            output_dir=tmp_path / "run",
            max_depth=1,
            max_states=10,
            max_actions_per_state=1,
            settle_seconds=0,
            navigation_only=True,
        ),
        progress=lambda _: None,
    )
    try:
        states, edges = explorer.run()
        assert device.taps[0] == (120, 105, "dialog")
        assert states == 2
        assert edges >= 1
        first_edge = explorer.db.graph_data()["edges"][0]
        assert first_edge["action"]["content_desc"] == "关闭"
        assert first_edge["from"] == 1 and first_edge["to"] == 2
    finally:
        explorer.close()


def test_revisiting_root_counts_one_observation_per_run(tmp_path):
    config = ExplorerConfig(
        package="example.app",
        output_dir=tmp_path / "run",
        max_depth=0,
        settle_seconds=0,
    )
    device = FakeDevice(tmp_path)
    explorer = Explorer(device, config, progress=lambda _: None)
    try:
        explorer.run()
        assert explorer.db.get_state(1).visit_count == 1

        explorer.run()
        assert explorer.db.get_state(1).visit_count == 2
    finally:
        explorer.close()


def test_config_rejects_invalid_limits(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="max_states"):
        ExplorerConfig("pkg", tmp_path, max_states=0)
    with pytest.raises(ValueError, match="ssim_threshold"):
        ExplorerConfig("pkg", tmp_path, ssim_threshold=1.1)
    with pytest.raises(ValueError, match="structure_threshold"):
        ExplorerConfig("pkg", tmp_path, structure_threshold=-0.1)
    with pytest.raises(ValueError, match="ready_timeout_seconds"):
        ExplorerConfig("pkg", tmp_path, ready_timeout_seconds=-1)


def test_state_limit_keeps_known_self_transitions(tmp_path):
    explorer = Explorer(
        FakeDevice(tmp_path),
        ExplorerConfig(
            package="example.app",
            output_dir=tmp_path / "run",
            max_depth=1,
            max_states=1,
            settle_seconds=0,
        ),
        progress=lambda _: None,
    )
    try:
        states, edges = explorer.run()
        assert states == 1
        assert edges >= 1
        assert all(edge["to"] == 1 for edge in explorer.db.graph_data()["edges"])
    finally:
        explorer.close()


def test_stop_request_is_safe_before_exploration(tmp_path):
    explorer = Explorer(
        FakeDevice(tmp_path),
        ExplorerConfig("example.app", tmp_path / "run", max_depth=1, settle_seconds=0),
        progress=lambda _: None,
    )
    try:
        explorer.request_stop()
        assert explorer.run() == (1, 0)
    finally:
        explorer.close()


class ReplayAdDevice(FakeDevice):
    """Replaying from a cold start lands on an unparseable full-screen ad."""

    def __init__(self, fixtures: Path):
        super().__init__(fixtures)
        self.launch_count = 0

    def launch(self, package, clear_task=True):
        self.launch_count += 1
        self.screen = "root" if self.launch_count == 1 else "ad"

    def force_stop(self, package):
        pass

    def capture(self, directory, name):
        if self.screen != "ad":
            return super().capture(directory, name)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        screenshot, xml = directory / f"{name}.png", directory / f"{name}.xml"
        Image.new("RGB", (240, 480), "black").save(screenshot)
        xml.write_text("<hierarchy />", encoding="utf-8")
        return Capture(
            screenshot, xml, "example.app/com.tencentmusic.ad.TMECoreActivity", 240, 480, False
        )


class OpaqueAfterTapDevice(FakeDevice):
    """Tapping the only control opens an unparseable ad screen."""

    def tap(self, x, y):
        self.screen = "ad"

    def capture(self, directory, name):
        if self.screen != "ad":
            return super().capture(directory, name)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        screenshot, xml = directory / f"{name}.png", directory / f"{name}.xml"
        Image.new("RGB", (240, 480), "black").save(screenshot)
        xml.write_text("<hierarchy />", encoding="utf-8")
        return Capture(
            screenshot, xml, "example.app/com.tencentmusic.ad.TMECoreActivity", 240, 480, False
        )


def test_route_replay_timeout_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr("app_graph.explorer.time.sleep", lambda _: None)
    messages = []
    explorer = Explorer(
        ReplayAdDevice(tmp_path),
        ExplorerConfig(
            package="example.app",
            output_dir=tmp_path / "run",
            max_depth=1,
            max_states=10,
            settle_seconds=0,
            ready_timeout_seconds=0.05,
        ),
        progress=messages.append,
    )
    try:
        states, edges = explorer.run()
        assert states == 2
        assert edges == 1
        assert any("Route replay" in message for message in messages)
        pending = [action.kind for _, action in explorer.db.pending_actions(1)]
        assert "click" not in pending
        assert pending
    finally:
        explorer.close()


def test_opaque_screen_is_not_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr("app_graph.explorer.time.sleep", lambda _: None)
    messages = []
    explorer = Explorer(
        OpaqueAfterTapDevice(tmp_path),
        ExplorerConfig(
            package="example.app",
            output_dir=tmp_path / "run",
            max_depth=1,
            max_states=10,
            settle_seconds=0,
        ),
        progress=messages.append,
    )
    try:
        states, edges = explorer.run()
        assert states == 1
        assert edges == 3  # swipe_up/swipe_down/back self-loops; the ad edge is omitted
        pending = [action.kind for _, action in explorer.db.pending_actions(1)]
        assert pending == ["click"]
        assert any("transient screen" in message for message in messages)
    finally:
        explorer.close()


class RotatingContentDevice(FakeDevice):
    """Clickable home page whose labels change on every capture, like hot-search hints."""

    def __init__(self, fixtures: Path):
        super().__init__(fixtures)
        self.captures = 0

    def capture(self, directory, name):
        capture = super().capture(directory, name)
        if self.screen == "root":
            self.captures += 1
            capture.xml.write_text(
                "<hierarchy>"
                f'<node text="hint {self.captures}" clickable="true" enabled="true" bounds="[20,100][220,140]"/>'
                "</hierarchy>",
                encoding="utf-8",
            )
        return capture


def test_dynamic_clickable_page_is_accepted_without_stable_signature(monkeypatch, tmp_path):
    monkeypatch.setattr("app_graph.explorer.time.sleep", lambda _: None)
    monkeypatch.setattr("app_graph.explorer.READY_GRACE_SECONDS", 0)
    explorer = Explorer(
        RotatingContentDevice(tmp_path),
        ExplorerConfig(
            package="example.app",
            output_dir=tmp_path / "run",
            max_depth=0,
            settle_seconds=0,
            ready_timeout_seconds=1,
        ),
        progress=lambda _: None,
    )
    try:
        states, edges = explorer.run()
        assert states == 1
        assert edges == 0
    finally:
        explorer.close()


def test_structural_match_accepts_rotated_content(tmp_path):
    explorer = Explorer(
        FakeDevice(tmp_path),
        ExplorerConfig("example.app", tmp_path / "run", settle_seconds=0),
        progress=lambda _: None,
    )
    try:
        state_dir = explorer.states_dir / "0001"
        state_dir.mkdir(parents=True)
        screenshot = state_dir / "screenshot.png"
        Image.new("RGB", (240, 480), "white").save(screenshot)
        xml = state_dir / "ui.xml"
        xml.write_text(
            '<hierarchy><node text="Open details" resource-id="example:id/open" '
            'class="android.widget.Button" clickable="true" enabled="true" '
            'bounds="[20,100][220,140]"/></hierarchy>',
            encoding="utf-8",
        )
        explorer.db.add_state(
            0,
            "example.app/.Main",
            "states/0001/screenshot.png",
            "states/0001/ui.xml",
            _phash(_prepared(screenshot)[1]),
        )

        working = explorer.states_dir / ".working"
        working.mkdir(exist_ok=True)
        rotated_screenshot = working / "rotated.png"
        rotated_image = Image.new("RGB", (240, 480), "black")
        ImageDraw.Draw(rotated_image).ellipse((25, 100, 215, 300), fill="yellow")
        rotated_image.save(rotated_screenshot)
        rotated_xml = working / "rotated.xml"
        rotated_xml.write_text(
            '<hierarchy><node text="New hint" resource-id="example:id/open" '
            'class="android.widget.Button" clickable="true" enabled="true" '
            'bounds="[20,100][220,140]"/></hierarchy>',
            encoding="utf-8",
        )
        capture = Capture(rotated_screenshot, rotated_xml, "example.app/.Main", 240, 480)

        matched, is_new, score = explorer._match_or_create(
            capture, depth=0, allow_new=False, store_variant=False, update_depth=False
        )
        assert matched is not None and matched.id == 1
        assert is_new is False
        assert score is None
    finally:
        explorer.close()


def test_structural_match_rejects_same_controls_in_different_hierarchy(tmp_path):
    explorer = Explorer(
        FakeDevice(tmp_path),
        ExplorerConfig("example.app", tmp_path / "run", settle_seconds=0),
        progress=lambda _: None,
    )
    try:
        state_dir = explorer.states_dir / "0001"
        state_dir.mkdir(parents=True)
        screenshot = state_dir / "screenshot.png"
        Image.new("RGB", (240, 480), "white").save(screenshot)
        xml = state_dir / "ui.xml"
        xml.write_text(
            '<hierarchy><node class="FrameLayout" resource-id="example:id/root">'
            '<node class="Button" resource-id="example:id/open" clickable="true" '
            'enabled="true" bounds="[20,100][220,140]"/>'
            "</node></hierarchy>",
            encoding="utf-8",
        )
        explorer.db.add_state(
            0,
            "example.app/.Main",
            "states/0001/screenshot.png",
            "states/0001/ui.xml",
            _phash(_prepared(screenshot)[1]),
        )

        working = explorer.states_dir / ".working"
        working.mkdir(exist_ok=True)
        rotated_screenshot = working / "rotated.png"
        rotated_image = Image.new("RGB", (240, 480), "black")
        ImageDraw.Draw(rotated_image).ellipse((25, 100, 215, 300), fill="yellow")
        rotated_image.save(rotated_screenshot)
        rotated_xml = working / "rotated.xml"
        rotated_xml.write_text(
            '<hierarchy><node class="FrameLayout" resource-id="example:id/root">'
            '<node class="LinearLayout" resource-id="example:id/panel">'
            '<node class="Button" resource-id="example:id/open" clickable="true" '
            'enabled="true" bounds="[20,100][220,140]"/>'
            "</node></node></hierarchy>",
            encoding="utf-8",
        )
        capture = Capture(rotated_screenshot, rotated_xml, "example.app/.Main", 240, 480)

        matched, is_new, _ = explorer._match_or_create(
            capture, depth=0, allow_new=False, store_variant=False, update_depth=False
        )
        assert matched is None
        assert is_new is False
    finally:
        explorer.close()


class TabRestoreDevice:
    """Cold start restores the last selected tab instead of the root page."""

    def __init__(self):
        self.screen = "root"

    def ensure_device(self):
        return "fake-device"

    def launch(self, package, clear_task=True):
        self.screen = "listen"

    def force_stop(self, package):
        pass

    def tap(self, x, y):
        if self.screen == "listen":
            self.screen = "root"

    def swipe(self, x, y, x2, y2, duration_ms):
        pass

    def back(self):
        pass

    def capture(self, directory, name):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        screenshot, xml = directory / f"{name}.png", directory / f"{name}.xml"
        if self.screen == "root":
            image = Image.new("RGB", (240, 480), "white")
            ImageDraw.Draw(image).rectangle((15, 80, 225, 150), fill="blue")
            xml.write_text(
                '<hierarchy><node text="Open details" resource-id="example:id/open" '
                'class="Button" clickable="true" enabled="true" bounds="[20,100][220,140]"/>'
                "</hierarchy>",
                encoding="utf-8",
            )
        else:
            image = Image.new("RGB", (240, 480), "black")
            ImageDraw.Draw(image).ellipse((25, 100, 215, 300), fill="yellow")
            xml.write_text(
                '<hierarchy><node text="" content-desc="首页" resource-id="example:id/home" '
                'class="Button" clickable="true" enabled="true" bounds="[40,430][80,470]"/>'
                "</hierarchy>",
                encoding="utf-8",
            )
        image.save(screenshot)
        return Capture(screenshot, xml, "example.app/.Main", 240, 480)


def test_route_replay_taps_home_when_cold_start_restores_another_tab(tmp_path):
    device = TabRestoreDevice()
    messages = []
    explorer = Explorer(
        device,
        ExplorerConfig("example.app", tmp_path / "run", settle_seconds=0),
        progress=messages.append,
    )
    try:
        for state_id, screen, activity in ((1, "root", "example.app/.Main"), (2, "listen", "example.app/.Listen")):
            state_dir = explorer.states_dir / f"{state_id:04d}"
            state_dir.mkdir(parents=True)
            device.screen = screen
            capture = device.capture(state_dir, "screenshot")
            explorer.db.add_state(
                state_id - 1,
                activity,
                f"states/{state_id:04d}/screenshot.png",
                f"states/{state_id:04d}/screenshot.xml",
                _phash(_prepared(capture.screenshot)[1]),
            )
        (explorer.states_dir / "0001/screenshot.xml").rename(explorer.states_dir / "0001/ui.xml")
        (explorer.states_dir / "0002/screenshot.xml").rename(explorer.states_dir / "0002/ui.xml")
        explorer._root_state_id = 1

        assert explorer._restore(1, []) is True
        assert device.screen == "root"
        assert any("returning to a known page" in message for message in messages)
    finally:
        explorer.close()


class PersistedTabDevice:
    """Cold start restores the last tab; tapping Home makes it stick."""

    def __init__(self):
        self.screen = "listen"
        self.last_tab = "listen"

    def ensure_device(self):
        return "fake-device"

    def launch(self, package, clear_task=True):
        self.screen = self.last_tab

    def force_stop(self, package):
        pass

    def tap(self, x, y):
        if self.screen == "listen":
            self.last_tab = "root"
            self.screen = "root"

    def swipe(self, x, y, x2, y2, duration_ms):
        pass

    def back(self):
        pass

    def capture(self, directory, name):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        screenshot, xml = directory / f"{name}.png", directory / f"{name}.xml"
        if self.screen == "root":
            image = Image.new("RGB", (240, 480), "white")
            ImageDraw.Draw(image).rectangle((15, 80, 225, 150), fill="blue")
            xml.write_text(
                '<hierarchy><node text="Open details" clickable="true" enabled="true" '
                'bounds="[20,100][220,140]"/></hierarchy>',
                encoding="utf-8",
            )
        else:
            image = Image.new("RGB", (240, 480), "black")
            ImageDraw.Draw(image).ellipse((25, 100, 215, 300), fill="yellow")
            xml.write_text(
                '<hierarchy><node text="" content-desc="首页" resource-id="example:id/home" '
                'clickable="true" enabled="true" bounds="[40,430][80,470]"/></hierarchy>',
                encoding="utf-8",
            )
        image.save(screenshot)
        return Capture(screenshot, xml, "example.app/.Main", 240, 480)


def test_root_is_pinned_to_home_when_cold_start_restores_another_tab(monkeypatch, tmp_path):
    monkeypatch.setattr("app_graph.explorer.time.sleep", lambda _: None)
    device = PersistedTabDevice()
    messages = []
    explorer = Explorer(
        device,
        ExplorerConfig("example.app", tmp_path / "run", max_depth=0, settle_seconds=0),
        progress=messages.append,
    )
    try:
        states, _ = explorer.run()
        assert states == 1
        assert any("Pinning root page" in message for message in messages)
        assert device.last_tab == "root"
        stored = (tmp_path / "run" / explorer.db.get_state(1).xml_path).read_text(
            encoding="utf-8"
        )
        assert "Open details" in stored
    finally:
        explorer.close()


class SeedDevice(FakeDevice):
    """Fake device exposing two launchable activities, one of them broken."""

    def __init__(self, fixtures: Path):
        super().__init__(fixtures)
        self.launched_components: list[str] = []
        self.fail_components: set[str] = set()

    def read_manifest_axml(self, package, cache_path):
        return None

    def list_entry_activities(self, package):
        return ["example.app/.Seed", "example.app/.Broken"]

    def launch_component(self, component, clear_task=True):
        self.launched_components.append(component)
        if component in self.fail_components:
            return False
        self.screen = "seed"
        return True

    def tap(self, x, y):
        if self.screen == "seed":
            return
        super().tap(x, y)

    def capture(self, directory, name):
        if self.screen != "seed":
            return super().capture(directory, name)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        screenshot, xml = directory / f"{name}.png", directory / f"{name}.xml"
        image = Image.new("RGB", (240, 480), "gray")
        ImageDraw.Draw(image).rectangle((20, 300, 220, 360), fill="purple")
        xml.write_text(
            '<hierarchy><node text="Seed page" resource-id="example:id/seed" '
            'clickable="true" enabled="true" bounds="[30,310][210,350]"/></hierarchy>',
            encoding="utf-8",
        )
        image.save(screenshot)
        return Capture(screenshot, xml, "example.app/.Seed", 240, 480)


def test_seed_activities_are_added_as_roots_and_explored(tmp_path):
    device = SeedDevice(tmp_path)
    device.fail_components.add("example.app/.Broken")
    messages = []
    explorer = Explorer(
        device,
        ExplorerConfig(
            package="example.app",
            output_dir=tmp_path / "run",
            max_depth=1,
            max_states=10,
            max_actions_per_state=2,
            settle_seconds=0,
            seed_activities=True,
        ),
        progress=messages.append,
    )
    try:
        explorer.run()
        seeded = next(
            state for state in explorer.db.states() if state.activity == "example.app/.Seed"
        )
        assert seeded.depth == 0
        assert any("seed example.app/.Seed → state" in message for message in messages)
        assert any(".Broken" in message and "could not launch" in message for message in messages)
        # Seed routes must be replayed through the component, not the launcher.
        assert device.launched_components.count("example.app/.Seed") >= 2
        graph = explorer.db.graph_data()
        seed_edges = [
            edge for edge in graph["edges"] if edge["from"] == seeded.id or edge["to"] == seeded.id
        ]
        assert seed_edges
    finally:
        explorer.close()
