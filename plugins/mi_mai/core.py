from __future__ import annotations

import json
import math
from typing import Any


DEFAULT_SETTINGS: dict[str, Any] = {
    "control_tools_enabled": False,
    "scene_tools_enabled": False,
    "only_whitelisted_users": True,
    "maiweave_enabled": False,
    "allowed_users": [],
    "devices": {},
    "scenes": {},
}

DEVICE_CATEGORIES = [
    "照明",
    "插座",
    "空调",
    "传感器",
    "窗帘",
    "清洁设备",
    "空气净化",
    "厨房电器",
    "安防",
    "其他",
]


def scene_key(scene_id: Any, home_id: Any) -> str:
    return f"{home_id}:{scene_id}"


def display_device(device: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    did = str(device.get("did", ""))
    preferences = config.get("devices", {}).get(did, {})
    return {
        "did": did,
        "name": str(device.get("name") or "未命名设备"),
        "model": str(device.get("model") or ""),
        "spec_type": str(device.get("spec_type") or ""),
        "home_id": str(device.get("home_id") or ""),
        "alias": str(preferences.get("alias") or ""),
        "category": str(preferences.get("category") or ""),
        "allowed": bool(preferences.get("allowed", False)),
        "properties": list(device.get("properties") or []),
        "actions": list(device.get("actions") or []),
        "states": list(device.get("states") or []),
        "status_error": bool(device.get("status_error", False)),
    }


def display_scene(scene: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    scene_id = str(scene.get("scene_id", ""))
    home_id = str(scene.get("home_id", ""))
    key = scene_key(scene_id, home_id)
    return {
        "key": key,
        "scene_id": scene_id,
        "home_id": home_id,
        "name": str(scene.get("name") or "未命名场景"),
        "allowed": bool(config.get("scenes", {}).get(key, False)),
    }


def normalize_user_id(identity: str, platform: str = "") -> str:
    identity = identity.strip()
    platform = platform.strip().lower()
    if ":" in identity:
        raw_platform, raw_id = identity.split(":", 1)
        raw_platform = raw_platform.strip().lower()
        raw_id = raw_id.strip()
        if raw_platform and raw_id:
            return f"{raw_platform}:{raw_id}"
        return ""
    if not identity:
        return ""
    return f"{platform}:{identity}" if platform else identity


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            result = value.model_dump()
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
    if hasattr(value, "__dict__"):
        return vars(value)
    return {}


def extract_user_identity(*contexts: Any) -> str:
    """Read a platform identity from trusted SDK message/event context."""
    queue = list(contexts)
    seen: set[int] = set()
    platform = ""
    user_id = ""
    identity_keys = {
        "user_id",
        "sender_id",
        "platform_user_id",
        "openid",
        "qq",
    }
    nested_keys = {
        "message",
        "message_info",
        "user_info",
        "sender",
        "sender_info",
        "event",
        "data",
        "detail",
        "message_base_info",
        "additional_data",
    }

    while queue:
        value = queue.pop(0)
        item = _mapping(value)
        if not item or id(item) in seen:
            continue
        seen.add(id(item))
        if not platform:
            for key in ("platform", "platform_name"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    platform = candidate.strip().lower()
                    break
        if not user_id:
            for key in identity_keys:
                candidate = item.get(key)
                if candidate is not None and str(candidate).strip():
                    user_id = str(candidate).strip()
                    break
        for key in nested_keys:
            child = item.get(key)
            if child is not None:
                queue.append(child)
        if user_id and platform:
            break

    if not user_id:
        return ""
    return normalize_user_id(user_id, platform)


def user_is_allowed(config: dict[str, Any], identity: str) -> bool:
    if not config.get("only_whitelisted_users", True):
        return True
    normalized = normalize_user_id(identity)
    if not normalized:
        return False
    allowed = {
        normalize_user_id(str(item))
        for item in config.get("allowed_users", [])
        if str(item).strip()
    }
    return normalized in allowed


def device_is_allowed(config: dict[str, Any], did: str) -> bool:
    device = config.get("devices", {}).get(str(did), {})
    return (
        bool(device.get("allowed"))
        and bool(str(device.get("alias", "")).strip())
        and str(device.get("category", "")).strip() in DEVICE_CATEGORIES
    )


def scene_is_allowed(config: dict[str, Any], scene_id: str, home_id: str) -> bool:
    return bool(config.get("scenes", {}).get(scene_key(scene_id, home_id), False))


def maiweave_should_defer(result: Any) -> bool:
    if not isinstance(result, dict) or result.get("decision") != "defer":
        return False
    return result.get("reason") in {"sleeping", "busy"}


def resolve_device(devices: list[dict[str, Any]], config: dict[str, Any], query: str) -> dict[str, Any] | None:
    target = query.strip().casefold()
    if not target:
        return None
    matches = []
    for device in devices:
        did = str(device.get("did", ""))
        preferences = config.get("devices", {}).get(did, {})
        names = {
            str(preferences.get("alias") or "").strip().casefold(),
            str(device.get("name") or "").strip().casefold(),
        }
        names.discard("")
        if target in names:
            matches.append(device)
    return matches[0] if len(matches) == 1 else None


def resolve_scene(scenes: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    target = query.strip().casefold()
    matches = [
        scene
        for scene in scenes
        if str(scene.get("name") or "").strip().casefold() == target
    ]
    return matches[0] if len(matches) == 1 else None


def parse_property_value(raw_value: str, prop: dict[str, Any]) -> Any:
    """Convert and validate a value using MIoT metadata before any write."""
    value_type = str(prop.get("type") or "")
    try:
        value = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        value = raw_value

    if value_type == "bool":
        if isinstance(value, bool):
            parsed = value
        elif isinstance(value, int) and value in (0, 1):
            parsed = bool(value)
        elif isinstance(value, str) and value.casefold() in {"true", "false", "0", "1"}:
            parsed = value.casefold() in {"true", "1"}
        else:
            raise ValueError("该属性需要布尔值")
    elif value_type in {"int", "uint", "integer"} or value_type.startswith(
        ("int", "uint")
    ):
        if isinstance(value, bool):
            raise ValueError("该属性需要整数")
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("该属性需要整数") from exc
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("该属性需要整数")
        if value_type.startswith("uint") and parsed < 0:
            raise ValueError("该属性不能小于 0")
    elif value_type in {"float", "double", "number"}:
        if isinstance(value, bool):
            raise ValueError("该属性需要数字")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("该属性需要数字") from exc
        if not math.isfinite(parsed):
            raise ValueError("该属性需要有限数字")
    elif value_type == "string":
        if not isinstance(value, str):
            raise ValueError("该属性需要文本")
        parsed = value
    else:
        raise ValueError("设备属性类型不受支持")

    value_range = prop.get("range")
    if isinstance(value_range, list) and len(value_range) >= 2 and isinstance(parsed, (int, float)):
        lower, upper = value_range[:2]
        if lower is not None and parsed < lower:
            raise ValueError(f"数值不能低于 {lower}")
        if upper is not None and parsed > upper:
            raise ValueError(f"数值不能高于 {upper}")
        if (
            len(value_range) >= 3
            and lower is not None
            and value_range[2] not in (None, 0, 1)
        ):
            step = value_range[2]
            offset = (parsed - lower) / step
            if abs(offset - round(offset)) > 1e-7:
                raise ValueError(f"数值步长为 {step}")

    allowed_values = prop.get("value_list") or prop.get("value-list")
    if isinstance(allowed_values, list) and allowed_values:
        choices = [item.get("value") for item in allowed_values if isinstance(item, dict)]
        if parsed not in choices:
            raise ValueError("该值不在设备支持的选项中")
    return parsed


def parse_action_arguments(raw_arguments: str, action: dict[str, Any]) -> list[Any]:
    try:
        arguments = json.loads(raw_arguments or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("动作参数必须是 JSON 数组") from exc
    if not isinstance(arguments, list):
        raise ValueError("动作参数必须是 JSON 数组")

    definitions = action.get("in") or action.get("inputs") or []
    if not isinstance(definitions, list) or len(arguments) != len(definitions):
        raise ValueError("动作参数数量不正确")

    parsed = []
    for value, definition in zip(arguments, definitions):
        if not isinstance(definition, dict):
            raise ValueError("动作参数规格无效")
        parsed.append(
            parse_property_value(
                json.dumps(value, ensure_ascii=False),
                definition,
            )
        )
    return parsed


def public_property(prop: dict[str, Any], value: Any = None, has_value: bool = False) -> dict[str, Any]:
    safe = {
        "name": str(prop.get("name") or ""),
        "description": str(prop.get("description") or ""),
        "type": str(prop.get("type") or ""),
        "rw": str(prop.get("rw") or ""),
        "range": prop.get("range"),
        "value_list": prop.get("value_list") or prop.get("value-list") or [],
    }
    if has_value:
        safe["value"] = value
    return safe
