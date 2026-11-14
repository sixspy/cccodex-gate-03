from datetime import datetime, time, timedelta

RULE_NATURAL = "natural"
RULE_CUTOFF = "cutoff"
RULE_CHOICES = (RULE_NATURAL, RULE_CUTOFF)


def parse_cutoff(text):
    """把 '23:00' 读成 time(23, 0)，读不出返回 None。"""
    parts = str(text or "").strip().split(":")
    if len(parts) < 2:
        return None
    try:
        return time(int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def rule_label(settings):
    """规则名，日切列表和层详情都显示它。"""
    if not settings:
        return "日切未钉住"
    if settings["rule"] == RULE_CUTOFF and settings.get("cutoff_time") is not None:
        return f"过{settings['cutoff_time']:%H:%M}日切线改记次日"
    return "按扫描时刻落入自然日"


def compute_day(settings, stamp):
    """按钉住的日切规则，算出扫描时刻该记的业务日（改记日）。"""
    day = stamp.date()
    if (
        settings
        and settings["rule"] == RULE_CUTOFF
        and settings.get("cutoff_time") is not None
        and stamp.time() >= settings["cutoff_time"]
    ):
        return day + timedelta(days=1)
    return day


def scan_time(payload):
    """读出扫描时刻。时刻空着或读不出的扫描进不了跨日队列。"""
    stamp = str(payload.get("at") or "").strip()
    if not stamp:
        return None, "时刻空着"
    try:
        return datetime.fromisoformat(stamp), None
    except ValueError:
        return None, "扫描时间无法读取"
