"""Read-only, time-aware and bounded research excerpts for article analysis.

Excerpts are evidence, not executable instructions or a market consensus. The
fingerprint deliberately excludes the moving decision clock, so an unchanged
snapshot can share an existing analysis specification.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from .research import ResearchStore

CONTEXT_VERSION = "1"
MAX_CONTEXT_BYTES = 16000
_GROUPS = (
    r"存储|DRAM|DDR|NAND|NOR|HBM|memory",
    r"价格|报价|涨价|降价|售价|ASP|pricing|price",
    r"毛利|成本|代工|折旧|margin|cost|depreciation",
    r"订单|需求|客户|销量|出货|供需|库存|demand|order|inventory",
    r"产能|利用率|良率|扩产|晶圆|capacity|utilization|yield|wafer",
    r"利润|盈利|业绩|财报|营收|指引|earnings|profit|revenue|guidance",
    r"现金|应收|资本开支|capex|cash|receivable",
    r"回购|注销|减持|增持|分红|股本|buyback|dividend|dilution",
    r"出口|监管|政策|禁令|许可|诉讼|regulat|export|sanction",
    r"云|AI|算力|电商|补贴|cloud|commerce",
)
_PATTERNS = tuple(re.compile(p, re.I) for p in _GROUPS)


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("aware research decision time required")
    return value.astimezone(timezone.utc)


def _stamp(value):
    return _time(value).isoformat(timespec="seconds")


def _text(value, limit=260):
    return " ".join(str(value).split())[:limit]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _finish(context):
    # Trim whole optional entries rather than severing a selected statement's
    # evidence references. Sources are rebuilt after trimming.
    material = context.get("material", {})
    sources = context.get("sources", ())
    def align_sources():
        used = {ref for items in material.values() for item in items for ref in item.get("evidence_ids", ())}
        context["sources"] = [s for s in sources if s["id"] in used]
    align_sources()
    while len(_json(context).encode()) > MAX_CONTEXT_BYTES - 100:
        removed = False
        for name, minimum in (("metrics", 0), ("statements", 1), ("mappings", 1), ("hypotheses", 1)):
            if len(material.get(name, ())) > minimum:
                material[name].pop()
                removed = True
                context["truncated"] = True
                align_sources()
                break
        if not removed:
            # All source identities/times survive. Long URLs can be resolved
            # through the immutable baseline reference rather than repeated.
            for source in context["sources"]:
                source.pop("url", None)
            context["source_links_omitted_for_size"] = True
            context["truncated"] = True
            break
    if len(_json(context).encode()) > MAX_CONTEXT_BYTES - 100:
        context["material"] = dict(statements=[], hypotheses=[], mappings=[], metrics=[])
        context["sources"] = []
        context["comparison_available"] = False
        context["missing_reason"] = "research_excerpt_size_limit"
    stable = {k: v for k, v in context.items() if k not in {"cutoff_at", "decision_at"}}
    context["fingerprint"] = hashlib.sha256(_json(stable).encode()).hexdigest()
    return context


def select_context(conn, symbol, article, decision_at):
    """Freeze public research for one security without writing or networking.

    A pre-publication baseline is preferred. A later baseline known by the
    decision time may provide current background only; it is explicitly barred
    from acting as the original expectation for this news event.
    """
    decision = _time(decision_at)
    public = None
    if getattr(article, "time_status", "trusted") == "trusted":
        public = _time(article.source_published_at or article.published_at)
    cutoff = min(public, decision) if public is not None else None
    context = dict(context_version=CONTEXT_VERSION, symbol=_text(symbol or "", 64).upper(),
                   status="missing", comparison_available=False, baseline_ref=None,
                   cutoff_at=_stamp(cutoff) if cutoff else None,
                   decision_at=_stamp(decision), missing_reason="research_not_available",
                   material=dict(statements=[], hypotheses=[], mappings=[], metrics=[]), sources=[])
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not symbol or not {"research_baselines", "research_securities"} <= tables:
        return _finish(context)
    mapping = conn.execute("SELECT company_id FROM research_securities WHERE symbol=?", (context["symbol"],)).fetchone()
    if mapping is None:
        return _finish(context)
    store = ResearchStore(conn)
    prior = store.get_baseline(mapping[0], _stamp(cutoff)) if cutoff else None
    record = prior or store.get_baseline(mapping[0], _stamp(decision))
    if record is None:
        return _finish(context)
    doc = record["document"]
    # The job tracks the latest baseline, not necessarily the pre-event version
    # selected here. Derive readiness from that exact immutable record instead.
    baseline_status = "usable" if record["readiness"].get("fundamental") else "partial"
    needs_review = False
    if "research_review_requests" in tables:
        needs_review = conn.execute("""SELECT 1 FROM research_review_requests WHERE company_id=?
            AND julianday(created_at)>julianday(?) AND julianday(created_at)<=julianday(?) LIMIT 1""",
            (mapping[0], record["created_at"], _stamp(decision))).fetchone() is not None
    comparable = bool(prior and record["readiness"].get("fundamental"))
    context.update(status=(("needs_update" if needs_review else baseline_status) if prior else "background_only"),
                   baseline_status=baseline_status,
                   review_required=needs_review,
                   comparison_available=comparable,
                   baseline_ref={k: (_text(record[k], 128) if k == "company_id" else record[k]) for k in ("id", "company_id", "version", "as_of", "created_at", "content_hash")},
                   readiness={k: record["readiness"].get(k, False) for k in ("fundamental", "trackable", "expectation_comparison", "validated_prediction")},
                   missing_reason="" if comparable else "partial_research" if prior else "no_pre_event_baseline")
    text = article.title + " " + article.summary
    groups = {i for i, p in enumerate(_PATTERNS) if p.search(text)}
    words = set(re.findall(r"[a-z][a-z0-9_-]{2,}", text.casefold()))

    def score(item):
        value = _json(item)
        return sum(10 for i in groups if _PATTERNS[i].search(value)) + sum(1 for w in words if w in value.casefold())

    def choose(items, maximum, fallback=1):
        ranked = sorted(enumerate(items), key=lambda pair: (-score(pair[1]), pair[0]))
        positive = [item for _, item in ranked if score(item)]
        return (positive or [item for _, item in ranked[:fallback]])[:maximum]

    def evidence(values):
        bounded = list(dict.fromkeys(v for v in values if isinstance(v, str) and len(v) <= 200))[:8]
        if any(len(str(v)) > 200 for v in values):
            context["comparison_available"] = False
            context["missing_reason"] = "unsupported_research_source_identifier"
        return bounded

    material = context["material"]
    for item in choose(doc.get("assumptions", ()), 4, 2):
        material["hypotheses"].append(dict(hypothesis=_text(item["hypothesis"]),
            counter_evidence=_text(item["counter_evidence"]), review_condition=_text(item["review_condition"]),
            evidence_ids=evidence(item["support_evidence_ids"])))
    for item in choose(doc.get("monitor_mappings", ()), 4, 1):
        material["mappings"].append({k: _text(item[k], 170) for k in ("event", "business", "metric", "transmission", "verify")} |
                                    dict(evidence_ids=evidence(item["evidence_ids"])))
    # The business model and the relevant risk remain visible even when price
    # headlines do not repeat the exact terms used by the research author.
    for name in ("business_model", "earnings_drivers", "industry_cycle", "risks"):
        section = doc.get("sections", {}).get(name, {})
        for item in choose(section.get("statements", ()), 1):
            material["statements"].append(dict(section=name, kind=item["kind"], text=_text(item["text"], 220), evidence_ids=evidence(item["evidence_ids"])))
    metrics = [dict(item, section=name) for name, section in doc.get("sections", {}).items() for item in section.get("metrics", ())]
    for item in choose(metrics, 6, 2):
        material["metrics"].append({k: _text(item[k], 100) for k in ("name", "value", "period", "unit", "currency", "basis", "section")} |
                                   dict(evidence_ids=evidence(item["evidence_ids"])))
    refs = {ref for items in material.values() for item in items for ref in item["evidence_ids"]}
    context["sources"] = [{k: (_text(source[k], 1000 if k == "url" else 200 if k == "id" else 180) if source[k] is not None else None)
                            for k in ("id", "title", "url", "published_at", "available_at", "ingested_at", "locator")
                            if k != "url" or len(str(source[k])) <= 1000}
                           for source in doc.get("sources", ()) if source["id"] in refs]
    return _finish(context)
