"""
气象规则。
负责按预警颜色等级与关键词白名单筛选气象事件。
"""

from __future__ import annotations

import re

from ...utils.china_regions import province_short
from ..domain.event_models import WeatherEvent
from ..domain.event_payload import SourcePayload
from .base_rule import BaseRule, RuleContext
from .rule_result import RuleDecision


_REGION_ENDINGS = (
    "特别行政区", "自治区", "自治州", "自治县", "地区", "省", "市", "县", "区", "旗", "盟"
)
_STATION_PREFIX = re.compile(r"^\s*([\u4e00-\u9fff]{2,40}?)(?:气象台|气象局)")
_PUBLISHING_PREFIX = re.compile(
    r"^\s*([\u4e00-\u9fff]{2,40}?)(?:继续发布|更新发布|发布|解除|取消|变更|升级|降级)"
)


def _matches_issuing_region(issuer: str, excluded: str) -> bool:
    """Match an issuing region, never a place mentioned in the warning body.

    A full hierarchy may precede the region (省/市/县), but arbitrary suffix
    matches must not turn 长丰县 into 丰县. Station names may omit the final
    administrative suffix, e.g. 肥东气象台 versus 肥东县.
    """
    names = [excluded]
    if not issuer.endswith(_REGION_ENDINGS):
        for ending in _REGION_ENDINGS:
            if excluded.endswith(ending) and len(excluded) > len(ending):
                names.append(excluded[: -len(ending)])
                break
    elif not excluded.endswith(_REGION_ENDINGS):
        names.extend(excluded + ending for ending in _REGION_ENDINGS)
    for name in names:
        if not issuer.endswith(name):
            continue
        prefix = issuer[: -len(name)]
        if not prefix or prefix.endswith(_REGION_ENDINGS):
            return True
    return False


def _excluded_issuing_region(domain_event, sources, excluded_regions) -> str:
    """Prefer the original station header over title wrappers or geo fields.

    Province/city warnings can mention excluded counties in their coverage.
    Only a leading issuing station, a publication title or finally a structured
    district is considered. Conflicting issuers are kept conservatively.
    """
    def consensus(issuers):
        found = ""
        for issuer in dict.fromkeys(issuers):
            match = next(
                (region for region in excluded_regions
                 if _matches_issuing_region(issuer, region)),
                "",
            )
            if not match:
                return ""
            found = found or match
        return found

    def extract(texts, pattern):
        return [
            match.group(1)
            for text in texts
            if isinstance(text, str)
            if (match := pattern.match(text))
        ]

    # 正文开头的气象台发布句最接近原始发布机构，不扫描正文中的覆盖地区。
    issuers = extract(
        (source.get("description") for source in sources), _STATION_PREFIX
    )
    if issuers:
        return consensus(issuers)

    title_fields = [
        getattr(domain_event, "title", ""),
        getattr(domain_event, "headline", ""),
    ]
    title_fields.extend(
        source.get(key) for source in sources for key in ("title", "headline")
    )
    issuers = extract(title_fields, _STATION_PREFIX)
    if issuers:
        return consensus(issuers)
    issuers = extract(title_fields, _PUBLISHING_PREFIX)
    if issuers:
        return consensus(issuers)

    districts = [
        source.get("district").strip()
        for source in sources
        if isinstance(source.get("district"), str)
        and source.get("district").strip()
    ]
    return consensus(districts)


class WeatherRule(BaseRule):
    """旧气象过滤器的规则化包装。"""

    rule_name = "weather_rule"

    def evaluate(self, context: RuleContext) -> RuleDecision:
        """按颜色等级与地区关键词对白名单气象事件做过滤。"""
        domain_event = context.domain_event
        # 仅针对气象预警事件生效
        if not isinstance(domain_event, WeatherEvent):
            return RuleDecision.accept(reason="非气象事件，跳过气象规则")

        weather_filter = context.policy_state.get("weather_filter") or {}
        if not weather_filter:
            return RuleDecision.accept(reason="未配置气象过滤器")

        # 检查是否启用了气象过滤器开关
        if not weather_filter.get("enabled", False):
            return RuleDecision.accept(reason="气象规则未启用")

        # 标题与说明可能分散在领域对象、元数据和原始载荷中，这里统一兜底提取。
        envelope_payload = context.envelope.payload
        payload = (
            envelope_payload.to_dict()
            if isinstance(envelope_payload, SourcePayload)
            else {}
        )
        metadata = (
            context.envelope.metadata
            if isinstance(context.envelope.metadata, dict)
            else {}
        )
        domain_metadata = (
            domain_event.metadata if isinstance(domain_event.metadata, dict) else {}
        )
        # 颜色仍按原有标题优先级识别，不把正文中的其他颜色当作本次预警等级。
        title_text = next(
            (
                text
                for text in (
                    getattr(domain_event, "title", ""),
                    getattr(domain_event, "headline", ""),
                    metadata.get("title", ""),
                    metadata.get("headline", ""),
                    payload.get("title", ""),
                    payload.get("headline", ""),
                )
                if isinstance(text, str) and text
            ),
            "",
        )
        min_color_level = weather_filter.get("min_color_level", "白色")

        # 规范化关键字过滤列表
        keywords = [
            str(keyword).strip()
            for keyword in weather_filter.get("keywords", [])
            if str(keyword).strip()
        ]
        legacy_provinces = [
            str(keyword).strip()
            for keyword in weather_filter.get("provinces", [])
            if str(keyword).strip()
        ]
        # 兼容老版本用户遗留的 provinces 属性作为区域白名单
        if not keywords:
            keywords = legacy_provinces
        elif legacy_provinces:
            keywords = list(dict.fromkeys([*keywords, *legacy_provinces]))

        color_levels = {"白色": 0, "蓝色": 1, "黄色": 2, "橙色": 3, "红色": 4}

        # 颜色按由高到低的优先级识别，命中后即可停止继续扫描。
        detected_color = "白色"
        for color in ["红色", "橙色", "黄色", "蓝色", "白色"]:
            if color in title_text:
                detected_color = color
                break

        # 对比颜色等级阈值进行过滤
        if color_levels.get(detected_color, 0) < color_levels.get(min_color_level, 0):
            return RuleDecision.reject(
                reason="气象颜色级别过滤",
                detail=f"当前颜色 {detected_color} 低于最低要求 {min_color_level}",
                context={
                    "detected_color": detected_color,
                    "min_color_level": min_color_level,
                },
            )

        excluded_regions = weather_filter.get("excluded_regions", [])
        excluded_regions = (
            [
                region.strip()
                for region in excluded_regions
                if isinstance(region, str) and region.strip()
            ]
            if isinstance(excluded_regions, list)
            else []
        )
        if excluded_regions:
            excluded_region = _excluded_issuing_region(
                domain_event, (domain_metadata, metadata, payload), excluded_regions
            )
            if excluded_region:
                return RuleDecision.reject(
                    reason="气象发布地区排除过滤",
                    detail=f"预警由已排除的地区 {excluded_region} 单独发布",
                    context={"excluded_region": excluded_region},
                )

        # 地区匹配需检查所有标题、摘要和完整正文，不能让非空摘要遮蔽正文。
        # 仅收集字符串，且用换行隔开字段，避免字段间拼出并不存在的地区关键词。
        headline_text = "\n".join(
            dict.fromkeys(
                text
                for text in (
                    getattr(domain_event, "title", ""),
                    getattr(domain_event, "headline", ""),
                    *(
                        source.get(key, "")
                        for source in (domain_metadata, metadata, payload)
                        for key in ("title", "headline", "description")
                    ),
                )
                if isinstance(text, str) and text
            )
        )

        # 白名单关键词校验：要求标题、灾害描述正文或解析出的地区归属中命中关键词
        if keywords:
            title_hits = [keyword for keyword in keywords if keyword in title_text]
            headline_hits = [
                keyword for keyword in keywords if keyword in headline_text
            ]

            def region_value(key: str) -> str:
                # 空白及非字符串不是可靠地区；保留元数据优先、原始载荷兜底。
                for source in (metadata, payload):
                    value = source.get(key)
                    if not isinstance(value, str) or not value.strip():
                        continue
                    raw_value = payload.get(key)
                    # 解析器可能将畸形原始值 str() 化；这不是有效的结构化地区。
                    if (
                        source is metadata
                        and key in payload
                        and not isinstance(raw_value, str)
                        and value.strip() == str(raw_value or "").strip()
                    ):
                        continue
                    return value.strip()
                return ""

            province_val = region_value("province")
            city_val = region_value("city")
            district_val = region_value("district")
            geo_text = f"{province_val} {city_val} {district_val}".strip()

            geo_hits = [
                keyword
                for keyword in keywords
                if (geo_text and keyword in geo_text)
                or (
                    province_val
                    and (
                        keyword == province_val
                        or keyword == province_short(province_val)
                    )
                )
            ]

            # 明确的市/区县归属优先，正文偶然提及其他城市不能扩大覆盖范围。
            # 只有省级提示时不足以排除具体城市，仍需完整正文兜底。
            has_affected_region = bool(city_val or district_val)
            # 区县可能没有父级城市字段；此时发布标题/摘要可补充市名，
            # 完整正文仍不参与有明确区县归属的兜底，避免偶然提及外地。
            publication_text = "\n".join(
                text.split("预警", 1)[0]
                for text in (
                    getattr(domain_event, "title", ""),
                    getattr(domain_event, "headline", ""),
                    *(source.get(key, "")
                      for source in (domain_metadata, metadata, payload)
                      for key in ("title", "headline")),
                )
                if isinstance(text, str)
            )
            publication_hits = [keyword for keyword in keywords if keyword in publication_text]
            matched = bool(geo_hits) or (
                not city_val and bool(publication_hits)
            ) or (
                not has_affected_region and bool(headline_hits)
            )
            if not matched:
                return RuleDecision.reject(
                    reason="气象关键词白名单过滤",
                    detail=(
                        "明确的市/区县归属未命中关键词白名单"
                        if has_affected_region
                        else "标题、正文与地区归属均未命中关键词白名单"
                    ),
                    context={
                        "keywords": keywords,
                        "title_hits": title_hits,
                        "headline_hits": headline_hits,
                        "geo_hits": geo_hits,
                        "structured_region_authoritative": has_affected_region,
                    },
                )

        return RuleDecision.accept(reason="气象规则通过")
