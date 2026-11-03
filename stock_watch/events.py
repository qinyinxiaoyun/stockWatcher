"""Conservative, source-backed business events and immutable factual versions.

Only explicit anchors prove cross-title identity. Extractors intentionally cover
a finite set of financial metrics and lifecycle facts; unsupported prose stays
article-scoped, never becoming executable instructions or model-made identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import re
from typing import Any

from .news import NewsArticle, NewsTarget, SHANGHAI, _clean_text, parse_published_at


RULE_VERSION = "business-facts-v1"
EVENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS business_events (
 event_id TEXT PRIMARY KEY, subject TEXT NOT NULL, event_type TEXT NOT NULL,
 anchor TEXT NOT NULL, strong_identity INTEGER NOT NULL,
 first_public_at TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS business_event_facts (
 event_id TEXT NOT NULL, fact_id TEXT NOT NULL, fact_key TEXT NOT NULL,
 fact_json TEXT NOT NULL, first_public_at TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(event_id,fact_id)
);
CREATE TABLE IF NOT EXISTS business_event_current (
 event_id TEXT NOT NULL, fact_key TEXT NOT NULL, fact_id TEXT NOT NULL,
 PRIMARY KEY(event_id,fact_key)
);
CREATE TABLE IF NOT EXISTS business_event_versions (
 event_id TEXT NOT NULL, change_id TEXT NOT NULL, facts_json TEXT NOT NULL,
 changes_json TEXT NOT NULL, first_public_at TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(event_id,change_id)
);
CREATE TABLE IF NOT EXISTS business_event_evidence (
 event_id TEXT NOT NULL, canonical_id TEXT NOT NULL, revision_key TEXT NOT NULL,
 source TEXT NOT NULL, url TEXT NOT NULL, published_at TEXT,
 observed_at TEXT NOT NULL, evidence_kind TEXT NOT NULL,
 PRIMARY KEY(event_id,canonical_id,revision_key,source)
);
CREATE TABLE IF NOT EXISTS business_event_fact_evidence (
 event_id TEXT NOT NULL, fact_id TEXT NOT NULL, canonical_id TEXT NOT NULL,
 revision_key TEXT NOT NULL, source TEXT NOT NULL, quote TEXT NOT NULL,
 PRIMARY KEY(event_id,fact_id,canonical_id,revision_key,source)
);
CREATE TABLE IF NOT EXISTS business_event_article_bindings (
 canonical_id TEXT NOT NULL, subject TEXT NOT NULL, event_id TEXT NOT NULL,
 PRIMARY KEY(canonical_id,subject)
);
"""


def _hash(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def _iso(value: datetime) -> str:
    return (value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value).astimezone(SHANGHAI).isoformat()


_TYPES = (
    ("earnings", r"财报|业绩|净利润|营收|营业收入|毛利率|现金流"),
    ("buyback", r"回购"), ("order", r"订单|合同|中标"),
    ("acquisition", r"并购|收购|重组"), ("capacity", r"产能|量产|投产|扩产"),
    ("regulatory", r"监管|调查|处罚|禁令|政策"), ("litigation", r"诉讼|仲裁"),
    ("credit", r"违约|破产|清算|偿债"),
)
_PERIOD = re.compile(r"(20\d{2})年?\s*(前三季度|前三季|三季报|第三季度|三季度|Q3|上半年|半年报|中报|H1|第一季度|一季度|一季报|Q1|第二季度|二季度|Q2|第四季度|四季度|Q4|全年|年度|年报)", re.I)
_PERIODS = {
    "前三季度": "9M", "前三季": "9M", "三季报": "9M", "第三季度": "Q3", "三季度": "Q3", "Q3": "Q3",
    "上半年": "H1", "半年报": "H1", "中报": "H1", "H1": "H1",
    "第一季度": "Q1", "一季度": "Q1", "一季报": "Q1", "Q1": "Q1",
    "第二季度": "Q2", "二季度": "Q2", "Q2": "Q2", "第四季度": "Q4", "四季度": "Q4", "Q4": "Q4",
    "全年": "FY", "年度": "FY", "年报": "FY",
}


def reporting_period(text: str) -> str:
    matches = _PERIOD.findall(text)
    periods = {year + ":" + _PERIODS[period.upper()] for year, period in matches}
    return next(iter(periods)) if len(periods) == 1 else ""


def strong_identity(article: NewsArticle, target: NewsTarget | str) -> tuple[str, str, str] | None:
    subject = target.name if isinstance(target, NewsTarget) else str(target)
    subject = re.sub(r"[-－](?:W|SW|S|B)$", "", _clean_text(subject), flags=re.I)
    title, summary = _clean_text(article.title), _clean_text(article.summary)
    text = title + "。" + summary
    if not subject or subject not in text:
        return None
    if isinstance(target, NewsTarget):
        if target.kind != "stock":
            return None
        # Operator display aliases never become issuer identity. Cross-listing
        # needs a verified issuer mapping; absent one, keep securities separate.
        subject = "stock:" + target.key.upper()
    event_type = next((name for name, pattern in _TYPES if re.search(pattern, title)), "unknown")
    if event_type == "unknown":
        event_type = next((name for name, pattern in _TYPES if re.search(pattern, summary)), "unknown")
    if event_type == "earnings":
        period = reporting_period(title) or reporting_period(summary)
        if period:
            return subject, event_type, "report:" + period
    if event_type == "buyback":
        plan = re.search(r"回购(?:计划|方案)(?:编号|ID)[:：]?\s*([A-Za-z0-9][A-Za-z0-9_-]{2,39})", text, re.I)
        if plan:
            return subject, event_type, "plan:" + plan[1].upper()
        date = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日(?:[^。；;]{0,15}?)(?:披露|发布|审议|通过|启动)(?:的)?(?:[^。；;]{0,5}?)回购(?:计划|方案)", text)
        if date:
            try:
                day = datetime(*map(int, date.group(1, 2, 3))).date().isoformat()
            except ValueError:
                return None
            return subject, event_type, "plan-date:" + day
    if event_type in {"order", "acquisition", "capacity"}:
        identifier = re.search(r"(?:订单|合同|项目)(?:编号|号)[:：]?\s*([A-Za-z0-9][A-Za-z0-9/_-]{2,59})", text)
        if identifier:
            return subject, event_type, "business-id:" + identifier[1].upper()
    announcement = re.search(r"公告编号[:：]?\s*([A-Za-z0-9]*20\d{2}[-—－][A-Za-z0-9-]{1,30})", text)
    if announcement and event_type != "unknown":
        return subject, event_type, "announcement:" + announcement[1].replace("—", "-").replace("－", "-").upper()
    return None


def anchored_identity(article: NewsArticle, target: NewsTarget | str) -> str | None:
    anchor = strong_identity(article, target)
    return _hash(RULE_VERSION, *anchor) if anchor else None


_NUMERAL = r"[+-]?\d+(?:,\d{3})*(?:\.\d+)?"
_UNIT = r"亿美元|万美元|亿港元|万港元|亿元|万元|美元|港元|人民币元|GWh|MWh|GW|MW|%|个百分点|元|吨|片|台|股|倍"
_NUMBER = re.compile(rf"({_NUMERAL})\s*({_UNIT})", re.I)
_RANGE = re.compile(rf"({_NUMERAL})\s*({_UNIT})?\s*(?:至|到|~|～|—|－|-)\s*({_NUMERAL})\s*({_UNIT})", re.I)
_METRIC = re.compile(r"(?P<revenue>营业总收入|营业收入|营收)|(?P<net_profit>扣非归母净利润|归母净利润|归属于(?:上市公司)?股东的净利润|扣非净利润|净利润|净亏损)|(?P<gross_margin>毛利率)|(?P<operating_cash_flow>经营活动产生的现金流量净额|经营活动现金流|经营现金流)|(?P<capacity>产能)|(?P<ownership>持股比例|股权比例)")
_DESTINATION = re.compile(r"应为|实际为|更正为|修正为|调整为|变更为|下调为|上调为|改为|现为")
_NEGATED = re.compile(r"(?:未|不|没有|否认|并未|尚未|可能|或将|拟).{0,2}$")
_CORRECTION = re.compile(r"更正|修正|上调指引|下调指引")


def _corrected(text: str) -> bool:
    return any(not _NEGATED.search(text[max(0, match.start()-5):match.start()]) and not text[match.end():].startswith("前")
               for match in _CORRECTION.finditer(text))


def _number(match: re.Match[str]) -> tuple[str, str]:
    number = Decimal(match[1].replace(",", ""))
    unit = match[2].lower()
    scales = {"亿元": ("CNY", 10**8), "万元": ("CNY", 10**4), "人民币元": ("CNY", 1), "元": ("CNY", 1),
              "亿美元": ("USD", 10**8), "万美元": ("USD", 10**4), "美元": ("USD", 1),
              "亿港元": ("HKD", 10**8), "万港元": ("HKD", 10**4), "港元": ("HKD", 1),
              "gw": ("MW", 1000), "mw": ("MW", 1), "gwh": ("MWh", 1000), "mwh": ("MWh", 1)}
    if unit in scales:
        unit, scale = scales[unit]
        number *= scale
    return format(number.normalize(), "f"), unit


def _reading(text: str, number: re.Match[str]) -> tuple[str, str] | None:
    """An explicit interval is a range, never an invented point estimate."""
    interval = next((match for match in _RANGE.finditer(text) if match.start() <= number.start() < match.end()), None)
    if interval is None:
        return _number(number)
    lower = _NUMBER.fullmatch(interval[1] + (interval[2] or interval[4]))
    upper = _NUMBER.fullmatch(interval[3] + interval[4])
    low, low_unit = _number(lower)
    high, high_unit = _number(upper)
    if low_unit != high_unit:
        return None
    bounds = sorted((Decimal(low), Decimal(high)))
    return "..".join(format(value.normalize(), "f") for value in bounds), low_unit


def _negative(value: str) -> str:
    return "..".join(format(item.normalize(), "f") for item in sorted(-abs(Decimal(part)) for part in value.split("..")))


def extract_facts(article: NewsArticle) -> tuple[dict[str, Any], ...]:
    """Return only complete facts with positions in source snippets.

    The latest explicit correction wins within one observation. Numeric values
    with unlike periods, units or bases have different keys and never average.
    """
    title, summary = _clean_text(article.title), _clean_text(article.summary)
    full = title + "。" + summary
    default_period = reporting_period(title) or reporting_period(summary)
    event_type = next((name for name, pattern in _TYPES if re.search(pattern, title)), "unknown")
    facts: dict[str, dict[str, Any]] = {}
    priorities: dict[str, int] = {}
    for statement in re.split(r"[。；;!?！？]", full):
        if not statement:
            continue
        correction = _corrected(statement)
        if re.search(r"更正前|(?:尚未|拟|可能)更正", statement) and not correction:
            continue
        if re.search(r"(?:分析师|研报|观点|我们认为|有望|料将)", statement):
            nature = "opinion"
        elif re.search(r"传闻|据传|网传|未经证实", statement):
            nature = "rumor"
        elif re.search(r"预计|预测|指引", statement):
            nature = "forecast"
        elif re.search(r"拟|计划", statement) and not re.search(r"(?:计划|方案).*(?:实施|完成|取消|撤销|终止)", statement):
            nature = "planned"
        else:
            nature = "confirmed" if re.search(r"公告|正式披露|官方确认|公司确认", title + "。" + statement) else "reported"
        period = reporting_period(statement) or default_period

        def add(metric: str, value: str, unit: str, basis: str = "", status: str = nature, relation: str = "absolute") -> None:
            if status == "forecast":
                relation = "forecast_" + relation
            key = ":".join((metric, period, basis, unit, relation))
            priority = 2 if correction else 1
            if priorities.get(key, 0) > priority:
                return
            facts[key] = dict(key=key, metric=metric, period=period, basis=basis, unit=unit,
                              relation=relation, value=value, status=status, quote=statement[:1000], correction=correction)
            priorities[key] = priority

        metrics = list(_METRIC.finditer(statement))
        for index, metric in enumerate(metrics):
            full_tail = statement[metric.end(): metrics[index+1].start() if index+1 < len(metrics) else len(statement)]
            tail = full_tail
            # Do not attach a percentage from the next independent clause.
            tail = re.split(r"[,，](?!\d{3})", tail, maxsplit=1)[0]
            destinations = list(_DESTINATION.finditer(tail))
            if correction and destinations:
                tail = tail[destinations[-1].end():]
            number = _NUMBER.search(tail[:70])
            if not number:
                continue
            reading = _reading(tail[:70], number)
            if reading is None:
                continue
            value, unit = reading
            prefix = tail[:number.start()]
            relation = "yoy" if "同比" in prefix else "qoq" if "环比" in prefix else "absolute"
            if unit in {"%", "个百分点"} and re.search(r"下滑|下降|减少|降低|下跌", prefix):
                value = _negative(value)
            if metric.lastgroup == "net_profit" and ("亏损" in prefix or "亏损" in metric[0]):
                value = _negative(value)
            attributable = "归母" in metric[0] or "股东" in metric[0]
            basis = ("nonrecurring_excluded_attributable" if "扣非" in metric[0] and attributable else
                     "nonrecurring_excluded" if "扣非" in metric[0] else "attributable" if attributable else "")
            add(metric.lastgroup, value, unit, basis, relation=relation)
            for comparison in re.finditer(r"(同比|环比)([^,，。；;]{0,15})", full_tail):
                change_number = _NUMBER.search(comparison[2])
                if not change_number:
                    continue
                delta_reading = _reading(comparison[2], change_number)
                if delta_reading is None:
                    continue
                delta, delta_unit = delta_reading
                if delta_unit not in {"%", "个百分点"}:
                    continue
                if re.search(r"下滑|下降|减少|降低|下跌", comparison[2][:change_number.start()]):
                    delta = _negative(delta)
                add(metric.lastgroup, delta, delta_unit, basis, relation="yoy" if comparison[1] == "同比" else "qoq")
        if event_type in {"order", "buyback", "acquisition"} and not metrics:
            amount_text = re.split(r"[,，](?=(?:较|比)?(?:原|此前|先前|去年|上年|更正前))", statement, maxsplit=1)[0] if correction else statement
            money = [match for match in _NUMBER.finditer(amount_text) if _number(match)[1] in {"CNY", "USD", "HKD"}]
            if money and re.search(r"订单|合同|回购|收购|金额", statement):
                if correction:
                    destinations = list(_DESTINATION.finditer(statement))
                    after = [match for match in money if not destinations or match.start() >= destinations[-1].end()]
                    chosen = after[0] if destinations and after else money[-1]
                else:
                    chosen = money[0]
                reading = _reading(amount_text, chosen)
                if reading is None:
                    continue
                value, unit = reading
                add(event_type + "_amount", value, unit)
        for state in re.finditer(r"撤销|取消|终止|作废|撤回|由计划转为实施|正式实施|已实施|开始实施|已完成", statement):
            # A forecast/opinion about completion is not a completed event.
            # Lifecycle values cannot discard the epistemic status above.
            if nature in {"opinion", "rumor", "forecast"}:
                continue
            if _NEGATED.search(statement[max(0, state.start()-5):state.start()]):
                continue
            prefix = statement[max(0, state.start()-16):state.start()]
            if re.search(r"(?:预计|预测|有望|可能|或将|预期|将|拟)[^。；;]{0,12}$", prefix):
                continue
            if prefix.endswith("计划") and not re.search(r"(?:回购|收购|项目|建设)计划$", prefix):
                continue
            if re.search(r"订单|合同|回购|收购|并购|项目|产线|指引", statement):
                status = "cancelled" if state[0] in {"撤销", "取消", "终止", "作废", "撤回"} else "completed" if state[0] == "已完成" else "implemented"
                add("lifecycle", status, "state", status=status)
    return tuple(facts[key] for key in sorted(facts))


def factual_revision_tokens(article: NewsArticle) -> tuple[str, ...]:
    """Stable complete metrics for article revision detection (not identity)."""
    tokens = []
    for fact in extract_facts(article):
        if fact["status"] in {"opinion", "rumor"}:
            continue
        tokens.append(json.dumps({key: fact[key] for key in ("key", "value", "status")}, sort_keys=True, ensure_ascii=False))
    return tuple(tokens)


@dataclass(frozen=True, slots=True)
class EventObservation:
    event_id: str
    change_id: str
    facts: tuple[dict[str, Any], ...]
    changes: tuple[dict[str, Any], ...]
    first_public_at: datetime | None
    notify_eligible: bool
    strong_identity: bool
    observation_kind: str = "substantive"

    def as_link(self) -> dict[str, Any]:
        return dict(event_id=self.event_id, event_version=self.change_id, change_id=self.change_id,
                    event_facts=list(self.facts), fact_changes=list(self.changes),
                    first_public_at=_iso(self.first_public_at) if self.first_public_at else None,
                    notify_eligible=self.notify_eligible, strong_identity=self.strong_identity,
                    observation_kind=self.observation_kind,
                    fact_ids=[fact["fact_id"] for fact in self.facts if not fact.get("disputed") and not fact.get("stale") and fact.get("kind") not in {"opinion", "rumor", "conflict", "additional_evidence"}])


def register_event(conn, article: NewsArticle, target: NewsTarget | str, now: datetime) -> EventObservation:
    """Append evidence/fact versions inside the caller's short transaction.

    Global duplicate facts are returned too: a member who has never received
    them must still be evaluated independently. No commit or remote I/O occurs.
    """
    from .quality import canonical_identity, event_identity, revision_key
    public = _iso(article.source_published_at or article.published_at) if article.time_status == "trusted" else None
    anchored = strong_identity(article, target) if public else None
    canonical = article.key if article.first_seen_at else canonical_identity(article)
    stable_subject = (target.kind + ":" + target.key.upper()) if isinstance(target, NewsTarget) else str(target)
    binding = conn.execute("SELECT event_id FROM business_event_article_bindings WHERE canonical_id=? AND subject=?", (canonical, stable_subject)).fetchone()
    event_id = (_hash(RULE_VERSION, *anchored) if anchored else
                binding[0] if binding else
                _hash(RULE_VERSION, "pending", stable_subject, canonical) if not public else
                _hash(RULE_VERSION, "weak", stable_subject, event_identity(article, target)[0]))
    subject, event_type, anchor = anchored or (stable_subject, "unknown", "article-fallback")
    bound_event = conn.execute("SELECT anchor,strong_identity FROM business_events WHERE event_id=?", (event_id,)).fetchone() if binding and not anchored and public else None
    proven_identity = bool(anchored or (bound_event and bound_event["strong_identity"]))
    bound_period = bound_event["anchor"][len("report:"):] if bound_event and bound_event["anchor"].startswith("report:") else ""
    stamp = _iso(now)
    conn.execute("""INSERT INTO business_events VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(event_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,
        first_public_at=CASE WHEN business_events.first_public_at IS NULL OR
          (excluded.first_public_at IS NOT NULL AND excluded.first_public_at<business_events.first_public_at)
          THEN excluded.first_public_at ELSE business_events.first_public_at END""",
        (event_id, subject, event_type, anchor, int(bool(anchored)), public, stamp, stamp))
    if public:
        conn.execute("INSERT INTO business_event_article_bindings VALUES(?,?,?) ON CONFLICT(canonical_id,subject) DO UPDATE SET event_id=excluded.event_id", (canonical, stable_subject, event_id))
    revision = revision_key(article)
    evidence_kind = "independent_evidence" if re.search(r"独立核实|独立调查|独立核验", article.summary) else "additional_source"
    conn.execute("INSERT OR IGNORE INTO business_event_evidence VALUES(?,?,?,?,?,?,?,?)",
                 (event_id, canonical, revision, article.source, article.url, public, stamp, evidence_kind))
    facts: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    for raw in extract_facts(article) if public else ():
        if bound_period and not raw["period"]:
            # A shortened observation of this same canonical report inherits
            # its already proven reporting scope, not a fresh unscoped metric.
            raw = dict(raw, period=bound_period,
                       key=":".join((raw["metric"], bound_period, raw["basis"], raw["unit"], raw["relation"])))
        key = raw["key"]
        previous = conn.execute("""SELECT f.fact_json,f.first_public_at FROM business_event_current c JOIN business_event_facts f
            ON f.event_id=c.event_id AND f.fact_id=c.fact_id WHERE c.event_id=? AND c.fact_key=?""", (event_id, key)).fetchone()
        before = json.loads(previous[0]) if previous else None
        if before is not None and previous["first_public_at"]:
            before = dict(before, first_public_at=previous["first_public_at"])
        if before is None and raw["status"] == "confirmed":
            uncertain = conn.execute("SELECT fact_json FROM business_event_facts WHERE event_id=? AND fact_key=? ORDER BY rowid DESC", (event_id, key)).fetchall()
            before = next((value for row in uncertain if (value := json.loads(row[0]))["status"] == "rumor" and value["value"] == raw["value"]), None)
        content = {name: raw[name] for name in ("key", "metric", "period", "basis", "unit", "relation", "value", "status")}
        fact_id = _hash(RULE_VERSION, event_id, json.dumps(content, sort_keys=True, ensure_ascii=False))
        if raw["correction"]:
            fact_id = _hash(fact_id, "explicit-correction")
        if before and before["value"] == raw["value"] and before["status"] == raw["status"]:
            fact_id = before["fact_id"]
        known = conn.execute("SELECT fact_json,first_public_at FROM business_event_facts WHERE event_id=? AND fact_id=?", (event_id, fact_id)).fetchone()
        if known and json.loads(known[0]).get("kind") != "historical" and before and before["fact_id"] != fact_id and raw["correction"] and re.search(r"再次更正|再度更正|恢复|重新调整|再次调整", article.title + article.summary) and before["first_public_at"] and parse_published_at(public) > parse_published_at(before["first_public_at"]):
            fact_id = _hash(fact_id, "reassertion", before["fact_id"])
            known = conn.execute("SELECT fact_json,first_public_at FROM business_event_facts WHERE event_id=? AND fact_id=?", (event_id, fact_id)).fetchone()
        if known:
            fact = json.loads(known[0])
            earliest = min(value for value in (known["first_public_at"], public) if value)
            conn.execute("UPDATE business_event_facts SET first_public_at=? WHERE event_id=? AND fact_id=? AND (first_public_at IS NULL OR first_public_at>?)", (earliest, event_id, fact_id, earliest))
            fact = dict(fact, first_public_at=earliest)
            if before and before["fact_id"] != fact_id and fact["kind"] not in {"conflict", "opinion", "rumor", "additional_evidence"}:
                fact = dict(fact, stale=True)
            change = "duplicate"
        else:
            same_value = before and before["value"] == raw["value"]
            historical = bool(previous and before["first_public_at"] and
                              parse_published_at(public) < parse_published_at(before["first_public_at"]))
            if historical:
                change = "historical"
            elif raw["status"] in {"opinion", "rumor"}:
                change = raw["status"]
            elif before and not same_value and not raw["correction"] and raw["metric"] != "lifecycle":
                change = "conflict"
            elif before and raw["correction"]:
                change = "correction"
            elif raw["metric"] == "lifecycle" and before:
                change = "progress"
            elif same_value and before["status"] in {"reported", "rumor", "forecast", "planned"} and raw["status"] == "confirmed":
                change = "confirmation"
            elif before and same_value:
                change = "additional_evidence"
            else:
                change = "new_fact"
            fact = dict(content, fact_id=fact_id, first_public_at=public, kind=change,
                        supersedes=before["fact_id"] if before and change in {"correction", "progress", "confirmation"} else None,
                        disputed=change == "conflict", stale=historical)
            conn.execute("INSERT INTO business_event_facts VALUES(?,?,?,?,?,?)", (event_id, fact_id, key, json.dumps(fact, ensure_ascii=False), public, stamp))
            if public and change not in {"conflict", "opinion", "rumor", "additional_evidence", "historical"}:
                conn.execute("INSERT INTO business_event_current VALUES(?,?,?) ON CONFLICT(event_id,fact_key) DO UPDATE SET fact_id=excluded.fact_id", (event_id, key, fact_id))
        conn.execute("INSERT OR IGNORE INTO business_event_fact_evidence VALUES(?,?,?,?,?,?)", (event_id, fact_id, canonical, revision, article.source, raw["quote"]))
        facts.append(fact)
        changes.append(dict(kind=change, fact_id=fact_id, before=before, after=fact))
    # Unsupported exact-fact stories keep their existing article version path.
    if not facts:
        change_id = event_identity(article, target)[1]
    else:
        change_id = _hash(RULE_VERSION, event_id, *sorted(fact["fact_id"] for fact in facts))
    eligible_facts = [fact for fact in facts if not fact.get("stale") and fact["kind"] not in {"conflict", "opinion", "rumor", "additional_evidence"}]
    times = [parse_published_at(fact["first_public_at"]) for fact in eligible_facts if fact["first_public_at"]]
    nonfactual = bool(re.search(r"观点|研报|分析师|传闻|据传|网传|未经证实", article.title + article.summary))
    text = article.title + "。" + article.summary
    if re.search(r"实施|完成|取消|撤销|终止", text) and re.search(r"否认|未|不|没有|可能|预计|预测|有望|或将|预期|拟|将", text):
        nonfactual = True
    if re.search(r"(?:公司|管理层|董事会|企业)计划[^。；;]{0,20}(?:实施|完成)", text):
        nonfactual = True
    first_public = max(times) if times else parse_published_at(public) if public and not facts and not nonfactual else None
    eligible = bool(public and (eligible_facts or (not facts and not nonfactual)))
    conn.execute("INSERT OR IGNORE INTO business_event_versions VALUES(?,?,?,?,?,?)", (event_id, change_id, json.dumps(facts, ensure_ascii=False), json.dumps(changes, ensure_ascii=False), _iso(first_public) if first_public else None, stamp))
    version = conn.execute("SELECT changes_json FROM business_event_versions WHERE event_id=? AND change_id=?", (event_id, change_id)).fetchone()
    observation_kind = "duplicate" if changes and all(change["kind"] == "duplicate" for change in changes) else "substantive"
    return EventObservation(event_id, change_id, tuple(facts), tuple(json.loads(version[0])), first_public, eligible, proven_identity, observation_kind)
