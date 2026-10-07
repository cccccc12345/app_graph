"""Minimal parser for Android binary XML (AXML) manifests.

Only what activity seeding needs is extracted: declared ``<activity>`` and
``<activity-alias>`` elements with their exported/enabled flags, intent-filter
presence, and alias target. The parser works on the raw bytes of
``AndroidManifest.xml`` inside an APK; no Android SDK tools are required.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

_CHUNK_STRING_POOL = 0x0001
_CHUNK_RESOURCE_MAP = 0x0180
_CHUNK_START_ELEMENT = 0x0102
_CHUNK_END_ELEMENT = 0x0103

_ATTR_NAME = 0x01010003
_ATTR_ENABLED = 0x0101000E
_ATTR_EXPORTED = 0x01010010
_ATTR_TARGET_ACTIVITY = 0x01010202

_TYPE_STRING = 0x03
_TYPE_INT_BOOLEAN = 0x12


@dataclass(frozen=True)
class ManifestActivity:
    name: str
    component: str
    exported: bool
    enabled: bool
    has_filter: bool
    alias_target: str | None = None


def _decode_length(data: bytes, offset: int) -> tuple[int, int]:
    value = data[offset]
    offset += 1
    if value & 0x80:
        value = ((value & 0x7F) << 8) | data[offset]
        offset += 1
    return value, offset


def _parse_string_pool(data: bytes, offset: int) -> tuple[list[str], int]:
    chunk_type, header_size, chunk_size = struct.unpack_from("<HHI", data, offset)
    if chunk_type != _CHUNK_STRING_POOL:
        raise ValueError("not a string pool chunk")
    string_count, _style_count, flags, strings_start, _styles_start = struct.unpack_from(
        "<IIIII", data, offset + 8
    )
    utf8 = bool(flags & (1 << 8))
    offsets = struct.unpack_from(f"<{string_count}I", data, offset + header_size)
    base = offset + strings_start
    strings: list[str] = []
    for string_offset in offsets:
        position = base + string_offset
        if utf8:
            _, position = _decode_length(data, position)  # UTF-16 length, unused
            length, position = _decode_length(data, position)
            strings.append(data[position : position + length].decode("utf-8", errors="replace"))
        else:
            length = struct.unpack_from("<H", data, position)[0]
            position += 2
            strings.append(
                data[position : position + length * 2].decode("utf-16-le", errors="replace")
            )
    return strings, offset + chunk_size


def _string_attr(attrs: dict[int, tuple[int, int]], key: int, strings: list[str]) -> str | None:
    value = attrs.get(key)
    if value is None:
        return None
    value_type, data = value
    if value_type == _TYPE_STRING and 0 <= data < len(strings):
        return strings[data]
    return None


def _bool_attr(attrs: dict[int, tuple[int, int]], key: int, default: bool | None = None) -> bool | None:
    value = attrs.get(key)
    if value is None:
        return default
    value_type, data = value
    if value_type == _TYPE_INT_BOOLEAN:
        return data != 0
    return default


def parse_manifest(data: bytes, package: str) -> list[ManifestActivity]:
    """Parse the binary AndroidManifest.xml and return all declared activities."""
    _file_type, _header_size, file_size = struct.unpack_from("<HHI", data, 0)
    offset = 8
    strings: list[str] = []
    resource_map: list[int] = []
    activities: list[ManifestActivity] = []
    current: dict | None = None

    def resolve(name: str) -> str:
        if name.startswith("."):
            return package + name
        if "." not in name:
            return f"{package}.{name}"
        return name

    while 8 <= offset <= file_size - 8 and offset + 8 <= len(data):
        chunk_type, _chunk_header_size, chunk_size = struct.unpack_from("<HHI", data, offset)
        if chunk_size < 8:
            break
        if chunk_type == _CHUNK_STRING_POOL:
            strings, offset = _parse_string_pool(data, offset)
            continue
        if chunk_type == _CHUNK_RESOURCE_MAP:
            count = max(0, (chunk_size - 8) // 4)
            resource_map = list(struct.unpack_from(f"<{count}I", data, offset + 8))
        elif chunk_type == _CHUNK_START_ELEMENT:
            ns_index, name_index = struct.unpack_from("<II", data, offset + 16)
            del ns_index
            attribute_start, attribute_size, attribute_count = struct.unpack_from(
                "<HHH", data, offset + 24
            )
            attrs: dict[int, tuple[int, int]] = {}
            for index in range(attribute_count):
                position = offset + 16 + attribute_start + index * attribute_size
                _attr_ns, attr_name_index, _raw_value = struct.unpack_from("<III", data, position)
                value_type = data[position + 15]
                value_data = struct.unpack_from("<I", data, position + 16)[0]
                resource_id = (
                    resource_map[attr_name_index] if attr_name_index < len(resource_map) else 0
                )
                if resource_id:
                    attrs[resource_id] = (value_type, value_data)
            element = strings[name_index] if name_index < len(strings) else ""
            if element in {"activity", "activity-alias"}:
                current = {"attrs": attrs, "has_filter": False}
            elif element == "intent-filter" and current is not None:
                current["has_filter"] = True
        elif chunk_type == _CHUNK_END_ELEMENT:
            _ns_index, name_index = struct.unpack_from("<II", data, offset + 16)
            element = strings[name_index] if name_index < len(strings) else ""
            if element in {"activity", "activity-alias"} and current is not None:
                attrs = current["attrs"]
                declared = _string_attr(attrs, _ATTR_NAME, strings)
                if declared:
                    target = _string_attr(attrs, _ATTR_TARGET_ACTIVITY, strings)
                    exported = _bool_attr(attrs, _ATTR_EXPORTED)
                    enabled = _bool_attr(attrs, _ATTR_ENABLED, default=True)
                    has_filter = bool(current["has_filter"])
                    name = resolve(declared)
                    activities.append(
                        ManifestActivity(
                            name=name,
                            component=f"{package}/{name}",
                            exported=exported if exported is not None else has_filter,
                            enabled=bool(enabled),
                            has_filter=has_filter,
                            alias_target=resolve(target) if target else None,
                        )
                    )
                current = None
        offset += chunk_size
    return activities


def launchable_components(activities: list[ManifestActivity]) -> list[str]:
    """Return exported, enabled components, preferring real activities over aliases."""
    ordered: dict[str, str] = {}
    for activity in activities:
        if not activity.enabled or not activity.exported:
            continue
        key = activity.alias_target or activity.name
        if activity.alias_target is not None and key in ordered:
            continue
        ordered[key] = activity.component
    return list(ordered.values())
