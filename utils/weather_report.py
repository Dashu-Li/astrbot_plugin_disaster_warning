"""Conservative comparison of the known CMA relay packaging of one warning.

Raw payloads and display text are kept by the parser. These values are only
used for an identical warning ID; unrelated releases must retain strict matching.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any


_CHINA = timezone(timedelta(hours=8))
_SOURCE_NOTE = "（预警信息来源：国家预警信息发布中心）"
_ISSUE_HEADER = re.compile(
    r"^(?P<issuer>[\u4e00-\u9fff]{2,30}气象台)"
    r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
    r"(?P<hour>\d{1,2})时(?P<minute>\d{1,2})分"
    r"(?P<action>继续发布|发布|更新)"
    r"(?P<product>[\u4e00-\u9fff]{1,12}(?:蓝|黄|橙|红|白)色预警)"
    r"(?:信号)?[。：:]"
)
_TITLE = re.compile(
    r"^(?P<region>[\u4e00-\u9fff]{2,60})"
    r"(?P<action>继续发布|发布|更新)"
    r"(?P<product>[\u4e00-\u9fff]{1,12}(?:蓝|黄|橙|红|白)色预警)(?:信号)?$"
)
_SEVERITY = {"白色": None, "蓝色": "IV级/一般", "黄色": "III级/较重",
             "橙色": "II级/严重", "红色": "I级/特别严重"}
# The standard fifteen p-code types and colors mirror the local
# weather_alarm_code_map. Keep this helper pure: importing the presenter package
# from parsers would introduce a circular import. Private/unknown codes retain
# strict comparison, as do known codes contradicting the validated body.
_P_TYPES = {
    "0001": "台风", "0002": "暴雨", "0003": "高温", "0004": "寒潮",
    "0005": "大雾", "0006": "暴雪", "0007": "大风", "0008": "沙尘暴",
    "0009": "冰雹", "0010": "干旱", "0011": "道路结冰", "0012": "雷电",
    "0013": "霜冻", "0014": "霾", "0015": "雷雨大风",
}
_P_COLORS = {"1": "红色", "2": "橙色", "3": "黄色", "4": "蓝色"}
_STANDARD_P_CODE = re.compile(r"p(?P<type>\d{4})00(?P<color>[1-4])")
_AUXILIARY_FIELDS = ("province", "city", "district", "adcode", "latitude", "longitude")


def _canonical_weather_code(value: Any, product: str) -> Any:
    """Only missing or provably equivalent standard codes are packaging."""
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        match = _STANDARD_P_CODE.fullmatch(value)
        if match is not None:
            hazard = _P_TYPES.get(match["type"])
            color = _P_COLORS[match["color"]]
            if hazard is not None and product == hazard + color + "预警":
                return ""
    return value


def canonical_weather_report(
    fields: dict[str, Any], *, effective_is_issued: bool = False
) -> dict[str, Any] | None:
    """Return a known-format comparison, or None to retain strict behavior.

    A complete issuing-station/date/action/product header, matching title and
    valid source issue time within ten minutes are all required. Effective clocks
    differing from publication, expiry, conflicting levels/codes and status stay
    significant.
    Once the body establishes the issuer and affected area, auxiliary geographic
    metadata and consistent color/code completion do not create another release.
    Cancellation keeps its structured clock, even if it carries an old body.
    """
    if fields.get("is_cancel") or fields.get("status") == "cancelled":
        return None
    body = fields.get("description")
    title = fields.get("title")
    if not isinstance(body, str) or not isinstance(title, str):
        return None
    body = body.strip()
    header = _ISSUE_HEADER.match(body)
    title_match = _TITLE.fullmatch(title.strip())
    if header is None or title_match is None:
        return None
    if (title_match["product"] != header["product"]
            or not title_match["region"].endswith(header["issuer"][:-3])):
        return None
    try:
        issued = datetime(
            *(int(header[key]) for key in ("year", "month", "day", "hour", "minute")),
            tzinfo=_CHINA,
        )
        raw_issued = datetime.fromisoformat(str(fields.get("issued") or ""))
        if raw_issued.tzinfo is None:
            return None
        if abs((issued - raw_issued).total_seconds()) > 600:
            return None
    except (ValueError, TypeError, OverflowError):
        return None

    normalized = dict(fields)
    # Keep unknown business fields significant; ignore only the six declared
    # relay auxiliaries, without changing the payload, display or region filter.
    for key in _AUXILIARY_FIELDS:
        normalized.pop(key, None)
    color = next(color for color in _SEVERITY
                 if header["product"].endswith(color + "预警"))
    if fields.get("level") in (None, "", color, _SEVERITY[color]):
        normalized["level"] = color
    normalized["weather_code"] = _canonical_weather_code(
        fields.get("weather_code"), header["product"]
    )
    if body.endswith(_SOURCE_NOTE):
        body = body[:-len(_SOURCE_NOTE)].rstrip()
    normalized["description"] = body
    normalized["title"] = (
        title_match["region"] + title_match["action"] + title_match["product"]
    )
    # An absent headline, the title fallback, and this exact standard headline
    # are presentation variants. Unknown headlines remain real report content.
    headline = str(fields.get("headline") or "").strip()
    standard = header["issuer"] + r"(?:继续发布|发布|更新)" + re.escape(header["product"])
    severity = next((value for color, value in _SEVERITY.items()
                     if header["product"].endswith(color + "预警")), None)
    suffix = r"(?:信号)?" + (r"(?:\[" + re.escape(severity) + r"\])?" if severity else "")
    if (not headline or headline == title.strip()
            or re.fullmatch(standard + suffix, headline)):
        normalized["headline"] = ""
    normalized["issued"] = issued.astimezone(timezone.utc).isoformat()
    if effective_is_issued:
        normalized["effective"] = normalized["issued"]
    return normalized
