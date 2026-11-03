import argparse
import json
import sqlite3

import pytest

from stock_watch.research import ResearchStore, SCHEMA_SQL
from stock_watch.research_cli import add_parser, run
from test_research import document


@pytest.fixture
def conn():
    conn = sqlite3.connect(':memory:')
    conn.executescript(SCHEMA_SQL)
    conn.executescript('CREATE TABLE app_meta(key TEXT,value TEXT); CREATE TABLE stocks(symbol TEXT,name TEXT,market TEXT,enabled INTEGER,paused INTEGER); CREATE TABLE members(id INTEGER,active INTEGER); CREATE TABLE member_configs(member_id INTEGER,snapshot_json TEXT); CREATE TABLE outbox(value TEXT); CREATE TABLE seen_articles(value TEXT);')
    conn.execute("INSERT INTO outbox VALUES ('keep')")
    conn.execute("INSERT INTO seen_articles VALUES ('keep')")
    conn.commit()
    return conn


def invoke(conn, *argv):
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest='command',required=True))
    return run(parser.parse_args(['research', *map(str,argv)]), conn)


def write_json(path, obj):
    path.write_text(json.dumps(obj),encoding='utf-8')
    return path


def fixture_doc():
    doc = document()
    doc['as_of'] = '2026-01-01T00:00:00Z'
    return doc


def test_import_list_show_jobs_export_and_no_side_effects(conn,tmp_path):
    doc = fixture_doc()
    doc['company']['name'] = '<script>alert(1)</script> [unsafe](javascript:x)'
    doc['company']['company_id'] = '../../escape'
    path = write_json(tmp_path/'input.json',doc)
    first = invoke(conn,'import',path)
    assert first['imported'][0]['version'] == 1
    assert invoke(conn,'import',path)['imported'][0]['version'] == 1
    assert len(invoke(conn,'list')) == 1
    assert invoke(conn,'show','../../escape')['document'] == doc
    assert invoke(conn,'show','../../escape','--as-of','2026-01-02T00:00:00Z') is None
    assert invoke(conn,'jobs')[0]['status'] == 'partial'
    output = tmp_path/'reports'
    assert invoke(conn,'export','--output',output)['companies'] == 1
    markdown = '\n'.join(p.read_text(encoding='utf-8') for p in output.glob('*.md'))
    assert '<script>' not in markdown
    assert '&lt;script&gt;' in markdown
    assert len(list(output.iterdir())) == 3
    assert all(p.parent == output for p in output.iterdir())
    assert not (tmp_path/'escape').exists()
    assert conn.execute('SELECT value FROM outbox').fetchone()[0] == 'keep'
    assert conn.execute('SELECT value FROM seen_articles').fetchone()[0] == 'keep'


def test_batch_import_rollback(conn,tmp_path):
    write_json(tmp_path/'a.json',fixture_doc())
    bad = fixture_doc()
    bad['untrusted'] = 'python'
    write_json(tmp_path/'b.json',bad)
    with pytest.raises(ValueError):
        invoke(conn,'import',tmp_path)
    assert not ResearchStore(conn).list_baselines()
    assert not ResearchStore(conn).list_jobs()


def test_watchlist_prepare_idempotent_and_atomic(conn,tmp_path):
    path = write_json(tmp_path/'watch.json',{'checked_at':'2026-01-01T00:00:00Z','securities':[{'symbol':'HK:00001','name':'Example'}]})
    first = invoke(conn,'prepare','--watchlist',path)
    assert first == invoke(conn,'prepare','--watchlist',path)
    assert first['prepared'] == ['security:HK:00001']
    assert len(invoke(conn,'jobs')) == 1
    bad = {'checked_at':'2026-01-01T00:00:00Z','securities':[{'symbol':'HK:00002','name':'New'},{'symbol':'bad symbol','name':'Bad'}]}
    write_json(path,bad)
    with pytest.raises(ValueError):
        invoke(conn,'prepare','--watchlist',path)
    assert len(invoke(conn,'jobs')) == 1


def test_prepare_reuses_company_map(conn,tmp_path):
    invoke(conn,'import',write_json(tmp_path/'doc.json',fixture_doc()))
    watch = write_json(tmp_path/'watch.json',{'checked_at':'2026-01-01T00:00:00Z','securities':[{'symbol':'HK:00001','name':'Alias'}]})
    assert invoke(conn,'prepare','--watchlist',watch)['prepared'] == ['example']


def test_from_subscriptions_active_enabled_only_no_private_fields(conn):
    conn.execute("INSERT INTO app_meta VALUES ('independent_subscriptions','org')")
    conn.executemany('INSERT INTO members VALUES (?,?)', [(1,1),(2,0)])
    def stock(symbol,enabled=1,paused=0):
        return {'symbol':symbol,'name':'Public','market':'HK','enabled':enabled,'paused':paused,'focus':'PRIVATE MEMBER CONTEXT'}
    conn.executemany('INSERT INTO member_configs VALUES (?,?)',[(1,json.dumps({'stocks':[stock('HK:00001'),stock('HK:00002',0),stock('HK:00003',1,1)]})),(2,json.dumps({'stocks':[stock('HK:00004')]}))])
    before = list(conn.execute('SELECT * FROM member_configs'))
    assert invoke(conn,'prepare','--from-subscriptions')['prepared'] == ['security:HK:00001']
    assert list(conn.execute('SELECT * FROM member_configs')) == before
    assert 'PRIVATE' not in str(list(conn.execute('SELECT * FROM research_companies')))


def test_empty_import_rejected(conn,tmp_path):
    with pytest.raises(ValueError):
        invoke(conn,'import',tmp_path)


def test_prepare_placeholder_then_import_official_identity(conn,tmp_path):
    watch = write_json(tmp_path/'watch.json',{'checked_at':'2026-01-01T00:00:00Z','securities':[{'symbol':'600001.SH','name':'Example'}]})
    invoke(conn,'prepare','--watchlist',watch)
    doc = fixture_doc()
    doc['securities'] = [{'symbol':'600001.SH','market':'SSE','currency':'CNY'}]
    path = write_json(tmp_path/'doc.json',doc)
    assert invoke(conn,'import',path)['imported'][0]['version'] == 1
    assert invoke(conn,'import',path)['imported'][0]['version'] == 1
    assert [job['company_id'] for job in invoke(conn,'jobs')] == ['example']
    assert conn.execute("SELECT 1 FROM research_companies WHERE company_id LIKE 'security:%'").fetchone() is None
    assert ResearchStore(conn).company_securities('example')[0]['market'] == 'SSE'


def test_placeholder_with_history_cannot_be_rebound(conn,tmp_path):
    doc = fixture_doc()
    doc['company']['company_id'] = 'security:HK:00001'
    path = write_json(tmp_path/'doc.json',doc)
    invoke(conn,'import',path)
    doc['company']['company_id'] = 'official'
    write_json(path,doc)
    with pytest.raises(ValueError):
        invoke(conn,'import',path)
    assert conn.execute("SELECT 1 FROM research_companies WHERE company_id='official'").fetchone() is None


def test_review_does_not_overwrite_history_or_reset_on_duplicate(conn,tmp_path):
    path = write_json(tmp_path/'doc.json',fixture_doc())
    invoke(conn,'import',path)
    original = invoke(conn,'show','example')
    invoke(conn,'review','example','--reason','New report needs reconciliation')
    invoke(conn,'import',path)
    assert invoke(conn,'jobs')[0]['status'] == 'needs_update'
    assert invoke(conn,'show','example') == original
    assert invoke(conn,'list')[0]['research_status'] == 'needs_update'
    output = tmp_path/'out'
    invoke(conn,'export','--output',output)
    assert any(r'needs\_update' in p.read_text(encoding='utf-8') for p in output.glob('*.md'))
    changed = fixture_doc()
    changed['sections']['risks']['statements'][0]['text'] = 'Reconciled'
    write_json(path,changed)
    invoke(conn,'import',path)
    assert invoke(conn,'jobs')[0]['status'] == 'partial'
    assert len(ResearchStore(conn).review_requests('example')) == 1


@pytest.mark.parametrize('market,currency', [('SSE','CNY'),('SZSE','CNY'),('SEHK','HKD')])
def test_subscription_exchange_currency_mapping(conn,market,currency):
    conn.execute('INSERT INTO stocks VALUES (?,?,?,?,?)', ('600001.SH','Example',market,1,0))
    invoke(conn,'prepare','--from-subscriptions')
    assert ResearchStore(conn).company_securities('security:600001.SH')[0]['currency'] == currency
