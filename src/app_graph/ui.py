"""Parse UIAutomator XML and derive safe, replayable actions."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter

from .models import Action, Bounds

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# Exploration is intentionally conservative. Risky actions are omitted rather
# than guessed; use a disposable emulator and test account regardless.
_BLOCKED_LABELS = re.compile(
    r"(?<![a-z0-9])(?:delete|remove|purchase|buy now|checkout|place order|pay(?:ment)?|transfer|"
    r"send(?: now)?|submit|publish|post(?: now)?|logout|log out|sign out|uninstall|"
    r"clear data|reset|factory reset)(?![a-z0-9])|"
    r"删除|移除|购买|付款|支付|转账|发送|提交|发布|下单|退出登录|注销|卸载|清空|重置",
    re.IGNORECASE,
)
_NAVIGATION_ONLY_BLOCKED = re.compile(
    r"play|pause|next|previous|song|track|album|cover|minibar|carousel|banner|"
    r"播放|暂停|下一首|上一首|歌曲|专辑|封面|播放列表",
    re.IGNORECASE,
)
_NAVIGATION_ONLY_ALLOWED = re.compile(
    r"(?<![a-z0-9])(?:home|back|close|dismiss|skip|menu|tab|navigation|settings?|profile|library|local)(?![a-z0-9])|"
    r"首页|主页|返回|关闭|跳过|菜单|更多|导航|设置|我的|本地|听书|音乐|视频|活动",
    re.IGNORECASE,
)


def parse_bounds(value: str | None) -> Bounds | None:
    if not value:
        return None
    match = _BOUNDS_RE.fullmatch(value.strip())
    if not match:
        return None
    left, top, right, bottom = map(int, match.groups())
    if right <= left or bottom <= top:
        return None
    return Bounds(left, top, right, bottom)


def _truthy(value: str | None) -> bool:
    return (value or "").lower() == "true"


HierarchySignature = Counter[tuple[tuple[str, ...], ...]]


def hierarchy_signature(xml_text: str) -> HierarchySignature:
    """Count node paths in the hierarchy, ignoring volatile attributes.

    Each node contributes a token built from its resource-id, or from its
    class/label when it has no id (UI dumps may omit ids on custom views).
    The node's path from the root is the key. Exact bounds are intentionally
    excluded because dynamic pages rotate carousels and list geometry on every
    visit while the id/class skeleton stays put.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return Counter()
    signature: HierarchySignature = Counter()

    def token(node: ET.Element) -> tuple[str, ...]:
        resource_id = node.attrib.get("resource-id", "").strip()
        if resource_id:
            return ("id", resource_id)
        return (
            "anon",
            node.attrib.get("class", "").strip(),
            node.attrib.get("text", "").strip(),
            node.attrib.get("content-desc", "").strip(),
        )

    def walk(node: ET.Element, prefix: tuple[tuple[str, ...], ...]) -> None:
        path = prefix + (token(node),)
        signature[path] += 1
        for child in node:
            if child.tag == "node":
                walk(child, path)

    if root.tag == "hierarchy":
        for child in root:
            if child.tag == "node":
                walk(child, ())
    else:
        walk(root, ())
    return signature


def structure_similarity(first: Counter, second: Counter) -> float:
    """Counter Jaccard similarity; 1.0 means an identical control makeup."""
    if not first or not second:
        return 0.0
    common = sum((first & second).values())
    total = sum((first | second).values())
    return common / total if total else 0.0


def extract_actions(
    xml_text: str,
    screen_width: int,
    screen_height: int,
    navigation_only: bool = False,
) -> list[Action]:
    """Return visible enabled click targets and a pair of scroll gestures.

    The XML is UIAutomator's hierarchy format. Off-screen/empty targets,
    duplicate click targets, and common destructive/financial actions are
    excluded. Scroll gestures are global because Android does not expose a
    reliable generic scroll action for every custom view.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    actions: list[Action] = []
    seen: set[tuple[int, int]] = set()
    for node in root.iter("node"):
        if not _truthy(node.attrib.get("clickable")) or not _truthy(node.attrib.get("enabled", "true")):
            continue
        if node.attrib.get("visible-to-user", "true").lower() == "false":
            continue
        bounds = parse_bounds(node.attrib.get("bounds"))
        if not bounds:
            continue
        # Reject fully off-screen elements first; then clip partial overlaps.
        if bounds.right <= 0 or bounds.bottom <= 0 or bounds.left >= screen_width or bounds.top >= screen_height:
            continue
        left = max(0, min(bounds.left, screen_width - 1))
        right = max(0, min(bounds.right, screen_width))
        top = max(0, min(bounds.top, screen_height - 1))
        bottom = max(0, min(bounds.bottom, screen_height))
        if right <= left or bottom <= top:
            continue

        text = node.attrib.get("text", "").strip()
        description = node.attrib.get("content-desc", "").strip()
        resource_id = node.attrib.get("resource-id", "").strip()
        label = " ".join((text, description, resource_id))
        if _BLOCKED_LABELS.search(label):
            continue
        if navigation_only and (
            bounds.height > screen_height * 0.12
            or bounds.width > screen_width * 0.55
            or bounds.width < screen_width * 0.05
            or bounds.height < screen_height * 0.025
            or _NAVIGATION_ONLY_BLOCKED.search(label)
            or not _NAVIGATION_ONLY_ALLOWED.search(label)
        ):
            # Avoid ambiguous full-screen content regions, play controls,
            # and tiny icon buttons. This mode is a safer smoke-test profile,
            # not a guarantee that remaining navigation is harmless.
            continue

        x, y = ((left + right) // 2, (top + bottom) // 2)
        action = Action(
            kind="click",
            x=x,
            y=y,
            text=text,
            resource_id=resource_id,
            content_desc=description,
            bounds=Bounds(left, top, right, bottom),
        )
        # Parent/child nodes often share one clickable region. A single tap
        # at the same center is one exploration action even when labels differ.
        point = (x, y)
        if point not in seen:
            actions.append(action)
            seen.add(point)

    # Keep gestures away from system bars. They're useful for lazy lists and
    # custom scroll containers, and are bounded to one attempt per state.
    x = max(1, screen_width // 2)
    top = max(1, int(screen_height * 0.28))
    bottom = max(top + 1, min(screen_height - 1, int(screen_height * 0.76)))
    actions.extend(
        [
            Action("swipe_up", x=x, y=bottom, x2=x, y2=top),
            Action("swipe_down", x=x, y=top, x2=x, y2=bottom),
            Action("back"),
        ]
    )
    return actions
