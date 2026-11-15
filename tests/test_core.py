import unittest

from core import (
    DEFAULT_SETTINGS,
    device_is_allowed,
    extract_user_identity,
    maiweave_should_defer,
    normalize_user_id,
    parse_action_arguments,
    parse_property_value,
    resolve_device,
    scene_is_allowed,
    user_is_allowed,
)


class CorePolicyTests(unittest.TestCase):
    def test_defaults_keep_control_off_and_user_allowlist_on(self):
        self.assertFalse(DEFAULT_SETTINGS["control_tools_enabled"])
        self.assertFalse(DEFAULT_SETTINGS["scene_tools_enabled"])
        self.assertTrue(DEFAULT_SETTINGS["only_whitelisted_users"])

    def test_user_identity_requires_user_and_includes_platform_when_available(self):
        message = {
            "platform": "QQ",
            "sender": {"user_id": 12345},
        }
        self.assertEqual(extract_user_identity(message), "qq:12345")
        self.assertEqual(normalize_user_id("discord:alice"), "discord:alice")

    def test_missing_user_identity_fails_closed_by_default(self):
        config = dict(DEFAULT_SETTINGS)
        self.assertFalse(user_is_allowed(config, ""))
        config["only_whitelisted_users"] = False
        self.assertTrue(user_is_allowed(config, ""))

    def test_device_requires_explicit_allowlist_alias_and_category(self):
        config = {
            "devices": {
                "d1": {"allowed": True, "alias": "书桌灯", "category": "照明"},
                "d2": {"allowed": True, "alias": "", "category": "照明"},
                "d3": {"allowed": True, "alias": "传感器", "category": "未知"},
            }
        }
        self.assertTrue(device_is_allowed(config, "d1"))
        self.assertFalse(device_is_allowed(config, "d2"))
        self.assertFalse(device_is_allowed(config, "d3"))

    def test_scene_whitelist_is_scoped_to_home_and_scene(self):
        config = {"scenes": {"home-1:scene-1": True}}
        self.assertTrue(scene_is_allowed(config, "scene-1", "home-1"))
        self.assertFalse(scene_is_allowed(config, "scene-1", "home-2"))

    def test_maiweave_only_defers_clear_busy_or_sleeping_decisions(self):
        self.assertTrue(
            maiweave_should_defer({"decision": "defer", "reason": "sleeping"})
        )
        self.assertTrue(maiweave_should_defer({"decision": "defer", "reason": "busy"}))
        self.assertFalse(maiweave_should_defer({"decision": "defer", "reason": "quiet"}))
        self.assertFalse(
            maiweave_should_defer({"decision": "continue", "reason": "busy"})
        )

    def test_device_resolution_requires_unique_exact_alias_or_name(self):
        devices = [
            {"did": "d1", "name": "Lamp"},
            {"did": "d2", "name": "Lamp"},
        ]
        config = {
            "devices": {
                "d1": {"alias": "桌灯"},
                "d2": {"alias": "床灯"},
            }
        }
        self.assertEqual(resolve_device(devices, config, "桌灯")["did"], "d1")
        self.assertIsNone(resolve_device(devices, config, "Lamp"))

    def test_value_validation_supports_bounds_enums_and_bool(self):
        self.assertEqual(
            parse_property_value("50", {"type": "int", "range": [0, 100, 5]}),
            50,
        )
        self.assertEqual(
            parse_property_value("255", {"type": "uint8", "range": [0, 255, 1]}),
            255,
        )
        with self.assertRaises(ValueError):
            parse_property_value("52", {"type": "int", "range": [0, 100, 5]})
        with self.assertRaises(ValueError):
            parse_property_value(
                '"turbo"',
                {"type": "string", "value_list": [{"value": "auto"}]},
            )
        self.assertIs(
            parse_property_value("true", {"type": "bool"}),
            True,
        )

    def test_action_arguments_follow_device_specification(self):
        action = {
            "in": [
                {"name": "count", "type": "int", "range": [1, 10, 1]},
                {"name": "enabled", "type": "bool"},
            ]
        }
        self.assertEqual(parse_action_arguments("[3, true]", action), [3, True])
        with self.assertRaises(ValueError):
            parse_action_arguments("[11, true]", action)
        with self.assertRaises(ValueError):
            parse_action_arguments("[3]", action)
        self.assertEqual(parse_action_arguments("[]", {"in": []}), [])


if __name__ == "__main__":
    unittest.main()
