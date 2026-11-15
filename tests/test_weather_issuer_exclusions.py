"""Issuer filtering with invented regions and synthetic events only."""

import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest


SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "_issuer_rule_fixture", SOURCE / "tests/test_weather_rule.py"
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class WeatherIssuerExclusionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = fixture.load_modules()

    def evaluate(self, *, excluded=None, enabled=True, **kwargs):
        rule_fixture = fixture.WeatherRuleTests()
        rule_fixture.modules = self.modules
        policy = {
            "enabled": enabled,
            "keywords": [],
            "min_color_level": "蓝色",
            "excluded_regions": ["东岭县"] if excluded is None else excluded,
        }
        kwargs.setdefault("title", "大雾黄色预警")
        kwargs.setdefault("headline", "大雾黄色预警")
        context = rule_fixture.context(weather_filter=policy, **kwargs)
        return self.modules.rule.WeatherRule().evaluate(context)

    def test_publication_prefixes_and_hierarchy(self):
        for prefix in ("东岭县", "示例省东岭县", "示例省示例市东岭县"):
            with self.subTest(prefix=prefix):
                result = self.evaluate(title=prefix + "发布大雾黄色预警")
                self.assertFalse(result.accepted)
                self.assertEqual(result.reason, "气象发布地区排除过滤")

    def test_station_headers_and_abbreviated_region(self):
        for region in ("东岭县", "东岭"):
            for source in ("title", "headline", "domain_metadata", "metadata", "raw"):
                with self.subTest(region=region, source=source):
                    text = region + "气象台发布大雾黄色预警"
                    value = text if source in ("title", "headline") else {"description": text}
                    self.assertFalse(self.evaluate(**{source: value}).accepted)

    def test_structured_district_is_last_fallback(self):
        for source in ("domain_metadata", "metadata", "raw"):
            with self.subTest(source=source):
                self.assertFalse(self.evaluate(**{source: {"district": "东岭县"}}).accepted)

    def test_broad_issuer_and_body_mentions_remain_allowed(self):
        for issuer in ("示例省", "示例市"):
            with self.subTest(issuer=issuer):
                self.assertTrue(self.evaluate(
                    title=issuer + "发布大雾黄色预警",
                    metadata={"district": "东岭县"},
                    raw={"description": issuer + "气象台发布大雾黄色预警，覆盖东岭县。"},
                ).accepted)
                self.assertTrue(self.evaluate(
                    title=issuer + "发布大雾黄色预警",
                    raw={"description": "预警覆盖东岭县，请注意防范。"},
                ).accepted)

    def test_conflicting_issuers_are_kept(self):
        self.assertTrue(self.evaluate(
            headline="示例市气象台发布大雾黄色预警",
            raw={"headline": "东岭县气象台发布大雾黄色预警"},
        ).accepted)

    def test_suffix_match_requires_administrative_boundary(self):
        for issuer in ("新东岭县", "长东岭县"):
            with self.subTest(issuer=issuer):
                self.assertTrue(self.evaluate(title=issuer + "发布大雾黄色预警").accepted)

    def test_empty_malformed_disabled_and_unknown_inputs(self):
        for excluded in ([], "东岭县", 123, [None, {}]):
            with self.subTest(excluded=excluded):
                self.assertTrue(self.evaluate(
                    title="东岭县发布大雾黄色预警", excluded=excluded
                ).accepted)
        self.assertTrue(self.evaluate(title="东岭县发布大雾黄色预警", enabled=False).accepted)
        for value in (None, 123, False, [], {}):
            with self.subTest(value=value):
                self.assertTrue(self.evaluate(raw={"description": value, "district": value}).accepted)

    def test_color_threshold_is_preserved(self):
        result = self.evaluate(title="东岭县发布大雾白色预警")
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "气象颜色级别过滤")

    def test_empty_default_and_session_override_use_real_manager(self):
        schema = json.loads((SOURCE / "_conf_schema.json").read_text())
        node = schema["weather_config"]["items"]["weather_filter"]["items"]["excluded_regions"]
        self.assertEqual(node["default"], [])
        target = SOURCE / "core/storage/session_config_manager.py"
        session_spec = importlib.util.spec_from_file_location("_issuer_session_manager", target)
        session = importlib.util.module_from_spec(session_spec)
        session_spec.loader.exec_module(session)
        manager = object.__new__(session.SessionConfigManager)
        manager.schema = schema
        manager.default_config_ref = {"weather_config": {"weather_filter": {
            "enabled": True, "excluded_regions": ["东岭县"],
        }}}
        patch = {"weather_config": {"weather_filter": {"excluded_regions": []}}}
        self.assertEqual(manager._sanitize_patch(copy.deepcopy(patch)), patch)
        manager._overrides = {"synthetic:GroupMessage:override": patch}
        self.assertEqual(manager.get_effective_config("synthetic:GroupMessage:override")
                         ["weather_config"]["weather_filter"]["excluded_regions"], [])
        self.assertEqual(manager.get_effective_config("synthetic:GroupMessage:inherit")
                         ["weather_config"]["weather_filter"]["excluded_regions"], ["东岭县"])

    def test_excluded_regions_use_real_config_validator(self):
        # Keep service package initializers out of this isolated module import.
        # AstrBot, policy modules and the validator itself use their real code.
        for suffix in ("core.services", "core.services.config", "core.services.snet",
                       "core.network", "core.network.websocket"):
            name = fixture.PREFIX + "." + suffix
            if name not in sys.modules:
                package = ModuleType(name)
                package.__path__ = [str(SOURCE.joinpath(*suffix.split(".")))]
                sys.modules[name] = package
        validator = self.modules.load(
            "core.services.config.config_validation_service"
        ).ConfigValidator

        original = {
            "enabled": True,
            "keywords": ["示例市"],
            "min_color_level": "黄色",
            "excluded_regions": [" 东岭县 ", "东岭县", None, 123, {}, "", "   ", "西岭县"],
        }
        result = validator._validate_weather_config({"weather_filter": copy.deepcopy(original)})
        self.assertEqual(result["weather_filter"], {
            **original, "excluded_regions": ["东岭县", "西岭县"],
        })
        for value in (None, "东岭县", 123, False, {}):
            with self.subTest(value=value):
                result = validator._validate_weather_config({
                    "weather_filter": {"enabled": True, "excluded_regions": value},
                })
                self.assertEqual(result["weather_filter"]["excluded_regions"], [])
        legacy = {"enabled": True, "keywords": ["示例市"], "min_color_level": "黄色"}
        result = validator._validate_weather_config({"weather_filter": copy.deepcopy(legacy)})
        self.assertEqual(result["weather_filter"], legacy)


if __name__ == "__main__":
    unittest.main()
