import sqlite3
from types import SimpleNamespace

import pytest

from stock_watch.research_update_worker import ResearchUpdateService
from stock_watch.research_cli import _atomic_text
from stock_watch.worker import run_worker_once


def test_backup_retains_original_database(tmp_path):
    database = tmp_path / 'app.sqlite'
    conn = sqlite3.connect(database, isolation_level=None)
    conn.execute('CREATE TABLE evidence(value TEXT)')
    conn.execute("INSERT INTO evidence VALUES ('before')")
    service = ResearchUpdateService(SimpleNamespace(database_path=database))
    service._backup(conn)
    conn.execute("UPDATE evidence SET value='after'")
    service._backup(conn)
    backups = list((tmp_path / 'research' / 'backups').glob('*.sqlite'))
    assert len(backups) == 2
    values = []
    for path in backups:
        with sqlite3.connect(path) as backup:
            values.append(backup.execute('SELECT value FROM evidence').fetchone()[0])
    assert sorted(values) == ['after', 'before']
    conn.close()


def test_atomic_export_failure_keeps_previous_file(tmp_path, monkeypatch):
    target = tmp_path / 'report.md'
    target.write_text('old')
    def fail(*args):
        raise OSError('simulated replacement failure')
    monkeypatch.setattr('stock_watch.research_cli.os.replace', fail)
    with pytest.raises(OSError):
        _atomic_text(target, 'new')
    assert target.read_text() == 'old'
    assert list(tmp_path.iterdir()) == [target]


def test_worker_research_independent_and_dryrun_safe():
    class Research:
        calls = 0
        def run_one(self, now=None):
            self.calls += 1
            return True
    research = Research()
    assert run_worker_once(None, research_service=research).research_processed
    assert not run_worker_once(None, research_service=research, dry_run=True).research_processed
    assert research.calls == 1


def test_disabled_research_does_not_create_database(tmp_path):
    path = tmp_path / 'absent.sqlite'
    assert not ResearchUpdateService(SimpleNamespace(research_api_enabled=False, database_path=path)).run_one()
    assert not path.exists()


def test_real_queue_export_retry_does_not_append_again(tmp_path, monkeypatch):
    from stock_watch.db import connect, init_db
    from stock_watch.research import ResearchStore, SECTIONS
    from stock_watch.research_updates import ResearchUpdates
    from datetime import datetime, timezone
    stamp = '2026-11-05T00:00:00+00:00'
    database = tmp_path / 'app.sqlite'
    settings = SimpleNamespace(database_path=database, monitor_enabled=False,
        research_api_enabled=True, research_apply_enabled=True,
        research_allowed_companies=('gigadevice',), research_report_dir=tmp_path / 'reports')
    conn = connect(database)
    init_db(conn)
    doc = dict(schema_version=1, company=dict(company_id='gigadevice',name='Example'),
        securities=[dict(symbol='603986.SH',market='CN',currency='CNY')], as_of=stamp,
        sections={name:dict(statements=[dict(kind='unknown',text='Unverified',evidence_ids=[])]) for name in SECTIONS},
        sources=[], assumptions=[], monitor_mappings=[])
    ResearchUpdates(conn, ('gigadevice',)).submit('tester',dict(contract_version=1,run_id='run1',as_of=stamp,
        items=[dict(company_id='gigadevice',expected_base_hash=None,result='UPDATE',document=doc,summary='update',
            coverage=dict(**{'from':stamp,'through':stamp},complete=True,sources=[dict(url='https://example.com',status='ok')],gaps=[]))]),apply_enabled=True,now=stamp)
    import stock_watch.research_update_worker as module
    original = module._export
    def fail(*args):
        raise OSError('simulated failure')
    monkeypatch.setattr(module, '_export', fail)
    service = ResearchUpdateService(settings)
    assert run_worker_once(None, research_service=service, now=datetime(2026,11,5,tzinfo=timezone.utc)).research_processed
    assert len(ResearchStore(conn).list_baselines()) == 1
    assert ResearchUpdates(conn).get_status('tester','run1')['status'] == 'committed_export_pending'
    monkeypatch.setattr(module, '_export', original)
    assert service.run_one(now='2026-11-05T00:10:00+00:00')
    assert len(ResearchStore(conn).list_baselines()) == 1
    assert ResearchUpdates(conn).get_status('tester','run1')['status'] == 'completed'
    assert (settings.research_report_dir / 'index.md').exists()
    backup = next((tmp_path / 'research' / 'backups').glob('*.sqlite'))
    with sqlite3.connect(backup) as saved:
        assert ResearchStore(saved).list_baselines() == []
    conn.close()


def test_reject_linked_report_parent(tmp_path):
    from stock_watch.research_cli import _safe_directory
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = tmp_path / 'link'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('host does not permit symlinks')
    with pytest.raises(ValueError):
        _safe_directory(link / 'reports')
    assert not (outside / 'reports').exists()


def test_export_lock_competition_and_exception_release(tmp_path, monkeypatch):
    import stock_watch.research_cli as module
    # Separate open file handles compete through the operating system lock.
    with module._export_lock(tmp_path):
        with pytest.raises(RuntimeError, match='research export busy'):
            with module._export_lock(tmp_path):
                pytest.fail('second exporter entered')
        called = []
        monkeypatch.setattr(module, '_current', lambda store: called.append(True))
        with pytest.raises(RuntimeError, match='research export busy'):
            module._export(None, tmp_path)
        assert called == []  # cannot take a stale snapshot while another export owns lock
    with pytest.raises(ValueError, match='simulated'):
        with module._export_lock(tmp_path):
            raise ValueError('simulated')
    with module._export_lock(tmp_path):
        pass


def test_export_materializes_snapshot_and_preserves_caller_transaction(tmp_path, monkeypatch):
    import stock_watch.research_cli as module
    from stock_watch.research import ResearchStore, SCHEMA_SQL
    conn = sqlite3.connect(':memory:')
    conn.executescript(SCHEMA_SQL)
    conn.execute('CREATE TABLE unrelated(value TEXT)')
    seen = []
    original = module._current
    def current(store):
        seen.append(store.conn.in_transaction)
        return original(store)
    monkeypatch.setattr(module, '_current', current)
    module._export(ResearchStore(conn), tmp_path / 'reports')
    assert seen == [True]
    assert not conn.in_transaction
    conn.execute("INSERT INTO unrelated VALUES ('caller')")
    module._export(ResearchStore(conn), tmp_path / 'reports')
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute('SELECT * FROM unrelated').fetchall() == []
    conn.close()
