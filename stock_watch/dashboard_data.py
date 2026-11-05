"""Bounded, organization-scoped read models for the administration portal."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .research import ResearchStore
from .db import SCHEMA_VERSION


class Unavailable(Exception):
    """The configured archive cannot safely be displayed."""


@contextmanager
def _read(settings):
    conn = None
    try:
        conn = sqlite3.connect(Path(settings.database_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        if conn.execute('PRAGMA user_version').fetchone()[0] > SCHEMA_VERSION:
            raise Unavailable('unsupported_schema')
        mode = conn.execute("SELECT value FROM app_meta WHERE key='independent_subscriptions'").fetchone()
        if mode and mode[0] != settings.wecom_corp_id:
            raise Unavailable('organization_mismatch')
        yield conn, bool(mode)
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise Unavailable('dashboard_data_unavailable') from None
    finally:
        if conn is not None:
            conn.close()


def _domain(data):
    if not isinstance(data, dict):
        raise ValueError()
    stocks = [{k: row.get(k) for k in ('symbol', 'name', 'focus', 'enabled', 'paused', 'direction', 'push_level', 'updated_at')} for row in data['stocks']]
    themes = [{k: row.get(k) for k in ('name', 'enabled', 'updated_at')} | {'keywords': [kw['keyword'] for kw in data['theme_keywords'] if kw['theme_id'] == row['id']]} for row in data['themes']]
    return stocks, themes


def _research(conn, symbol, version=None):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    empty = dict(status='not_started', job_status=None, baseline=None, versions=[], securities=[], source_count=0, financial_period=None, readiness={})
    if 'research_securities' not in tables:
        return empty
    mapping = conn.execute('SELECT company_id FROM research_securities WHERE symbol=?', (symbol,)).fetchone()
    if not mapping:
        return empty
    store = ResearchStore(conn)
    company = mapping[0]
    latest = store.get_baseline(company)
    records = store.list_baselines(company)
    selected = latest if version is None else next((r for r in records if str(r['version']) == str(version)), None)
    job = conn.execute('SELECT status FROM research_jobs WHERE company_id=?', (company,)).fetchone()
    job_status = job[0] if job else None
    result = empty | dict(job_status=job_status, status=job_status or 'not_started', company_id=company, securities=store.company_securities(company), versions=[{'version': r['version'], 'as_of': r['as_of'], 'created_at': r['created_at']} for r in records], baseline=selected, historical=bool(selected and latest and selected['id'] != latest['id']), latest_version=latest['version'] if latest else None)
    # These are current operational checks, kept separate from the selected archive.
    current_update = dict(last_checked=None, last_updated=latest['created_at'] if latest else None,
                          covered_through=None, gaps=[], task_status=None, export_status=None)
    if 'research_update_checks' in tables:
        check = conn.execute('SELECT last_checked,last_updated,covered_through,gaps_json FROM research_update_checks WHERE company_id=?', (company,)).fetchone()
        if check:
            current_update.update(last_checked=check[0], last_updated=check[1] or current_update['last_updated'],
                                  covered_through=check[2], gaps=json.loads(check[3]))
    if 'research_update_items' in tables:
        task = conn.execute('SELECT status,receipt_json FROM research_update_items WHERE company_id=? ORDER BY id DESC LIMIT 1', (company,)).fetchone()
        if task:
            receipt = json.loads(task[1]) if task[1] else {}
            current_update.update(task_status=task[0], export_status='exported' if receipt.get('exported') else
                                  'pending' if task[0] == 'committed_export_pending' else 'not_required')
    result['current_update'] = current_update
    if selected:
        doc = selected['document']
        result.update(source_count=len(doc.get('sources', [])), financial_period=doc.get('financial_review', {}).get('period'), readiness=selected['readiness'], as_of=selected['as_of'], created_at=selected['created_at'])
        if job_status != 'needs_update':
            result['status'] = 'usable' if latest['readiness'].get('fundamental') else 'partial'
    return result


def load_dashboard(settings, query=''):
    with _read(settings) as (conn, personal):
        members = []
        if personal:
            rows = conn.execute('SELECT m.user_id,m.role,m.active,m.updated_at,c.config_version,c.snapshot_json FROM members m LEFT JOIN member_configs c ON c.member_id=m.id WHERE m.corp_id=? ORDER BY m.user_id', (settings.wecom_corp_id,))
            for row in rows:
                if row['snapshot_json'] is None or len(row['snapshot_json']) > 2_000_000:
                    raise Unavailable('member_configuration_unavailable')
                stocks, themes = _domain(json.loads(row['snapshot_json']))
                members.append({k: row[k] for k in ('user_id', 'role', 'active', 'updated_at', 'config_version')} | dict(stocks=stocks, themes=themes))
        else:
            data = {t: [dict(r) for r in conn.execute('SELECT * FROM ' + t)] for t in ('stocks', 'themes', 'theme_keywords')}
            stocks, themes = _domain(data)
            state = conn.execute('SELECT config_version,updated_at FROM config_state WHERE singleton=1').fetchone()
            members.append(dict(user_id='legacy', role='legacy', active=True, config_version=state[0], updated_at=state[1], stocks=stocks, themes=themes))
        grouped = {}
        for member in members:
            for stock in member['stocks']:
                entry = grouped.setdefault(stock['symbol'], dict(symbol=stock['symbol'], name=stock['name'], watchers=[]))
                entry['watchers'].append(dict(user_id=member['user_id'], active=member['active'], **stock))
        for entry in grouped.values():
            entry['research'] = _research(conn, entry['symbol'])
        stocks = list(grouped.values())
        counts = dict(members=len(members), stocks=len(stocks), active_stocks=sum(any(w['active'] and w['enabled'] and not w['paused'] for w in s['watchers']) for s in stocks), themes=sum(len(m['themes']) for m in members))
        counts['usable_research'] = sum(s['research']['status'] == 'usable' for s in stocks)
        counts['pending_research'] = len(stocks) - counts['usable_research']
        if query:
            needle = query.casefold()
            members = [m for m in members if needle in json.dumps(m, ensure_ascii=False).casefold()]
            stocks = [s for s in stocks if needle in (s['symbol'] + s['name']).casefold()]
        return dict(members=members, stocks=stocks, counts=counts, mode='members' if personal else 'legacy', snapshot_at=datetime.now(timezone.utc).isoformat())


def load_stock(settings, symbol, version=None):
    dashboard = load_dashboard(settings)
    stock = next((s for s in dashboard['stocks'] if s['symbol'] == symbol), None)
    if stock is None:
        return None
    if version is not None:
        with _read(settings) as (conn, _):
            stock['research'] = _research(conn, symbol, version)
    return stock
