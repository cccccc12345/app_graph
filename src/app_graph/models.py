"""Core data models for Android UI exploration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class Action:
    """A replayable UI gesture. Coordinates are in device screenshot pixels."""

    kind: str
    x: int = 0
    y: int = 0
    x2: int = 0
    y2: int = 0
    duration_ms: int = 350
    text: str = ""
    resource_id: str = ""
    content_desc: str = ""
    bounds: Bounds | None = None

    @property
    def key(self) -> str:
        # Include geometry to distinguish repeated unlabeled controls.
        if self.kind == "click":
            return "|".join(
                (
                    self.kind,
                    self.resource_id,
                    self.text,
                    self.content_desc,
                    str(self.x),
                    str(self.y),
                )
            )
        return "|".join((self.kind, str(self.x), str(self.y), str(self.x2), str(self.y2)))

    @property
    def label(self) -> str:
        if self.kind == "click":
            detail = self.text or self.content_desc or self.resource_id.rsplit("/", 1)[-1]
            return f"click: {detail}" if detail else f"click ({self.x}, {self.y})"
        if self.kind == "swipe_up":
            return "swipe up"
        if self.kind == "swipe_down":
            return "swipe down"
        return self.kind

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Action":
        values = dict(data)
        bounds = values.get("bounds")
        if bounds is not None and not isinstance(bounds, Bounds):
            values["bounds"] = Bounds(**bounds)
        return cls(**values)
