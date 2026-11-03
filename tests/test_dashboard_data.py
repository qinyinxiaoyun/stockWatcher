from types import SimpleNamespace
import sqlite3

import pytest

from stock_watch.dashboard_data import Unavailable, load_dashboard, load_stock
from stock_watch.db import connect, init_db


def test_missing_database_does_not_create(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises(Unavailable):
        load_dashboard(SimpleNamespace(database_path=path, wecom_corp_id='org'))
    assert not path.exists()


def test_scope_and_read_only(tmp_path):
    path = tmp_path / 'test.db'
    conn = connect(path)
    init_db(conn)
    conn.execute("INSERT INTO app_meta(key,value) VALUES('independent_subscriptions','org')")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    settings = SimpleNamespace(database_path=path, wecom_corp_id='org')
    assert load_dashboard(settings)['counts']['members'] == 0
    assert load_stock(settings, 'UNKNOWN') is None
    assert path.read_bytes() == before
    with pytest.raises(Unavailable):
        load_dashboard(SimpleNamespace(database_path=path, wecom_corp_id='other'))

import json
from stock_watch.research import ResearchStore
from test_research import document, sourced_document, NOW


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / 'archive.db'
    conn = connect(path)
    init_db(conn)
    conn.execute("INSERT INTO app_meta(key,value) VALUES('independent_subscriptions','org')")
    snapshot = {'stocks': [{'symbol': 'HK:00001', 'name': '<script>x</script>', 'focus': '现金流', 'enabled': 1, 'paused': 0, 'direction': 'both', 'push_level': 'important'}], 'themes': [{'id': 't', 'name': '芯片', 'enabled': 1}], 'theme_keywords': [{'theme_id': 't', 'keyword': '设备'}], 'private_background': 'NEVER EXPOSE'}
    for uid, corp, active in [('alice', 'org', 1), ('disabled', 'org', 0), ('foreign-secret', 'other', 1)]:
        mid = conn.execute('INSERT INTO members(corp_id,user_id,role,active,created_at,updated_at) VALUES(?,?,?,?,?,?)', (corp, uid, 'subscriber', active, NOW, NOW)).lastrowid
        snapshot['stocks'][0]['paused'] = int(not active)
        conn.execute('INSERT INTO member_configs VALUES(?,?,?,?)', (mid, 7, json.dumps(snapshot), NOW))
    conn.commit()
    yield SimpleNamespace(database_path=path, wecom_corp_id='org'), conn
    conn.close()


def test_members_scope_disabled_and_whitelist(archive):
    settings, conn = archive
    data = load_dashboard(settings)
    assert [m['user_id'] for m in data['members']] == ['alice', 'disabled']
    assert data['members'][1]['active'] == 0
    assert data['members'][1]['stocks'][0]['paused'] == 1
    assert data['members'][0]['config_version'] == 7
    assert data['members'][0]['themes'][0]['keywords'] == ['设备']
    assert data['members'][0]['stocks'][0]['focus'] == '现金流'
    assert 'NEVER EXPOSE' not in json.dumps(data)
    assert 'foreign-secret' not in json.dumps(data)
    assert data['counts']['active_stocks'] == 1
    assert data['stocks'][0]['name'] == '<script>x</script>'


def test_no_baseline_and_exact_symbols(archive):
    settings, _ = archive
    assert load_stock(settings, 'HK:00001')['research']['status'] == 'not_started'
    assert load_stock(settings, "' OR 1=1 --") is None
    assert len(load_dashboard(settings, '现金流')['members']) == 2


@pytest.mark.parametrize('ready', [True, False])
def test_research_readiness_sources_and_period(archive, ready):
    settings, conn = archive
    doc = sourced_document() if ready else document()
    ResearchStore(conn).append_baseline(doc, NOW)
    conn.commit()
    research = load_stock(settings, 'HK:00001')['research']
    assert research['status'] == ('usable' if ready else 'partial')
    assert research['readiness']['fundamental'] is ready
    assert research['readiness']['validated_prediction'] is False
    assert research['financial_period'] == ('2026Q3' if ready else None)
    assert research['source_count'] == int(ready)
    if ready:
        assert research['baseline']['document']['sources'][0]['published_at'] is None
    else:
        assert research['readiness']['reasons']


def test_history_keeps_latest_cutoff_and_current_job(archive):
    settings, conn = archive
    store = ResearchStore(conn)
    store.append_baseline(sourced_document(), NOW)
    old = document()
    old['as_of'] = '2026-11-01T10:00:00+08:00'
    store.append_baseline(old, NOW)
    store.mark_for_review('example', 'New report', NOW)
    conn.commit()
    current = load_stock(settings, 'HK:00001')['research']
    assert current['latest_version'] == 1
    assert current['status'] == 'needs_update'
    history = load_stock(settings, 'HK:00001', 2)['research']
    assert history['historical'] is True
    assert history['baseline']['version'] == 2
    assert history['job_status'] == 'needs_update'
    assert history['status'] == 'needs_update'


@pytest.mark.parametrize('payload', ['{', '[]', '{"stocks":null}', 'x' * 2_000_001], ids=['broken', 'array', 'missing-fields', 'oversize'])
def test_bad_snapshot_fails_closed(archive, payload):
    settings, conn = archive
    conn.execute('UPDATE member_configs SET snapshot_json=? WHERE member_id=(SELECT id FROM members WHERE user_id=?)', (payload, 'alice'))
    conn.commit()
    with pytest.raises(Unavailable):
        load_dashboard(settings)


def test_all_table_fingerprints_unchanged(archive):
    import hashlib
    settings, conn = archive
    ResearchStore(conn).append_baseline(sourced_document(), NOW)
    conn.commit()
    def fingerprint():
        return hashlib.sha256('\n'.join(conn.iterdump()).encode()).hexdigest()
    before = fingerprint()
    load_dashboard(settings)
    load_stock(settings, 'HK:00001', 1)
    assert fingerprint() == before


def test_future_schema_unavailable(archive):
    settings, conn = archive
    conn.execute('PRAGMA user_version=99999')
    with pytest.raises(Unavailable):
        load_dashboard(settings)
