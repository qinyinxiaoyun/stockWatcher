import copy
import sqlite3

import pytest

from stock_watch.research import ResearchStore, SCHEMA_SQL, SECTIONS

NOW = '2026-11-03T10:00:00+08:00'


def document():
    return {'schema_version': 1, 'company': {'company_id': 'example', 'name': 'Example'}, 'securities': [{'symbol': 'HK:00001', 'market': 'HK', 'currency': 'HKD'}, {'symbol': 'US:EX', 'market': 'US', 'currency': 'USD'}], 'as_of': NOW, 'sections': {name: {'statements': [{'kind': 'unknown', 'text': 'Not verified', 'evidence_ids': []}]} for name in SECTIONS}, 'sources': [], 'assumptions': [], 'monitor_mappings': []}


@pytest.fixture
def store():
    conn = sqlite3.connect(':memory:')
    conn.executescript(SCHEMA_SQL)
    return ResearchStore(conn)


def test_partial_idempotent_and_append_only(store):
    doc = document()
    first = store.append_baseline(doc, NOW)
    assert not any(first['readiness'][key] for key in ('trackable', 'fundamental', 'expectation_comparison', 'validated_prediction'))
    assert store.append_baseline(doc, NOW)['id'] == first['id']
    doc['sections']['risks']['statements'][0]['text'] = 'Updated unknown'
    second = store.append_baseline(doc, NOW)
    assert second['version'] == 2
    assert store.list_baselines()[0] == first
    assert len(store.company_securities('example')) == 2


def test_history_never_backdates_creation(store):
    doc = document()
    doc['as_of'] = '2026-11-01T10:00:00+08:00'
    store.append_baseline(doc, NOW)
    assert store.get_baseline('example', '2026-11-02T10:00:00+08:00') is None
    assert store.get_baseline('example', NOW)


@pytest.mark.parametrize('mutation', ['extra','prediction','future','fact'])
def test_rejects_untrusted_fields_and_unsupported_claims(store, mutation):
    doc = document()
    if mutation == 'extra':
        doc['shell'] = 'echo unsafe'
    elif mutation == 'prediction':
        doc['readiness'] = {'validated_prediction': True}
    elif mutation == 'future':
        doc['as_of'] = '2027-01-01T00:00:00Z'
    else:
        doc['sections']['business_model']['statements'][0]['kind'] = 'fact'
    with pytest.raises(ValueError):
        store.append_baseline(doc, NOW)
    assert not store.list_baselines()
    assert not store.company_securities('example')


def test_preserves_caller_transaction_and_atomic_failure(store):
    store.conn.execute('CREATE TABLE unrelated(value TEXT)')
    store.conn.execute("INSERT INTO unrelated VALUES ('keep')")
    store.append_baseline(document(), NOW)
    assert store.conn.in_transaction
    store.conn.rollback()
    assert not store.list_baselines()
    assert not store.conn.execute('SELECT * FROM unrelated').fetchall()


def test_source_cutoff_and_readiness_not_trusted(store):
    doc = document()
    doc['readiness'] = {'fundamental': True, 'trackable': True}
    assert not store.append_baseline(doc, NOW)['readiness']['fundamental']
    doc['sources'] = [{'id': 's', 'url': 'https://example.org/report', 'title': 'Report', 'published_at': NOW, 'available_at': '2026-11-04T10:00:00+08:00', 'ingested_at': '2026-11-04T10:00:00+08:00', 'locator': 'page 1'}]
    with pytest.raises(ValueError):
        store.append_baseline(doc, '2026-11-04T10:00:00+08:00')


def test_company_conflict_rolls_back(store):
    store.append_baseline(document(), NOW)
    changed = copy.deepcopy(document())
    changed['company']['company_id'] = 'other'
    with pytest.raises(ValueError):
        store.append_baseline(changed, NOW)
    assert store.conn.execute("SELECT 1 FROM research_companies WHERE company_id='other'").fetchone() is None


def test_later_import_of_old_snapshot_does_not_hide_latest(store):
    latest = store.append_baseline(document(), NOW)
    old = document()
    old['as_of'] = '2026-11-01T10:00:00+08:00'
    store.append_baseline(old, NOW)
    assert store.get_baseline('example')['id'] == latest['id']


@pytest.mark.parametrize('field,value', [('market','US'), ('currency','USD'), ('symbol','invalid symbol')])
def test_security_identity_is_stable(store, field, value):
    store.append_baseline(document(), NOW)
    doc = document()
    doc['securities'][0][field] = value
    with pytest.raises(ValueError):
        store.append_baseline(doc, NOW)
    assert store.company_securities('example')[0]['currency'] == 'HKD'


@pytest.mark.parametrize('value', [float('nan'), float('inf'), 'NaN', 'Infinity', '', 'not a number', 1.2, True])
def test_metrics_reject_nonexact_or_nonfinite_values(store, value):
    doc = sourced_document()
    doc['sections']['financial_quality']['metrics'][0]['value'] = value
    with pytest.raises(ValueError):
        store.append_baseline(doc, NOW)


def sourced_document():
    doc = document()
    doc['sources'] = [{'id': 's', 'url': 'https://example.org/report', 'title': 'Report', 'published_at': None, 'available_at': NOW, 'ingested_at': NOW, 'locator': 'page 1'}]
    for name in SECTIONS:
        doc['sections'][name]['statements'] = [{'kind': 'fact', 'text': 'Supported research observation', 'evidence_ids': ['s']}]
    doc['sections']['financial_quality']['metrics'] = [{'name': 'revenue', 'value': '123.45', 'period': '2026Q3', 'unit': 'million', 'currency': 'HKD', 'basis': 'IFRS consolidated', 'evidence_ids': ['s']}]
    doc['financial_review'] = {'period': '2026Q3', 'source_id': 's', 'latest_available_checked': True}
    doc['assumptions'] = [{'hypothesis': 'Demand persists', 'support_evidence_ids': ['s'], 'counter_evidence': 'Watch demand decline', 'review_condition': 'Next report'}]
    doc['monitor_mappings'] = [{'event': 'Report', 'business': 'Core', 'metric': 'revenue', 'transmission': 'Sales to cash', 'verify': 'Next report', 'evidence_ids': ['s']}]
    return doc


def test_unknown_publication_retained_and_financial_gate(store):
    doc = sourced_document()
    ready = store.append_baseline(doc, NOW)
    assert ready['document']['sources'][0]['published_at'] is None
    assert ready['readiness']['fundamental']
    assert not ready['readiness']['validated_prediction']
    doc['financial_review']['period'] = '2026Q2'
    partial = store.append_baseline(doc, NOW)
    assert not partial['readiness']['fundamental']
    assert 'latest_financial_period_not_reconciled' in partial['readiness']['reasons']


def test_jobs_created_automatically_and_preparation_is_idempotent(store):
    store.append_baseline(document(), NOW)
    original = store.list_jobs()
    store.ensure_preparation('example', '2026-11-04T10:00:00+08:00')
    assert store.list_jobs() == original
    assert original[0]['status'] == 'partial'


def test_metrics_container_and_unknown_section_stay_partial(store):
    doc = sourced_document()
    doc['sections']['risks']['statements'] = [{'kind': 'unknown', 'text': 'Not checked', 'evidence_ids': []}]
    assert not store.append_baseline(doc, NOW)['readiness']['fundamental']
    doc['sections']['risks']['metrics'] = {}
    with pytest.raises(ValueError):
        store.append_baseline(doc, NOW)


def test_unknown_valuation_and_expectations_do_not_block_fundamentals(store):
    doc = sourced_document()
    for name in ('valuation', 'scenarios_expectations'):
        doc['sections'][name]['statements'] = [{'kind': 'unknown', 'text': 'No qualified market snapshot', 'evidence_ids': []}]
    result = store.append_baseline(doc, NOW)
    assert result['readiness']['fundamental']
    assert not result['readiness']['expectation_comparison']
    assert not result['readiness']['validated_prediction']


@pytest.mark.parametrize('needs_review', [False, True])
def test_old_backfill_preserves_current_task_status(store, needs_review):
    current = sourced_document()
    first = store.append_baseline(current, NOW)
    if needs_review:
        store.mark_for_review('example', 'Await latest report', NOW)
    expected = 'needs_update' if needs_review else 'usable'
    historical = document()
    historical['as_of'] = '2026-01-01T00:00:00Z'
    store.append_baseline(historical, NOW)
    assert store.get_baseline('example')['id'] == first['id']
    assert store.list_jobs()[0]['status'] == expected
    assert len(store.list_baselines()) == 2
