"""Authoritative body refreshes remain distinct from incomplete search excerpts."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from stock_watch.db import connect, init_db
from stock_watch.news import NewsArticle, _clean_text, parse_published_at
from stock_watch.quality import QUALITY_SCHEMA_SQL, QualityStore, revision_key
from stock_watch.subscription_scan import _save_report
from stock_watch.subscription_monitor import _read_article


NOW = datetime(2026, 11, 5, 2, tzinfo=timezone.utc)


def article(**fields):
    value = dict(key='synthetic-body', raw_id='synthetic-body', title='合成企业需求情况',
                 summary='公司确认下游订单持续增长，客户采购积极。' * 5,
                 url='https://finance.eastmoney.com/a/12345.html', source='合成来源',
                 published_at=NOW, source_published_at=NOW, target_keys=('synthetic-target',),
                 evidence_status='full_text', evidence_checked_at=NOW.isoformat())
    value.update(fields)
    return NewsArticle(**value)


def database():
    conn = connect(':memory:')
    init_db(conn)
    return conn


def observe_and_archive(conn, value, now):
    conn.execute('BEGIN')
    selected = QualityStore(conn).observe(value, now)
    _save_report(conn, selected, 'eastmoney', now)
    conn.commit()
    return selected


@pytest.mark.parametrize('with_subscriber_archive', [False, True])
def test_latest_shorter_full_body_updates_without_manufacturing_a_revision(with_subscriber_archive):
    conn = database()
    first = article()
    apply = observe_and_archive if with_subscriber_archive else lambda c, a, n: QualityStore(c).observe(a, n)
    before = apply(conn, first, NOW)
    after_input = replace(first, summary='公司否认订单增长，相关说法不实。',
                          evidence_checked_at=(NOW + timedelta(minutes=20)).isoformat())
    after = apply(conn, after_input, NOW + timedelta(minutes=20))
    assert revision_key(before) == revision_key(after_input) == revision_key(after)
    assert after.summary == _clean_text(after_input.summary)
    assert len(after.summary) < len(before.summary)
    assert conn.execute('SELECT count(*) FROM quality_article_revisions').fetchone()[0] == 1
    stored = conn.execute('SELECT summary FROM quality_article_revisions').fetchone()[0]
    assert stored == after.summary
    assert conn.execute('SELECT count(*) FROM quality_article_observations').fetchone()[0] == 2
    if with_subscriber_archive:
        cached = _read_article(conn.execute('SELECT article_json FROM subscription_articles').fetchone()[0])
        assert cached.summary == after.summary and cached.evidence_checked_at == after.evidence_checked_at


@pytest.mark.parametrize('status', ['snippet', 'unavailable'])
def test_excerpt_or_failed_body_does_not_impersonate_a_full_text_refresh(status):
    conn = database()
    before = observe_and_archive(conn, article(), NOW)
    incoming = replace(article(), summary='不同查询取得的需求片段。' * 30, evidence_status=status,
                       evidence_checked_at=(NOW + timedelta(minutes=20)).isoformat())
    after = observe_and_archive(conn, incoming, NOW + timedelta(minutes=20))
    assert after.summary == before.summary
    assert after.evidence_status == 'full_text'
    assert parse_published_at(after.evidence_checked_at) == parse_published_at(before.evidence_checked_at)


def test_same_title_subscriber_full_body_metadata_can_seed_old_quality_archive():
    conn = database()
    prior = observe_and_archive(conn, article(), NOW)
    conn.execute("DELETE FROM monitor_state WHERE key LIKE 'quality-body-v1:%'")
    conn.commit()
    incoming = replace(article(), summary='查询摘录。', evidence_status='unavailable',
                       evidence_checked_at=(NOW + timedelta(minutes=20)).isoformat())
    after = observe_and_archive(conn, incoming, NOW + timedelta(minutes=20))
    assert after.summary == prior.summary and after.evidence_status == 'full_text'
    assert parse_published_at(after.evidence_checked_at) == parse_published_at(prior.evidence_checked_at)


def test_old_full_body_never_gets_copied_to_changed_title():
    conn = database()
    observe_and_archive(conn, article(), NOW)
    incoming = replace(article(), title='合成企业新版业务说明', summary='新标题下待核的材料。',
                       evidence_status='unavailable', evidence_checked_at=(NOW + timedelta(minutes=20)).isoformat())
    after = observe_and_archive(conn, incoming, NOW + timedelta(minutes=20))
    assert after.title == incoming.title and after.summary == incoming.summary
    assert after.evidence_status == 'unavailable'


def test_stale_full_body_check_cannot_replace_a_more_recent_verified_body():
    conn = database()
    newer = article(summary='公司明确否认此前增长说法。', evidence_checked_at=(NOW + timedelta(minutes=20)).isoformat())
    accepted = observe_and_archive(conn, newer, NOW + timedelta(minutes=20))
    stale = article(evidence_checked_at=NOW.isoformat())
    after = observe_and_archive(conn, stale, NOW + timedelta(minutes=25))
    assert after.summary == accepted.summary
    assert parse_published_at(after.evidence_checked_at) == parse_published_at(accepted.evidence_checked_at)


def test_unproven_excerpt_cannot_erase_a_known_numeric_correction():
    conn = database()
    corrected = article(title='合成企业订单公告', summary='公司更正订单金额为8亿元。')
    before = observe_and_archive(conn, corrected, NOW)
    incoming = replace(corrected, summary='公司订单金额为10亿元。', evidence_status='snippet',
                       evidence_checked_at=(NOW + timedelta(minutes=20)).isoformat())
    after = observe_and_archive(conn, incoming, NOW + timedelta(minutes=20))
    assert after.summary == before.summary
    assert revision_key(after) == revision_key(before)


def test_historical_numeric_full_body_cannot_undo_newer_correction():
    conn = database()
    initial = article(title='合成企业订单公告', summary='公司公告订单金额为10亿元。')
    observe_and_archive(conn, initial, NOW)
    correction = replace(initial, summary='公司公告更正订单金额为8亿元。',
                         published_at=NOW + timedelta(minutes=10), source_published_at=NOW + timedelta(minutes=10),
                         evidence_checked_at=(NOW + timedelta(minutes=10)).isoformat())
    before = observe_and_archive(conn, correction, NOW + timedelta(minutes=10))
    historical = replace(initial, evidence_checked_at=(NOW + timedelta(minutes=20)).isoformat())
    after = observe_and_archive(conn, historical, NOW + timedelta(minutes=20))
    assert after.summary == before.summary and revision_key(after) == revision_key(before)


def test_standalone_quality_metadata_creation_rolls_back_with_callers_transaction():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(QUALITY_SCHEMA_SQL)
    conn.execute('BEGIN')
    QualityStore(conn).observe(article(), NOW)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='monitor_state'").fetchone()
    conn.rollback()
    assert not conn.execute('SELECT * FROM quality_articles').fetchall()
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='monitor_state'").fetchone()
