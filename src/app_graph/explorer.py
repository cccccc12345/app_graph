"""Breadth-first Android app explorer."""

from __future__ import annotations

import logging
import re
import shutil
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .database import GraphDatabase, State
from .device import AndroidDevice, Capture, DeviceError
from .manifest import launchable_components, parse_manifest
from .matcher import MatchScore, _prepared, _phash, compare_screenshots
from .models import Action
from .ui import HierarchySignature, extract_actions, hierarchy_signature, structure_similarity

LOG = logging.getLogger("app_graph")

# Dynamic home pages (rotating search hints, carousels, play counts) never
# produce two byte-identical clickable signatures. Once clickable controls have
# been visible for this long, accept the screen even if its content keeps
# changing; a splash/ad without usable controls never reaches this grace.
READY_GRACE_SECONDS = 3.0

# Apps often persist the last selected tab across force-stop, so a cold start
# can restore a sub-page instead of the root. A control labelled like Home can
# usually bring the app back to the canonical root without app knowledge.
_HOME_LABELS = re.compile(r"(?<![a-z0-9])(?:home|首页|主页)(?![a-z0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class ExplorerConfig:
    package: str
    output_dir: Path
    max_depth: int = 5
    max_states: int = 100
    max_actions_per_state: int = 80
    settle_seconds: float = 0.6
    ready_timeout_seconds: float = 60.0
    phash_distance: int = 8
    ssim_threshold: float = 0.90
    structure_threshold: float = 0.90
    force_stop_before_replay: bool = True
    navigation_only: bool = False
    seed_activities: bool = False

    def __post_init__(self) -> None:
        if not self.package.strip():
            raise ValueError("package must not be empty")
        if self.max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        if self.max_states < 1:
            raise ValueError("max_states must be >= 1")
        if self.max_actions_per_state < 0:
            raise ValueError("max_actions_per_state must be >= 0")
        if self.settle_seconds < 0:
            raise ValueError("settle_seconds must be >= 0")
        if self.ready_timeout_seconds < 0:
            raise ValueError("ready_timeout_seconds must be >= 0")
        if not 0 <= self.phash_distance <= 64:
            raise ValueError("phash_distance must be between 0 and 64")
        if not 0 <= self.ssim_threshold <= 1:
            raise ValueError("ssim_threshold must be between 0 and 1")
        if not 0 <= self.structure_threshold <= 1:
            raise ValueError("structure_threshold must be between 0 and 1")


class Explorer:
    def __init__(
        self,
        device: AndroidDevice,
        config: ExplorerConfig,
        progress: Callable[[str], None] | None = None,
    ):
        self.device = device
        self.config = config
        self.output_dir = config.output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.states_dir = self.output_dir / "states"
        self.states_dir.mkdir(exist_ok=True)
        self.db = GraphDatabase(self.output_dir / "graph.db")
        self.progress = progress or LOG.info
        self._next_capture = 0
        self._expanded: set[int] = set()
        self._queued: set[int] = set()
        self._stop_requested = False
        self._root_state_id: int | None = None
        self._state_structures: dict[int, HierarchySignature] = {}
        # Route origins: state id -> component to launch directly (None = the
        # app's launcher root). Seeded activities are additional depth-0 roots.
        self._route_entries: dict[int, str | None] = {}

    def request_stop(self) -> None:
        self._stop_requested = True

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    def close(self) -> None:
        self.db.close()

    def run(self) -> tuple[int, int]:
        self.db.bind_package(self.config.package)
        model = self.device.ensure_device()
        self.progress(f"Connected device: {model or 'Android device'}")
        self.device.launch(self.config.package, clear_task=True)
        root_capture = self._capture_ready_screen(
            "initial launch", timeout_seconds=self.config.ready_timeout_seconds
        )
        root_capture = self._canonicalize_root(root_capture)
        if self._outside_target_app(root_capture.activity):
            self._discard_capture(root_capture)
            raise RuntimeError(
                f"Could not launch target package {self.config.package}; "
                f"foreground activity is {root_capture.activity!r}."
            )
        root, is_new, _ = self._match_or_create(root_capture, depth=0)
        if root is None:
            self._discard_capture(root_capture)
            raise RuntimeError("Could not create or match the root state")
        self.db.update_min_depth(root.id, 0)
        root = self.db.get_state(root.id)
        self._root_state_id = root.id
        self.progress(f"Root state: #{root.id} {'(new)' if is_new else '(matched)'}")

        # This run starts from a fresh root and explores the reachable graph.
        # If this output DB already contains part of a run, reconstruct known
        # BFS routes so unattempted actions can be resumed after interruption.
        routes = self.db.reachable_routes(root.id)
        queue: deque[tuple[int, list[tuple[Action, int]], int]] = deque(
            (state_id, path, root.id) for state_id, path in routes
        )
        self._queued.update(state_id for state_id, _ in routes)
        self._route_entries[root.id] = None
        # Seeding runs after the launcher-root BFS drains: the first root action
        # deliberately skips replay, so nothing may move the app before it runs.
        seeding_done = not self.config.seed_activities
        while not self._stop_requested:
            if not queue:
                if seeding_done:
                    break
                seeding_done = True
                self._seed_activities(root.id, queue)
                if not queue:
                    break
                continue
            state_id, path, origin_state_id = queue.popleft()
            if state_id in self._expanded:
                continue
            state = self.db.get_state(state_id)
            if state.depth > self.config.max_depth:
                self._expanded.add(state_id)
                continue
            # The first task already is the just-captured launcher state. Do
            # not launch a second time before its first action: some apps show
            # one-shot welcome/subscription dialogs only on the first launch.
            is_initial_root = state_id == root.id and not path
            if not is_initial_root and not self._restore(origin_state_id, path):
                self.progress(f"Could not replay route to state #{state_id}; skipping its actions")
                self._expanded.add(state_id)
                continue

            if is_initial_root:
                current = root_capture
            else:
                current = self._capture_parsed()
                if current is None:
                    self.progress(
                        f"Route verification for #{state_id}: screen is unreadable; skipping"
                    )
                    self._expanded.add(state_id)
                    continue
                current_state, _, score = self._match_or_create(
                    current, depth=state.depth, allow_new=False, store_variant=False
                )
                if current_state is None or current_state.id != state_id:
                    self._discard_capture(current)
                    reached = f"#{current_state.id}" if current_state else "an unknown state"
                    self.progress(
                        f"Route verification mismatch for #{state_id}: reached {reached} "
                        f"(pHash={score.phash_distance if score else 'n/a'}, "
                        f"SSIM={f'{score.ssim:.3f}' if score else 'n/a'}); skipping"
                    )
                    self._expanded.add(state_id)
                    continue
            state = self.db.get_state(state_id)
            actions = self._state_actions(state_id, current)
            self._discard_capture(current)
            if state.depth >= self.config.max_depth:
                self.progress(f"State #{state_id}: depth limit reached; {len(actions)} actions recorded but not run")
                self._expanded.add(state_id)
                continue

            self.progress(f"Exploring state #{state_id} at depth {state.depth} ({len(actions)} actions)")
            for action_index, action in enumerate(actions):
                if self._stop_requested:
                    self.progress("Stop requested; finishing the current action")
                    break
                # Always replay from the canonical root route: it makes each
                # edge independent of the side effects of the previous action.
                if not (is_initial_root and action_index == 0) and not self._restore(origin_state_id, path):
                    self.progress(f"Route replay failed while exploring state #{state_id}")
                    break
                if action.kind == "click":
                    self.device.tap(action.x, action.y)
                elif action.kind.startswith("swipe"):
                    self.device.swipe(action.x, action.y, action.x2, action.y2, action.duration_ms)
                elif action.kind == "back":
                    self.device.back()
                else:
                    continue
                time.sleep(self.config.settle_seconds)

                after = self._capture_parsed()
                if after is None:
                    # Opaque screens (startup/video ads, unparseable overlays)
                    # are not mapped into the graph. The action stays pending so
                    # a later run can retry it once the app is settled.
                    self.progress(
                        f"  {action.label} → transient screen (uiautomator dump failed); action skipped"
                    )
                    continue
                if self._outside_target_app(after.activity):
                    # Avoid mapping Android settings, launchers, permission
                    # managers, or other apps as if they belonged to the APK.
                    self.progress(f"Action '{action.label}' left the target app; recording a self-loop")
                    self.device.launch(self.config.package)
                    target = state
                    self.db.add_edge(state_id, action, target.id)
                    self._discard_capture(after)
                    continue

                target, is_new, match_score = self._match_or_create(
                    after,
                    depth=state.depth + 1,
                    allow_new=len(self.db.states()) < self.config.max_states,
                )
                self._discard_capture(after)
                if target is None:
                    self.progress(
                        f"State limit reached; '{action.label}' led to an unrecorded new page, so this edge was omitted"
                    )
                    continue
                self.db.add_edge(state_id, action, target.id)
                suffix = (
                    f"pHash={match_score.phash_distance}, SSIM={match_score.ssim:.3f}"
                    if match_score
                    else ("new state" if is_new else "structural match")
                )
                self.progress(f"  {action.label} → state #{target.id} ({suffix})")

                if target.id not in self._queued and target.depth <= self.config.max_depth:
                    queue.append((target.id, path + [(action, target.id)], origin_state_id))
                    self._queued.add(target.id)
            if not self._stop_requested:
                self._expanded.add(state_id)

        return self.db.counts()

    def _capture(self) -> Capture:
        self._next_capture += 1
        return self.device.capture(self.states_dir / ".working", f"capture_{self._next_capture:06d}")

    def _capture_parsed(self, attempts: int = 2) -> Capture | None:
        """Capture a screen with a readable hierarchy, retrying transient dump failures.

        Animating screens (video ads, heavy transitions) make UIAutomator fail to
        reach idle, so a single failed dump is not proof that the screen is
        unreadable. Returns None when no attempt produced a parsed hierarchy.
        """
        for attempt in range(attempts):
            if attempt:
                time.sleep(0.6)
            capture = self._capture()
            if capture.xml_ok:
                return capture
            self._discard_capture(capture)
        return None

    def _capture_ready_screen(self, context: str, timeout_seconds: float = 30.0) -> Capture:
        """Wait out splash/loading frames and return an actionable UI.

        A screen is ready as soon as its clickable controls repeat exactly, or
        once controls have been visible for READY_GRACE_SECONDS (dynamic pages
        keep rewriting their text, bounds and carousel content forever).
        """
        deadline = time.monotonic() + timeout_seconds
        previous: Capture | None = None
        previous_signature = None
        first_click_time: float | None = None
        while time.monotonic() < deadline:
            capture = self._capture()
            if self._outside_target_app(capture.activity):
                self._discard_capture(capture)
                if previous:
                    self._discard_capture(previous)
                raise RuntimeError(
                    f"During {context}, foreground activity left {self.config.package}: "
                    f"{capture.activity!r}."
                )
            try:
                xml_text = capture.xml.read_text(encoding="utf-8", errors="replace")
                clicks = [
                    action
                    for action in extract_actions(xml_text, capture.width, capture.height)
                    if action.kind == "click"
                ]
            except OSError:
                clicks = []
            if not clicks:
                self._discard_capture(capture)
                if previous:
                    self._discard_capture(previous)
                previous = None
                previous_signature = None
                first_click_time = None
                time.sleep(0.4)
                continue

            now = time.monotonic()
            if first_click_time is None:
                first_click_time = now
            signature = (
                capture.activity,
                tuple(
                    sorted(
                        (
                            action.resource_id,
                            action.text,
                            action.content_desc,
                            action.bounds.left if action.bounds else 0,
                            action.bounds.top if action.bounds else 0,
                            action.bounds.right if action.bounds else 0,
                            action.bounds.bottom if action.bounds else 0,
                        )
                        for action in clicks
                    )
                ),
            )
            if previous is not None and signature == previous_signature:
                self._discard_capture(previous)
                return capture
            if previous:
                self._discard_capture(previous)
            previous = capture
            previous_signature = signature
            if now - first_click_time >= READY_GRACE_SECONDS:
                return capture
            time.sleep(0.4)

        if previous:
            self._discard_capture(previous)
        raise RuntimeError(
            f"No stable clickable UI appeared during {context} within {timeout_seconds:.0f}s; "
            "the app may still be on a splash/loading screen."
        )

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.output_dir).as_posix()

    @staticmethod
    def _capture_structure(capture: Capture) -> HierarchySignature:
        try:
            xml_text = capture.xml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return Counter()
        return hierarchy_signature(xml_text)

    def _state_structure(self, state: State) -> HierarchySignature:
        cached = self._state_structures.get(state.id)
        if cached is None:
            try:
                xml_text = (self.output_dir / state.xml_path).read_text(
                    encoding="utf-8", errors="replace"
                )
                cached = hierarchy_signature(xml_text)
            except OSError:
                cached = Counter()
            self._state_structures[state.id] = cached
        return cached

    def _match_or_create(
        self,
        capture: Capture,
        depth: int,
        allow_new: bool = True,
        store_variant: bool = True,
        update_depth: bool = True,
    ) -> tuple[State | None, bool, MatchScore | None]:
        # A screen whose hierarchy failed to parse tells us nothing about its
        # controls; never match it against a known page or record it as new.
        if not capture.xml_ok:
            return None, False, None
        candidates: list[tuple[State, Path, int]] = []
        screenshot = capture.screenshot.resolve()
        _, hash_image = _prepared(screenshot)
        current_hash = _phash(hash_image)
        # Stored hashes avoid reopening and rehashing every screenshot just to
        # build the pHash candidate set.
        for state in self.db.states():
            images = [(state.screenshot_path, state.phash)]
            images.extend((v.screenshot_path, v.phash) for v in self.db.variants(state.id))
            for relative_path, stored_hash in images:
                candidate_path = (self.output_dir / relative_path).resolve()
                if not candidate_path.exists():
                    continue
                distance = (current_hash ^ int(stored_hash, 16)).bit_count()
                if distance <= self.config.phash_distance:
                    candidates.append((state, candidate_path, distance))

        best: tuple[State, MatchScore | None] | None = None
        for state, path, distance in candidates:
            try:
                score = compare_screenshots(screenshot, path)
            except Exception as exc:
                LOG.debug("Could not compare screenshots %s and %s: %s", screenshot, path, exc)
                continue
            score = MatchScore(distance, score.ssim)
            previous_ssim = best[1].ssim if best and best[1] is not None else -1.0
            if score.ssim >= self.config.ssim_threshold and score.ssim > previous_ssim:
                best = (state, score)
        if best is None:
            # Pixel matching fails on pages that rotate banners, search hints
            # and list content on every visit. Fall back to comparing the
            # clickable control makeup, which stays stable across visits.
            structure = self._capture_structure(capture)
            best_overlap = 0.0
            for state in self.db.states():
                overlap = structure_similarity(structure, self._state_structure(state))
                if overlap >= self.config.structure_threshold and overlap > best_overlap:
                    best_overlap = overlap
                    best = (state, None)
        if best:
            state, score = best
            if update_depth:
                self.db.update_min_depth(state.id, depth)
            if store_variant:
                screenshot_path, xml_path = self._save_capture(state.id, capture, variant=True)
                self.db.add_variant(
                    state.id,
                    self._relative(screenshot_path),
                    self._relative(xml_path),
                    current_hash,
                )
            return self.db.get_state(state.id), False, score

        if not allow_new:
            return None, False, None

        next_id = (self.db.states()[-1].id + 1) if self.db.states() else 1
        screenshot_path, xml_path = self._save_capture(next_id, capture, variant=False)
        state = self.db.add_state(
            depth,
            capture.activity,
            self._relative(screenshot_path),
            self._relative(xml_path),
            current_hash,
        )
        return state, True, None

    def _state_actions(self, state_id: int, capture: Capture) -> list[Action]:
        # On restart, retain the original action set and continue only actions
        # that were not recorded as attempted.
        if self.db.has_actions(state_id):
            return [action for _, action in self.db.pending_actions(state_id)]

        xml_text = capture.xml.read_text(encoding="utf-8", errors="replace")
        actions = extract_actions(
            xml_text,
            capture.width,
            capture.height,
            navigation_only=self.config.navigation_only,
        )
        gestures = [action for action in actions if action.kind != "click"]
        clicks = [action for action in actions if action.kind == "click"]
        limit = max(0, self.config.max_actions_per_state)
        # Preserve Back/scroll controls whenever the configured cap permits;
        # for unusually small caps, still honor the exact requested maximum.
        selected = (clicks + gestures)[:limit]
        for action in selected:
            self.db.add_action(state_id, action)
        return selected

    def _canonicalize_root(self, capture: Capture) -> Capture:
        """Prefer the app's Home page as the root.

        Apps often restore the last selected tab on a cold start, so the root
        would depend on where the previous run stopped. Tapping a control
        labelled like Home makes runs deterministic.
        """
        try:
            xml_text = capture.xml.read_text(encoding="utf-8", errors="replace")
            actions = extract_actions(
                xml_text, capture.width, capture.height, navigation_only=True
            )
        except OSError:
            return capture
        home = next(
            (
                action
                for action in actions
                if action.kind == "click" and _HOME_LABELS.search(action.label)
            ),
            None,
        )
        if home is None:
            return capture
        self.progress(f"Pinning root page via '{home.label}'")
        self.device.tap(home.x, home.y)
        time.sleep(self.config.settle_seconds)
        try:
            replacement = self._capture_ready_screen(
                "root pinning", timeout_seconds=min(self.config.ready_timeout_seconds, 15.0)
            )
        except (RuntimeError, DeviceError) as exc:
            self.progress(f"Root pinning skipped: {exc}")
            return capture
        self._discard_capture(capture)
        return replacement

    def _navigate_back_to_route(self, route_state_ids: list[int]) -> State | None:
        """Recover when a cold start restores a sub-page instead of the route.

        Tries a bounded number of taps on controls labelled like Home and
        returns the first matched route state; None when recovery fails.
        """
        for _ in range(3):
            capture = self._capture()
            if self._outside_target_app(capture.activity):
                self._discard_capture(capture)
                return None
            try:
                xml_text = capture.xml.read_text(encoding="utf-8", errors="replace")
                actions = extract_actions(
                    xml_text, capture.width, capture.height, navigation_only=True
                )
            except OSError:
                actions = []
            self._discard_capture(capture)
            home = next(
                (
                    action
                    for action in actions
                    if action.kind == "click" and _HOME_LABELS.search(action.label)
                ),
                None,
            )
            if home is None:
                return None
            self.progress(f"Route replay: returning to a known page via '{home.label}'")
            self.device.tap(home.x, home.y)
            time.sleep(self.config.settle_seconds)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                capture = self._capture()
                if self._outside_target_app(capture.activity):
                    self._discard_capture(capture)
                    return None
                matched, _, _ = self._match_or_create(
                    capture,
                    depth=0,
                    allow_new=False,
                    store_variant=False,
                    update_depth=False,
                )
                self._discard_capture(capture)
                if matched is not None:
                    if matched.id in route_state_ids:
                        return matched
                    break
                time.sleep(0.8)
        return None

    def _entry_components(self) -> list[str]:
        package = self.config.package
        manifest = self.device.read_manifest_axml(
            package, self.states_dir / ".working" / "manifest.apk"
        )
        if manifest:
            try:
                components = launchable_components(parse_manifest(manifest, package))
            except Exception as exc:  # malformed/odd AXML should not abort the run
                LOG.debug("Could not parse AndroidManifest.xml: %s", exc)
                components = []
            if components:
                return components
        # Fallback for split-only installs or parse failures: every component
        # with an intent filter in the package's resolver table.
        try:
            return self.device.list_entry_activities(package)
        except DeviceError as exc:
            LOG.debug("Could not list entry activities: %s", exc)
            return []

    def _seed_activities(
        self, root_state_id: int, queue: deque[tuple[int, list[tuple[Action, int]], int]]
    ) -> None:
        components = self._entry_components()
        if not components:
            self.progress("Activity seeding skipped: no launchable activities found")
            return
        self.progress(f"Seeding {len(components)} launchable activities")
        for component in components:
            if self._stop_requested:
                return
            if not self.device.launch_component(component, clear_task=True):
                self.progress(f"  seed {component}: could not launch; skipped")
                continue
            try:
                capture = self._capture_ready_screen(
                    f"seeded {component}",
                    timeout_seconds=min(self.config.ready_timeout_seconds, 30.0),
                )
            except (RuntimeError, DeviceError) as exc:
                self.progress(f"  seed {component}: {exc}; skipped")
                continue
            if self._outside_target_app(capture.activity):
                self._discard_capture(capture)
                # Foreign dialogs (share sheets, permission prompts) often stay
                # on top after a failed activity start; dismiss before moving on.
                self.device.back()
                self.progress(f"  seed {component}: left the target app; skipped")
                continue
            state, is_new, _ = self._match_or_create(
                capture,
                depth=0,
                allow_new=len(self.db.states()) < self.config.max_states,
            )
            self._discard_capture(capture)
            if state is None:
                self.progress(f"  seed {component}: state limit reached; skipped")
                continue
            if state.id == root_state_id:
                self.progress(f"  seed {component}: opens the app root; skipped")
                continue
            if state.id in self._queued:
                self.progress(f"  seed {component}: state #{state.id} already queued; skipped")
                continue
            self._route_entries[state.id] = component
            queue.append((state.id, [], state.id))
            self._queued.add(state.id)
            self.progress(
                f"  seed {component} → state #{state.id} ({'new state' if is_new else 'matched'})"
            )

    def _restore(self, origin_state_id: int, path: list[tuple[Action, int]]) -> bool:
        component = self._route_entries.get(origin_state_id)
        if component is None:
            if self.config.force_stop_before_replay:
                self.device.force_stop(self.config.package)
            self.device.launch(self.config.package, clear_task=True)
        else:
            if self.config.force_stop_before_replay:
                self.device.force_stop(self.config.package)
            if not self.device.launch_component(component, clear_task=True):
                self.progress(f"Route replay stopped: could not launch {component}")
                return False
        start_index = 0
        if self._root_state_id is not None:
            route_state_ids = [origin_state_id, *(target for _, target in path)]
            # Poll until the launched page matches a known state instead of
            # accepting the first clickable screen: startup ads and trampoline
            # activities are clickable too, but only the expected route state
            # proves the app has settled.
            matched_state: State | None = None
            deadline = time.monotonic() + self.config.ready_timeout_seconds
            try:
                while time.monotonic() < deadline:
                    capture = self._capture()
                    if self._outside_target_app(capture.activity):
                        self._discard_capture(capture)
                        self.device.back()
                        if component is None:
                            self.device.launch(self.config.package)
                        else:
                            self.device.launch_component(component, clear_task=True)
                        return False
                    matched, _, _ = self._match_or_create(
                        capture,
                        depth=0,
                        allow_new=False,
                        store_variant=False,
                        update_depth=False,
                    )
                    self._discard_capture(capture)
                    if matched is not None:
                        matched_state = matched
                        break
                    time.sleep(0.8)
            except DeviceError as exc:
                self.progress(f"Route replay stopped: {exc}")
                return False
            if matched_state is None:
                matched_state = self._navigate_back_to_route(route_state_ids)
            if matched_state is None:
                self.progress(
                    "Route replay stopped: launched page did not match any known state "
                    f"within {self.config.ready_timeout_seconds:.0f}s."
                )
                return False
            matching_indices = [
                i for i, state_id in enumerate(route_state_ids) if state_id == matched_state.id
            ]
            if not matching_indices:
                recovered = self._navigate_back_to_route(route_state_ids)
                if recovered is None:
                    self.progress(
                        f"Route replay stopped: launch reached state #{matched_state.id}, "
                        "not on the saved route."
                    )
                    return False
                matched_state = recovered
                matching_indices = [
                    i for i, state_id in enumerate(route_state_ids) if state_id == matched_state.id
                ]
            # If startup onboarding or a transient sheet was dismissed during
            # an earlier branch, a later fresh launch can land directly on a
            # known point in this route. Resume from that checkpoint instead
            # of tapping stale coordinates.
            start_index = max(matching_indices)
        for action, expected_state_id in path[start_index:]:
            if action.kind == "click":
                self.device.tap(action.x, action.y)
            elif action.kind.startswith("swipe"):
                self.device.swipe(action.x, action.y, action.x2, action.y2, action.duration_ms)
            elif action.kind == "back":
                self.device.back()
            time.sleep(self.config.settle_seconds)
            capture = self._capture()
            if self._outside_target_app(capture.activity):
                self._discard_capture(capture)
                self.device.back()
                if component is None:
                    self.device.launch(self.config.package)
                else:
                    self.device.launch_component(component, clear_task=True)
                return False
            matched, _, _ = self._match_or_create(
                capture, depth=self.db.get_state(expected_state_id).depth, allow_new=False, store_variant=False
            )
            self._discard_capture(capture)
            if matched is None or matched.id != expected_state_id:
                return False
        return True

    def _save_capture(self, state_id: int, capture: Capture, variant: bool) -> tuple[Path, Path]:
        state_dir = self.states_dir / f"{state_id:04d}"
        if variant:
            variant_dir = state_dir / "variants"
            variant_dir.mkdir(parents=True, exist_ok=True)
            index = len(list(variant_dir.glob("*.png"))) + 1
            screenshot_path = variant_dir / f"{index:04d}.png"
            xml_path = variant_dir / f"{index:04d}.xml"
        else:
            state_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = state_dir / "screenshot.png"
            xml_path = state_dir / "ui.xml"
        shutil.copy2(capture.screenshot, screenshot_path)
        shutil.copy2(capture.xml, xml_path)
        return screenshot_path, xml_path

    @staticmethod
    def _discard_capture(capture: Capture) -> None:
        capture.screenshot.unlink(missing_ok=True)
        capture.xml.unlink(missing_ok=True)

    def _outside_target_app(self, activity: str) -> bool:
        if not activity or "/" not in activity:
            return False  # dumpsys format differs across Android versions
        owner = activity.split("/", 1)[0]
        return owner != self.config.package
