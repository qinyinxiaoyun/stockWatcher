"""Research is additive and retained independently of notification hot data."""
from datetime import datetime, timedelta, timezone
import json

from stock_watch import research
from stock_watch.config import ConfigStore
from stock_watch.db import SCHEMA_VERSION, connect, init_db
from stock_watch.research import ResearchStore
from stock_watch.subscriptions import MemberConfigStore, MemberRegistry
from test_research import document


def test_schema4_upgrade_preserves_configuration_and_delivery(tmp_path, monkeypatch):
    path = tmp_path / 'migration.db'
    conn = connect(path)
    with monkeypatch.context() as old:
        old.setattr(research, 'SCHEMA_SQL', '')
        init_db(conn)
    conn.execute('PRAGMA user_version=4')
    conn.execute("UPDATE app_meta SET value='4' WHERE key='schema_version'")
    registry = MemberRegistry(conn, 'synthetic-org')
    member = registry.add('synthetic-owner')
    registry.assign_legacy_owner(member.user_id)
    before = ConfigStore(conn).snapshot().to_dict()
    personal = MemberConfigStore(conn, member.id).snapshot().to_dict()
    conn.execute("INSERT INTO seen_articles VALUES ('preserve','2026-11-01T00:00:00Z')")
    conn.execute("INSERT INTO outbox(event_key,payload_json,status,available_at,created_at) VALUES ('keep','{}','sent','2026-11-01T00:00:00Z','2026-11-01T00:00:00Z')")
    old_outbox = [tuple(row) for row in conn.execute('SELECT * FROM outbox')]
    init_db(conn)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION == 6
    current = ConfigStore(conn).snapshot().to_dict()
    before['schema_version'] = current['schema_version']
    personal['schema_version'] = SCHEMA_VERSION
    assert current == before
    assert MemberConfigStore(conn, member.id).snapshot().to_dict() == personal
    assert [tuple(row) for row in conn.execute('SELECT * FROM outbox')] == old_outbox
    assert conn.execute('SELECT article_key FROM seen_articles').fetchone()[0] == 'preserve'
    assert not ResearchStore(conn).list_jobs()
    init_db(conn)
    assert not ResearchStore(conn).list_baselines()
    conn.close()


def test_old_news_pruning_does_not_remove_research(tmp_path):
    from stock_watch.analyzer import FakeAnalyzer
    from stock_watch.subscription_monitor import SubscriptionMonitorService
    path = tmp_path / 'retention.db'
    conn = connect(path)
    init_db(conn)
    registry = MemberRegistry(conn, 'synthetic-org')
    member = registry.add('synthetic-owner')
    registry.assign_legacy_owner(member.user_id)
    doc = document()
    stored = ResearchStore(conn).append_baseline(doc, doc['as_of'])
    class Empty:
        def fetch_target(self, target, **kwargs):
            return []
    analyzer = FakeAnalyzer()
    service = SubscriptionMonitorService(path, Empty(), analyzer, corp_id='synthetic-org',
                                         environ={'MONITOR_ENABLED':'false'})
    later = datetime.fromisoformat(doc['as_of']) + timedelta(days=40)
    assert service.run_once(now=later, force=True).status == 'succeeded'
    assert ResearchStore(conn).get_baseline('example') == stored
    assert not analyzer.calls
    assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
    conn.close()
