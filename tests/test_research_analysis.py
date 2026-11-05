"""Offline research evidence, point-in-time selection and model contracts."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from stock_watch.analyzer import (AnalysisValidationError, DeepSeekAnalyzer,
    build_analysis_prompt, build_batch_prompt, validate_analysis)
from stock_watch.delivery import render_payload
from stock_watch.news import NewsArticle, NewsTarget
from stock_watch.research import ResearchStore, SCHEMA_SQL, SECTIONS
from stock_watch.research_context import MAX_CONTEXT_BYTES, select_context

UTC = timezone.utc
BASE = datetime(2026, 11, 1, 8, tzinfo=UTC)
EVENT = BASE + timedelta(days=1)
DECISION = EVENT + timedelta(hours=1)
TARGET = NewsTarget('synthetic-research', '合成研究公司', '合成研究公司', symbol='123456.SH')
ARTICLE = NewsArticle(key='research-news', title='DRAM现货报价下调，需核对成本与需求',
    summary='合成报价材料显示价格下调，供应及实际成本仍需核实。',
    url='https://example.invalid/research-news', published_at=EVENT, target_keys=(TARGET.key,))
VALID = dict(level='重要', impact='不确定', reason='现货变化需要核对合约价格与成本。',
             followup='核对下一期合约。', matched_targets=[TARGET.key],
             research_effect='weakens', analysis_kind='industry',
             baseline_comparison='价格变化削弱原供需假设，尚未证实公司实际售价变化。',
             counter_evidence='同规格合约价格和成本尚未披露。', is_fundamental=False,
             should_notify=True, new_facts=['合成现货报价下调。'])


def document(as_of=BASE, *, partial=False):
    stamp = as_of.isoformat()
    sections = {name: dict(statements=[dict(kind='fact', text='合成研究观察：供需及成本有待持续复核。', evidence_ids=['s'])]) for name in SECTIONS}
    sections['business_model']['statements'][0]['text'] = '公司经营合成存储产品。'
    sections['financial_quality']['metrics'] = [dict(name='合成营业收入', value='100', period='2026Q3', unit='百万元', currency='CNY', basis='合并', evidence_ids=['s'])]
    return dict(schema_version=1, company=dict(company_id='synthetic', name='合成研究公司'),
        securities=[dict(symbol='123456.SH', market='SH', currency='CNY')], as_of=stamp,
        sections=sections,
        sources=[dict(id='s', url='https://example.invalid/report', title='合成研究资料', published_at=stamp, available_at=stamp, ingested_at=stamp, locator='表1')],
        assumptions=[dict(hypothesis='回购注销改善每股权益。', counter_evidence='奖励发行可能稀释。', review_condition='核对注销股份。', support_evidence_ids=['s']),
                     dict(hypothesis='DRAM供需支持合约价格及毛利。', counter_evidence='成本追涨或需求下降削弱价格传导。', review_condition='核对实际售价、成本及订单。', support_evidence_ids=['s'])],
        monitor_mappings=[dict(event='回购', business='资本配置', metric='净股数', transmission='回购至注销', verify='下一次股本披露', evidence_ids=['s']),
                          dict(event='DRAM价格', business='存储', metric='毛利及ASP', transmission='售价与成本共同影响利润。', verify='同规格合约和成本。', evidence_ids=['s'])],
        financial_review=dict(period='2026Q3', source_id='s', latest_available_checked=not partial))


@pytest.fixture
def store():
    conn = sqlite3.connect(':memory:')
    conn.executescript(SCHEMA_SQL)
    yield ResearchStore(conn)
    conn.close()


def context(store, *, created=BASE, as_of=BASE, article=ARTICLE, decision=DECISION, partial=False):
    store.append_baseline(document(as_of, partial=partial), created.isoformat())
    return select_context(store.conn, TARGET.symbol, article, decision)


def tagged_json(prompt, tag):
    return json.loads(prompt.split('<' + tag + '>\n', 1)[1].split('\n</' + tag + '>', 1)[0])


def test_relevant_hypothesis_counter_and_sources_are_selected_read_only(store):
    store.append_baseline(document(), BASE.isoformat())
    before = store.conn.total_changes
    result = select_context(store.conn, TARGET.symbol, ARTICLE, DECISION)
    assert result['comparison_available'] and result['status'] == 'usable'
    assert result['material']['hypotheses'][0]['hypothesis'].startswith('DRAM')
    assert '需求下降' in result['material']['hypotheses'][0]['counter_evidence']
    assert result['material']['mappings'][0]['business'] == '存储'
    assert result['baseline_ref']['version'] == 1
    assert result['sources'][0]['id'] == 's'
    assert store.conn.total_changes == before
    assert len(json.dumps(result, ensure_ascii=False).encode()) < MAX_CONTEXT_BYTES


def test_later_baseline_is_not_recast_as_the_original_expectation(store):
    late = EVENT + timedelta(minutes=10)
    result = context(store, created=late, as_of=BASE)
    assert result['status'] == 'background_only'
    assert not result['comparison_available']
    assert result['missing_reason'] == 'no_pre_event_baseline'
    assert select_context(store.conn, TARGET.symbol, ARTICLE, EVENT)['status'] == 'missing'


def test_prior_available_version_wins_over_newer_post_event_research(store):
    first = store.append_baseline(document(), BASE.isoformat())
    newer = document(EVENT + timedelta(minutes=10))
    newer['assumptions'][1]['hypothesis'] = 'Post-event revision must not replace original view'
    store.append_baseline(newer, (EVENT + timedelta(minutes=20)).isoformat())
    result = select_context(store.conn, TARGET.symbol, ARTICLE, DECISION)
    assert result['baseline_ref']['id'] == first['id']
    assert 'Post-event' not in json.dumps(result)


def test_future_job_and_review_state_do_not_upgrade_or_taint_prior_snapshot(store):
    store.append_baseline(document(partial=True), BASE.isoformat())
    later = EVENT + timedelta(minutes=10)
    store.append_baseline(document(later), later.isoformat())
    result = select_context(store.conn, TARGET.symbol, ARTICLE, DECISION)
    assert result['baseline_ref']['version'] == 1
    assert result['status'] == 'partial' and not result['comparison_available']
    store.mark_for_review('synthetic', 'Future review must remain invisible', (DECISION + timedelta(days=1)).isoformat())
    assert select_context(store.conn, TARGET.symbol, ARTICLE, DECISION)['fingerprint'] == result['fingerprint']


def test_missing_partial_and_unknown_source_date_degrade_explicitly(store):
    assert select_context(store.conn, '654321.SH', ARTICLE, DECISION)['status'] == 'missing'
    result = context(store, partial=True)
    assert result['status'] == 'partial' and not result['comparison_available']
    unknown = select_context(store.conn, TARGET.symbol, replace(ARTICLE, time_status='conflict'), DECISION)
    assert unknown['status'] == 'background_only'
    assert unknown['cutoff_at'] is None and not unknown['comparison_available']


def test_no_research_schema_does_not_create_tables():
    conn = sqlite3.connect(':memory:')
    assert select_context(conn, TARGET.symbol, ARTICLE, DECISION)['status'] == 'missing'
    assert not conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()


def test_fingerprint_ignores_moving_clock_and_records_baseline_changes(store):
    first = context(store)
    later = select_context(store.conn, TARGET.symbol, ARTICLE, DECISION + timedelta(minutes=5))
    assert first['fingerprint'] == later['fingerprint']
    assert first['decision_at'] != later['decision_at']
    store.mark_for_review('synthetic', '核对新一期', DECISION.isoformat())
    reviewed = select_context(store.conn, TARGET.symbol, ARTICLE, DECISION)
    assert reviewed['status'] == 'needs_update'
    assert reviewed['fingerprint'] != first['fingerprint']


def test_context_and_referenced_sources_are_bounded_without_splitting_facts(store):
    doc = document()
    doc['sources'][0]['url'] = 'https://example.invalid/' + 'long-path-' * 150
    for item in doc['assumptions']:
        item['hypothesis'] *= 100
        item['counter_evidence'] *= 100
    store.append_baseline(doc, BASE.isoformat())
    result = select_context(store.conn, TARGET.symbol, ARTICLE, DECISION)
    assert len(json.dumps(result, ensure_ascii=False).encode()) < MAX_CONTEXT_BYTES
    refs = {r for items in result['material'].values() for item in items for r in item['evidence_ids']}
    assert refs <= {s['id'] for s in result['sources']}


def test_single_prompt_contains_research_counter_and_evidence_limitations(store):
    result = context(store)
    article = replace(ARTICLE, research_context=result, evidence_status='full_text_truncated')
    prompt = build_analysis_prompt(article, (TARGET,))
    raw = tagged_json(prompt, 'UNTRUSTED_ARTICLE')
    assert raw['research_context']['fingerprint'] == result['fingerprint']
    assert '需求下降' in prompt and 'full_text_truncated' in prompt
    assert '不能冒充事件发生前的预期' in prompt
    assert '来源股票关联只表示值得查看' in prompt


def test_batch_deduplicates_identical_public_research_but_keeps_task_context(store):
    first = context(store)
    second = {**first, 'cutoff_at': (EVENT + timedelta(minutes=2)).isoformat()}
    a = replace(ARTICLE, research_context=first)
    b = replace(ARTICLE, key='other-report', research_context=second)
    prompt = build_batch_prompt([('a', a, (TARGET,)), ('b', b, (replace(TARGET, focus=('成本风险',)),))])
    shared = tagged_json(prompt, 'UNTRUSTED_RESEARCH_CONTEXTS')
    tasks = tagged_json(prompt, 'UNTRUSTED_TASKS')
    assert len(shared) == 1
    assert tasks[0]['article']['research_context_ref'] == tasks[1]['article']['research_context_ref']
    assert tasks[0]['article']['research_cutoff_at'] != tasks[1]['article']['research_cutoff_at']
    assert tasks[0]['targets'][0]['focus'] != tasks[1]['targets'][0]['focus']


def test_batch_never_merges_different_baselines_with_same_supplied_fingerprint(store):
    first = context(store)
    second = {**first, 'baseline_ref': {**first['baseline_ref'], 'version': 99}}
    prompt = build_batch_prompt([('a', replace(ARTICLE, research_context=first), (TARGET,)),
                                ('b', replace(ARTICLE, research_context=second), (TARGET,))])
    assert len(tagged_json(prompt, 'UNTRUSTED_RESEARCH_CONTEXTS')) == 2


@pytest.mark.parametrize('bad', [dict(research_effect='guaranteed'), dict(analysis_kind='execute'),
    dict(baseline_comparison=['not-text']), dict(counter_evidence='x' * 501), dict(shell='do-not-execute')])
def test_new_analysis_fields_remain_strict(bad):
    with pytest.raises(AnalysisValidationError):
        validate_analysis({**VALID, **bad}, allowed_target_keys=(TARGET.key,))


def test_future_only_background_cannot_support_original_view(store):
    result = context(store, created=EVENT + timedelta(minutes=10))
    with pytest.raises(AnalysisValidationError, match='invalid_research_effect'):
        validate_analysis(VALID, allowed_target_keys=(TARGET.key,), research_context=result)
    valid = {**VALID, 'research_effect': 'insufficient'}
    assert validate_analysis(valid, allowed_target_keys=(TARGET.key,), research_context=result).should_notify
    assert isinstance(DeepSeekAnalyzer(lambda **_: dict(results=[dict(task_id='x', analysis=VALID)])).analyze_batch(
        [('x', replace(ARTICLE, research_context=result), (TARGET,))])['x'], AnalysisValidationError)


def test_legacy_analysis_remains_valid_and_irrelevant_cannot_notify():
    legacy = {k: VALID[k] for k in ('level', 'impact', 'reason', 'followup', 'matched_targets')}
    assert validate_analysis(legacy, allowed_target_keys=(TARGET.key,)).research_effect == 'insufficient'
    with pytest.raises(AnalysisValidationError, match='invalid_should_notify'):
        validate_analysis({**VALID, 'analysis_kind': 'irrelevant'}, allowed_target_keys=(TARGET.key,))


def test_new_request_with_baseline_requires_research_fields(store):
    selected = context(store)
    legacy = {k: VALID[k] for k in ('level', 'impact', 'reason', 'followup', 'matched_targets')}
    with pytest.raises(AnalysisValidationError, match='analysis_schema_mismatch'):
        validate_analysis(legacy, allowed_target_keys=(TARGET.key,), research_context=selected)
    with pytest.raises(AnalysisValidationError, match='invalid_baseline_comparison'):
        validate_analysis({**VALID, 'baseline_comparison': ''}, allowed_target_keys=(TARGET.key,), research_context=selected)


def test_delivery_shows_frozen_comparison_and_counter_without_database(store):
    result = context(store)
    payload = dict(article=dict(title=ARTICLE.title, url=ARTICLE.url, published_at=EVENT.isoformat()),
                   target_analyses=[dict(name=TARGET.name, analysis=VALID, research_context=result)], config_version=1)
    text = render_payload(payload)
    assert '研究基线：v1' in text and '削弱原判断' in text and '反证与局限' in text
    payload['target_analyses'][0]['research_context'] = {**result, 'comparison_available': False, 'status': 'background_only'}
    text = render_payload(payload)
    assert '缺少事件前可用基线' in text and '削弱原判断' not in text
