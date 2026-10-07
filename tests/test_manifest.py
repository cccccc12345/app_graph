import struct

from app_graph.manifest import ManifestActivity, launchable_components, parse_manifest

_TYPE_XML = 0x0003
_TYPE_STRING_POOL = 0x0001
_TYPE_RESOURCE_MAP = 0x0180
_TYPE_START_ELEMENT = 0x0102
_TYPE_END_ELEMENT = 0x0103

_VALUE_STRING = 0x03
_VALUE_BOOL = 0x12

_ATTR_NAME = 0x01010003
_ATTR_ENABLED = 0x0101000E
_ATTR_EXPORTED = 0x01010010
_ATTR_TARGET = 0x01010202


def _string_pool(strings):
    blob = b""
    offsets = []
    for text in strings:
        encoded = text.encode("utf-8")
        assert len(text) < 0x80 and len(encoded) < 0x80
        offsets.append(len(blob))
        blob += bytes([len(text), len(encoded)]) + encoded + b"\x00"
    offset_blob = b"".join(struct.pack("<I", offset) for offset in offsets)
    header_size = 28
    strings_start = header_size + len(offset_blob)
    chunk = struct.pack("<HHI", _TYPE_STRING_POOL, header_size, strings_start + len(blob))
    chunk += struct.pack("<IIIII", len(strings), 0, 0x100, strings_start, 0)
    return chunk + offset_blob + blob


def _resource_map(resource_ids):
    chunk = struct.pack("<HHI", _TYPE_RESOURCE_MAP, 8, 8 + 4 * len(resource_ids))
    return chunk + b"".join(struct.pack("<I", value) for value in resource_ids)


def _start_element(name_index, attrs):
    body = struct.pack("<II", 0xFFFFFFFF, name_index)
    body += struct.pack("<HHHHHH", 20, 20, len(attrs), 0, 0, 0)
    for attr_name_index, value_type, value_data in attrs:
        body += struct.pack("<III", 0xFFFFFFFF, attr_name_index, 0xFFFFFFFF)
        body += struct.pack("<HBBI", 8, 0, value_type, value_data)
    size = 16 + len(body)
    return (
        struct.pack("<HHI", _TYPE_START_ELEMENT, 16, size)
        + struct.pack("<II", 0, 0xFFFFFFFF)
        + body
    )


def _end_element(name_index):
    return (
        struct.pack("<HHI", _TYPE_END_ELEMENT, 16, 24)
        + struct.pack("<II", 0, 0xFFFFFFFF)
        + struct.pack("<II", 0xFFFFFFFF, name_index)
    )


def _build_manifest():
    # Attribute names must occupy the first string pool slots so the resource
    # map can map them to their android: resource ids.
    strings = [
        "name",
        "enabled",
        "exported",
        "targetActivity",
        "activity",
        "activity-alias",
        "intent-filter",
        "com.example",
        ".Main",
        ".Hidden",
        ".Alias",
    ]
    name, enabled, exported, target = 0, 1, 2, 3
    activity, alias, intent_filter = 4, 5, 6
    main, hidden, alias_name = 8, 9, 10

    body = _string_pool(strings)
    body += _resource_map([_ATTR_NAME, _ATTR_ENABLED, _ATTR_EXPORTED, _ATTR_TARGET])
    body += _start_element(activity, [(name, _VALUE_STRING, main)])
    body += _start_element(intent_filter, [])
    body += _end_element(intent_filter)
    body += _end_element(activity)
    body += _start_element(
        activity,
        [(name, _VALUE_STRING, hidden), (exported, _VALUE_BOOL, 0), (enabled, _VALUE_BOOL, 0)],
    )
    body += _end_element(activity)
    body += _start_element(
        alias,
        [(name, _VALUE_STRING, alias_name), (target, _VALUE_STRING, main), (exported, _VALUE_BOOL, 0xFFFFFFFF)],
    )
    body += _end_element(alias)
    return struct.pack("<HHI", _TYPE_XML, 8, 8 + len(body)) + body


def test_parse_manifest_extracts_activities():
    activities = parse_manifest(_build_manifest(), "com.example")
    by_name = {activity.name: activity for activity in activities}

    main = by_name["com.example.Main"]
    assert main.component == "com.example/com.example.Main"
    assert main.exported is True  # defaults to true because it has a filter
    assert main.has_filter is True
    assert main.enabled is True

    hidden = by_name["com.example.Hidden"]
    assert hidden.exported is False
    assert hidden.enabled is False

    alias = by_name["com.example.Alias"]
    assert alias.alias_target == "com.example.Main"
    assert alias.exported is True


def test_launchable_components_prefers_real_activity_over_alias():
    activities = parse_manifest(_build_manifest(), "com.example")
    assert launchable_components(activities) == ["com.example/com.example.Main"]


def test_launchable_components_keeps_alias_without_declared_target():
    alias = ManifestActivity(
        name="com.example.Alias",
        component="com.example/com.example.Alias",
        exported=True,
        enabled=True,
        has_filter=True,
        alias_target="com.example.NotDeclared",
    )
    assert launchable_components([alias]) == ["com.example/com.example.Alias"]
