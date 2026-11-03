"""Offline, append-only public company research archive. No delivery side effects."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import re
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlsplit

SECTIONS = ('business_model', 'industry_cycle', 'competitive_advantage', 'earnings_drivers', 'financial_quality', 'governance', 'scenarios_expectations', 'valuation', 'risks', 'catalysts')
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS research_companies(company_id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_securities(symbol TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES research_companies(company_id), market TEXT NOT NULL, currency TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_jobs(company_id TEXT PRIMARY KEY REFERENCES research_companies(company_id), status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_review_requests(id INTEGER PRIMARY KEY, company_id TEXT NOT NULL REFERENCES research_companies(company_id), reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_baselines(id INTEGER PRIMARY KEY, company_id TEXT NOT NULL REFERENCES research_companies(company_id), version INTEGER NOT NULL, as_of TEXT NOT NULL, created_at TEXT NOT NULL, content_hash TEXT NOT NULL, document_json TEXT NOT NULL, readiness_json TEXT NOT NULL, UNIQUE(company_id,version), UNIQUE(company_id,content_hash));
"""


def _time(value):
    if not isinstance(value, str):
        raise ValueError('timestamp required')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('invalid timestamp') from None
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result


def _keys(obj, allowed, required=()):
    if not isinstance(obj, dict) or set(obj) - set(allowed) or set(required) - set(obj):
        raise ValueError('invalid research fields')


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 20000:
        raise ValueError('nonempty bounded text required')


def _security(security):
    _keys(security, ('symbol', 'market', 'currency'), ('symbol', 'market', 'currency'))
    if not isinstance(security['symbol'], str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.:_-]{0,31}', security['symbol']):
        raise ValueError('invalid stable security symbol')
    if not isinstance(security['market'], str) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,11}', security['market']):
        raise ValueError('invalid market')
    if not isinstance(security['currency'], str) or not re.fullmatch(r'[A-Z]{3}', security['currency']):
        raise ValueError('invalid currency')


def _validate(doc, now):
    _keys(doc, ('schema_version','company','securities','as_of','sections','sources','assumptions','monitor_mappings','readiness','financial_review'), ('schema_version','company','securities','as_of','sections','sources','assumptions','monitor_mappings'))
    if doc['schema_version'] != 1:
        raise ValueError('unsupported research schema')
    cutoff = _time(doc['as_of'])
    if cutoff > _time(now):
        raise ValueError('future baseline')
    _keys(doc['company'], ('company_id','name'), ('company_id','name'))
    for value in doc['company'].values():
        _text(value)
    if not isinstance(doc['securities'], list) or not doc['securities']:
        raise ValueError('securities required')
    for security in doc['securities']:
        _security(security)
    ids = set()
    if not isinstance(doc['sources'], list):
        raise ValueError('sources must be a list')
    for source in doc['sources']:
        _keys(source, ('id','url','title','published_at','available_at','ingested_at','locator'), ('id','url','title','published_at','available_at','ingested_at','locator'))
        for key, value in source.items():
            if key != 'published_at' or value is not None:
                _text(value)
        parsed = urlsplit(source['url'])
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('public source URL required')
        if source['id'] in ids:
            raise ValueError('duplicate evidence ID')
        ids.add(source['id'])
        published = _time(source['published_at']) if source['published_at'] is not None else None
        available, ingested = (_time(source[k]) for k in ('available_at','ingested_at'))
        if (published is not None and published > available) or not available <= ingested <= _time(now) or available > cutoff:
            raise ValueError('source time ordering or cutoff violated')
    def refs(values):
        if not isinstance(values, list) or any(not isinstance(x,str) or x not in ids for x in values):
            raise ValueError('unknown evidence reference')
    _keys(doc['sections'], SECTIONS, SECTIONS)
    supported = set()
    for name, section in doc['sections'].items():
        _keys(section, ('statements','metrics'), ('statements',))
        if not isinstance(section['statements'], list):
            raise ValueError('statements must be a list')
        for statement in section['statements']:
            _keys(statement, ('kind','text','evidence_ids'), ('kind','text','evidence_ids'))
            if statement['kind'] not in ('fact','inference','assumption','unknown'):
                raise ValueError('invalid statement kind')
            _text(statement['text'])
            refs(statement['evidence_ids'])
            if statement['kind'] == 'fact' and not statement['evidence_ids']:
                raise ValueError('facts require evidence')
            if statement['kind'] == 'fact':
                supported.add(name)
        if not isinstance(section.get('metrics', []), list):
            raise ValueError('metrics must be a list')
        for metric in section.get('metrics', []):
            _keys(metric, ('name','value','period','unit','currency','basis','evidence_ids'), ('name','value','period','unit','currency','basis','evidence_ids'))
            for key in ('name','period','unit','currency','basis'):
                _text(metric[key])
            value = metric['value']
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError('metrics require integer or exact decimal string')
            if isinstance(value, str) and not re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', value):
                raise ValueError('invalid decimal metric')
            try:
                if not Decimal(value).is_finite():
                    raise ValueError('nonfinite metric')
            except InvalidOperation:
                raise ValueError('invalid decimal metric') from None
            refs(metric['evidence_ids'])
            if not metric['evidence_ids']:
                raise ValueError('metric evidence required')
    if not isinstance(doc['assumptions'], list) or not isinstance(doc['monitor_mappings'], list):
        raise ValueError('research tables must be lists')
    for assumption in doc['assumptions']:
        _keys(assumption, ('hypothesis','support_evidence_ids','counter_evidence','review_condition'), ('hypothesis','support_evidence_ids','counter_evidence','review_condition'))
        refs(assumption['support_evidence_ids'])
        for key in ('hypothesis','counter_evidence','review_condition'):
            _text(assumption[key])
    for mapping in doc['monitor_mappings']:
        _keys(mapping, ('event','business','metric','transmission','verify','evidence_ids'), ('event','business','metric','transmission','verify','evidence_ids'))
        refs(mapping['evidence_ids'])
        for key in ('event','business','metric','transmission','verify'):
            _text(mapping[key])
    review = doc.get('financial_review', {})
    if review:
        _keys(review, ('period','source_id','latest_available_checked'), ('period','source_id','latest_available_checked'))
        _text(review['period'])
        if review['source_id'] not in ids or type(review['latest_available_checked']) is not bool:
            raise ValueError('invalid financial review')
    requested = doc.get('readiness', {})
    _keys(requested, ('trackable','fundamental','expectation_comparison','validated_prediction'))
    if any(type(value) is not bool for value in requested.values()) or requested.get('expectation_comparison') or requested.get('validated_prediction'):
        raise ValueError('unsupported prediction capability')
    trackable = bool(ids and any(m['evidence_ids'] for m in doc['monitor_mappings']))
    financial = doc['sections']['financial_quality']
    financial_checked = bool(review.get('latest_available_checked') and any(review.get('source_id') in st['evidence_ids'] for st in financial['statements'] if st['kind'] == 'fact') and any(m['period'] == review.get('period') and review.get('source_id') in m['evidence_ids'] for m in financial.get('metrics', [])))
    reasons = []
    if not trackable:
        reasons.append('missing_evidenced_monitor_mapping')
    for name in SECTIONS:
        if not doc['sections'][name]['statements']:
            reasons.append('missing_section_statement:' + name)
        elif name in ('business_model','earnings_drivers','financial_quality','risks') and name not in supported:
            reasons.append('unverified_core_section:' + name)
    if not financial_checked:
        reasons.append('latest_financial_period_not_reconciled')
    if not any(a['support_evidence_ids'] for a in doc['assumptions']):
        reasons.append('missing_evidenced_core_assumption')
    fundamental = not reasons
    return dict(trackable=trackable, fundamental=fundamental, expectation_comparison=False, validated_prediction=False, reasons=reasons)



class ResearchStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @contextmanager
    def _atomic(self):
        # SAVEPOINT preserves any caller-owned transaction, including on failure.
        self.conn.execute('SAVEPOINT research_write')
        try:
            yield
        except BaseException:
            self.conn.execute('ROLLBACK TO research_write')
            self.conn.execute('RELEASE research_write')
            raise
        else:
            self.conn.execute('RELEASE research_write')

    def upsert_company(self, company, securities):
        _keys(company, ('company_id', 'name'), ('company_id', 'name'))
        for value in company.values():
            _text(value)
        if not isinstance(securities, list) or not securities:
            raise ValueError('securities required')
        for security in securities:
            _security(security)
        with self._atomic():
            self.conn.execute('INSERT INTO research_companies VALUES (?,?) ON CONFLICT(company_id) DO UPDATE SET name=excluded.name', (company['company_id'],company['name']))
            for s in securities:
                existing = self.conn.execute('SELECT company_id,market,currency FROM research_securities WHERE symbol=?', (s['symbol'],)).fetchone()
                if existing and tuple(existing) != (company['company_id'],s['market'],s['currency']):
                    old_id = existing[0]
                    placeholder = old_id == 'security:' + s['symbol'] and old_id != company['company_id']
                    history = self.conn.execute('SELECT 1 FROM research_baselines WHERE company_id=?', (old_id,)).fetchone()
                    count = self.conn.execute('SELECT COUNT(*) FROM research_securities WHERE company_id=?', (old_id,)).fetchone()[0]
                    if not placeholder or history or count != 1 or existing[2] != s['currency']:
                        raise ValueError('security belongs to another company')
                    self.conn.execute('UPDATE research_securities SET company_id=?,market=? WHERE symbol=?', (company['company_id'],s['market'],s['symbol']))
                    job = self.conn.execute('SELECT status,created_at FROM research_jobs WHERE company_id=?', (old_id,)).fetchone()
                    if job:
                        self.conn.execute('INSERT OR IGNORE INTO research_jobs VALUES (?,?,?)', (company['company_id'],job[0],job[1]))
                    self.conn.execute('UPDATE research_review_requests SET company_id=? WHERE company_id=?', (company['company_id'],old_id))
                    self.conn.execute('DELETE FROM research_jobs WHERE company_id=?', (old_id,))
                    self.conn.execute('DELETE FROM research_companies WHERE company_id=?', (old_id,))
                self.conn.execute('INSERT INTO research_securities VALUES (?,?,?,?) ON CONFLICT(symbol) DO NOTHING', (s['symbol'],company['company_id'],s['market'],s['currency']))
        return company['company_id']

    def company_securities(self, company_id):
        return [dict(zip(('symbol','market','currency'), row)) for row in self.conn.execute('SELECT symbol,market,currency FROM research_securities WHERE company_id=? ORDER BY symbol', (company_id,))]

    def ensure_preparation(self, company_id, now):
        _time(now)
        with self._atomic():
            self.conn.execute("INSERT OR IGNORE INTO research_jobs VALUES (?,'pending',?)", (company_id,now))

    def list_jobs(self):
        return [dict(zip(('company_id','status','created_at'), row)) for row in self.conn.execute('SELECT company_id,status,created_at FROM research_jobs ORDER BY company_id')]

    def mark_for_review(self, company_id, reason, now):
        _text(reason)
        _time(now)
        if not self.conn.execute('SELECT 1 FROM research_companies WHERE company_id=?', (company_id,)).fetchone():
            raise ValueError('unknown research company')
        with self._atomic():
            self.ensure_preparation(company_id, now)
            self.conn.execute('INSERT INTO research_review_requests(company_id,reason,created_at) VALUES (?,?,?)', (company_id,reason,now))
            self.conn.execute("UPDATE research_jobs SET status='needs_update' WHERE company_id=?", (company_id,))
        return {'company_id':company_id, 'status':'needs_update', 'reason':reason, 'created_at':now}

    def review_requests(self, company_id):
        return [dict(zip(('reason','created_at'), row)) for row in self.conn.execute('SELECT reason,created_at FROM research_review_requests WHERE company_id=? ORDER BY id', (company_id,))]

    def append_baseline(self, document, now):
        readiness = _validate(document, now)
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        company_id = document['company']['company_id']
        with self._atomic():
            self.upsert_company(document['company'],document['securities'])
            self.ensure_preparation(company_id, now)
            existing = self.conn.execute('SELECT id FROM research_baselines WHERE company_id=? AND content_hash=?', (company_id,digest)).fetchone()
            if existing:
                return self._read(existing[0])
            version = self.conn.execute('SELECT COALESCE(MAX(version),0)+1 FROM research_baselines WHERE company_id=?', (company_id,)).fetchone()[0]
            row = self.conn.execute('INSERT INTO research_baselines(company_id,version,as_of,created_at,content_hash,document_json,readiness_json) VALUES (?,?,?,?,?,?,?)', (company_id,version,document['as_of'],now,digest,payload,json.dumps(readiness)))
            if self.get_baseline(company_id)['id'] == row.lastrowid:
                self.conn.execute("UPDATE research_jobs SET status=? WHERE company_id=?", ('usable' if readiness['fundamental'] else 'partial', company_id))
            return self._read(row.lastrowid)

    def _read(self, baseline_id):
        row = self.conn.execute('SELECT id,company_id,version,as_of,created_at,content_hash,document_json,readiness_json FROM research_baselines WHERE id=?', (baseline_id,)).fetchone()
        result = dict(zip(('id','company_id','version','as_of','created_at','content_hash','document','readiness'),row))
        result['document'] = json.loads(result['document'])
        result['readiness'] = json.loads(result['readiness'])
        return result

    def list_baselines(self, company_id=None):
        rows = self.conn.execute('SELECT id FROM research_baselines' + (' WHERE company_id=?' if company_id else '') + ' ORDER BY id', (company_id,) if company_id else ())
        return [self._read(row[0]) for row in rows]

    def get_baseline(self, company_id, as_of=None):
        records = self.list_baselines(company_id)
        if as_of is not None:
            cutoff = _time(as_of)
            records = [r for r in records if _time(r['as_of']) <= cutoff and _time(r['created_at']) <= cutoff]
        return max(records, key=lambda r: (_time(r['as_of']), _time(r['created_at']), r['version'])) if records else None
