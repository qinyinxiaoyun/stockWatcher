"""Bounded, durable research intake. No network or notification side effects."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit

from .research import ResearchStore, SECTIONS, _validate, _time, _keys

REPORT_PATHS = {'gigadevice':'/stocks/603986.SH', 'alibaba':'/stocks/9988.HK', 'smic':'/stocks/688981.SH'}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS research_update_runs (
 principal TEXT NOT NULL, run_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
 as_of TEXT NOT NULL, dry_run INTEGER NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(principal,run_id));
CREATE TABLE IF NOT EXISTS research_update_items (
 id INTEGER PRIMARY KEY, principal TEXT NOT NULL, run_id TEXT NOT NULL,
 company_id TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
 error_code TEXT, lease_token TEXT, lease_until TEXT, receipt_json TEXT,
 UNIQUE(principal,run_id,company_id));
CREATE TABLE IF NOT EXISTS research_update_checks (
 company_id TEXT PRIMARY KEY, last_checked TEXT, last_updated TEXT,
 covered_through TEXT, gaps_json TEXT NOT NULL DEFAULT '[]');
"""


class ResearchUpdateError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def semantic_hash(document):
    """Version one: set ordering and collection timestamps carry no meaning."""
    def normalize(value, root=False):
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items()
                    if k != 'ingested_at' and not (root and k == 'as_of')}
        if isinstance(value, list):
            return sorted((normalize(v) for v in value), key=_json)
        return value
    return _hash({'normalization_version': 1, 'document': normalize(document, True)})


def _now(value):
    return value or datetime.now(timezone.utc).isoformat()


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}', value):
        raise ResearchUpdateError('invalid_identifier')


def _bounded(value, depth=0, count=None):
    if count is None:
        count = [0]
    count[0] += 1
    if depth > 40 or count[0] > 150000:
        raise ValueError('structure_limit')
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key,str):
                raise ValueError('invalid_key')
            _bounded(child,depth+1,count)
    elif isinstance(value,list):
        for child in value:
            _bounded(child,depth+1,count)


class ResearchUpdates:
    def __init__(self, conn, allowed_companies=('gigadevice', 'alibaba', 'smic')):
        if (not isinstance(allowed_companies,(list,tuple)) or not allowed_companies
                or any(not isinstance(c,str) or c not in REPORT_PATHS for c in allowed_companies)
                or len(set(allowed_companies)) != len(allowed_companies)):
            raise ResearchUpdateError('company_not_allowed')
        self.conn = conn
        self.allowed_companies = tuple(allowed_companies)
        self.store = ResearchStore(conn)

    @contextmanager
    def _transaction(self, write=True):
        if self.conn.in_transaction:
            raise ResearchUpdateError('transaction_in_progress', 409)
        self.conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def snapshot(self, company_ids=None, full=False, now=None):
        ids = self.allowed_companies if company_ids is None else company_ids
        if (type(full) is not bool or not isinstance(ids,(list,tuple))
                or any(not isinstance(cid,str) or cid not in self.allowed_companies for cid in ids)
                or len(set(ids)) != len(ids)):
            raise ResearchUpdateError('company_not_allowed')
        with self._transaction(False):
            companies = []
            for cid in ids:
                base = self.store.get_baseline(cid)
                check = self.conn.execute('SELECT last_checked,last_updated,covered_through,gaps_json FROM research_update_checks WHERE company_id=?', (cid,)).fetchone()
                item = dict(company_id=cid, report_path=REPORT_PATHS[cid], version=base['version'] if base else None,
                            hash=base['content_hash'] if base else None,
                            semantic_hash=semantic_hash(base['document']) if base else None,
                            as_of=base['as_of'] if base else None,
                            last_checked=check[0] if check else None,
                            last_updated=(check[1] if check else None) or (base['created_at'] if base else None),
                            covered_through=check[2] if check else None,
                            gaps=json.loads(check[3]) if check else [])
                if full:
                    item['document'] = base['document'] if base else None
                item['window_from'] = item['covered_through'] or item['as_of']
                item['lookback_start'] = (_time(item['window_from'])-timedelta(days=7)).isoformat() if item['window_from'] else None
                companies.append(item)
        return dict(contract_version=1, allowed_companies=list(self.allowed_companies), companies=companies,
                    research_standard={'document_schema_version':1,'sections':list(SECTIONS),'prediction_supported':False},
                    update_contract={'required':['contract_version','run_id','as_of','items'],'results':['UPDATE','NO_CHANGE','INCOMPLETE'],'coverage_required':['from','through','complete','sources','gaps'],'expected_base_hash':'snapshot.hash; null only for absent baseline','document':'required only for UPDATE; full research schema version 1'})

    def submit(self, principal, payload, *, apply_enabled=False, now=None):
        now = _now(now)
        _identity(principal)
        try:
            _bounded(payload)
            _keys(payload, ('contract_version','run_id','as_of','items'), ('contract_version','run_id','as_of','items'))
            if type(payload['contract_version']) is not int or payload['contract_version'] != 1:
                raise ValueError()
            _identity(payload['run_id'])
            if _time(payload['as_of']) > _time(now):
                raise ValueError()
            items = payload['items']
            if not isinstance(items, list) or not 1 <= len(items) <= len(self.allowed_companies):
                raise ValueError()
            seen = set()
            for item in items:
                required = ('company_id','expected_base_hash','result','coverage','summary')
                _keys(item, (*required,'document'), required)
                cid = item['company_id']
                if cid not in self.allowed_companies or cid in seen:
                    raise ValueError()
                seen.add(cid)
                if item['result'] not in ('UPDATE','NO_CHANGE','INCOMPLETE') or ('document' in item) != (item['result'] == 'UPDATE'):
                    raise ValueError()
                digest = item['expected_base_hash']
                if digest is not None and (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
                    raise ValueError()
                if not isinstance(item['summary'], str) or len(item['summary']) > 20000:
                    raise ValueError()
                coverage = item['coverage']
                _keys(coverage, ('from','through','complete','sources','gaps'), ('from','through','complete','sources','gaps'))
                if not _time(coverage['from']) <= _time(coverage['through']) <= _time(payload['as_of']):
                    raise ValueError()
                if type(coverage['complete']) is not bool or not isinstance(coverage['sources'], list) or len(coverage['sources']) > 1000:
                    raise ValueError()
                if not isinstance(coverage['gaps'], list) or len(coverage['gaps']) > 1000 or any(not isinstance(g,str) or not g.strip() or len(g)>20000 for g in coverage['gaps']):
                    raise ValueError()
                for source in coverage['sources']:
                    _keys(source, ('url','status'), ('url','status'))
                    if not isinstance(source['url'],str) or len(source['url']) > 4096:
                        raise ValueError()
                    url = urlsplit(source['url'])
                    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or source['status'] not in ('ok','failed'):
                        raise ValueError()
            encoded = _json(payload)
            if len(encoded.encode()) > 4194304:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise ResearchUpdateError('invalid_payload') from None
        with self._transaction():
            old = self.conn.execute('SELECT payload_hash FROM research_update_runs WHERE principal=? AND run_id=?', (principal,payload['run_id'])).fetchone()
            if old:
                if old[0] != _hash(payload):
                    raise ResearchUpdateError('run_id_conflict',409)
            else:
                self.conn.execute('INSERT INTO research_update_runs VALUES (?,?,?,?,?,?)', (principal,payload['run_id'],_hash(payload),payload['as_of'],int(not apply_enabled),now))
                for item in items:
                    self.conn.execute("INSERT INTO research_update_items(principal,run_id,company_id,payload_json,status) VALUES (?,?,?,?,'queued')", (principal,payload['run_id'],item['company_id'],_json(item)))
        return self.get_status(principal,payload['run_id'])

    def get_status(self, principal, run_id):
        with self._transaction(False):
            return self._get_status(principal,run_id)

    def _get_status(self, principal, run_id):
        _identity(principal)
        _identity(run_id)
        run = self.conn.execute('SELECT as_of,dry_run FROM research_update_runs WHERE principal=? AND run_id=?', (principal,run_id)).fetchone()
        if not run:
            raise ResearchUpdateError('run_not_found',404)
        items = []
        for row in self.conn.execute('SELECT company_id,status,error_code,receipt_json FROM research_update_items WHERE principal=? AND run_id=? ORDER BY id', (principal,run_id)):
            items.append(dict(company_id=row[0],report_path=REPORT_PATHS[row[0]],status=row[1],error_code=row[2], **(json.loads(row[3]) if row[3] else {})))
        statuses = {i['status'] for i in items}
        state = 'queued' if statuses == {'queued'} else ('running' if statuses & {'queued','running'} else ('committed_export_pending' if 'committed_export_pending' in statuses else ('partial_success' if len(statuses)>1 else next(iter(statuses)))))
        return dict(contract_version=1,run_id=run_id,as_of=run[0],dry_run=bool(run[1]),status=state,items=items)

    def process_one(self, *, now=None, apply_enabled=False, before_apply=None, exporter=None):
        now = _now(now)
        started = time.monotonic()
        token = uuid.uuid4().hex
        until = (_time(now) + timedelta(minutes=5)).isoformat()
        with self._transaction():
            rows = self.conn.execute("SELECT i.id,i.principal,i.run_id,i.company_id,i.payload_json,i.status,r.as_of,r.dry_run,i.lease_until FROM research_update_items i JOIN research_update_runs r USING(principal,run_id) WHERE i.status IN ('queued','running','committed_export_pending') ORDER BY CASE WHEN i.status='committed_export_pending' THEN 1 ELSE 0 END,i.id").fetchall()
            row = next((r for r in rows if not r[8] or _time(r[8]) <= _time(now)), None)
            if row is None:
                return False
            self.conn.execute('UPDATE research_update_items SET lease_token=?,lease_until=?,status=? WHERE id=?', (token,until,'committed_export_pending' if row[5]=='committed_export_pending' else 'running',row[0]))
        if row[5] == 'committed_export_pending':
            self._export(row[0],row[3],token,exporter,now,started)
            return True
        item = json.loads(row[4])
        cid = row[3]
        status, error = None, None
        try:
            if item['result'] == 'UPDATE':
                readiness = _validate(item['document'],now)
                if item['document']['company']['company_id'] != cid or _time(item['document']['as_of']) != _time(row[6]):
                    raise ValueError()
            base = self.store.get_baseline(cid)
            if item['result']=='UPDATE' and base and base['readiness']['fundamental'] and not readiness['fundamental']:
                raise ValueError()
            changed = item['result']=='UPDATE' and (not base or semantic_hash(base['document']) != semantic_hash(item['document']))
            matches = (base['content_hash'] if base else None) == item['expected_base_hash']
            if changed and matches and not row[7] and apply_enabled and before_apply:
                before_apply(cid)
        except Exception:
            status, error = 'failed','validation_or_backup_failed'
        with self._transaction():
            lease = self.conn.execute('SELECT lease_token,lease_until FROM research_update_items WHERE id=?',(row[0],)).fetchone()
            effective_now = _time(now) + timedelta(seconds=time.monotonic() - started)
            if lease[0] != token or _time(lease[1]) <= effective_now:
                return True
            base = self.store.get_baseline(cid)
            old_hash = base['content_hash'] if base else None
            receipt = dict(old_version=base['version'] if base else None,old_hash=old_hash,new_version=base['version'] if base else None,new_hash=old_hash,committed=False,exported=False)
            if status is None:
                if old_hash != item['expected_base_hash']:
                    status,error = 'conflict','base_hash_conflict'
                elif item['result']=='UPDATE' and base and _time(item['document']['as_of']) < _time(base['as_of']):
                    status,error = 'failed','stale_document'
                elif item['result']=='NO_CHANGE' and base is None:
                    status,error = 'incomplete','missing_baseline'
                elif row[7] or not apply_enabled:
                    status = 'validated'
                else:
                    coverage = item['coverage']
                    previous = self.conn.execute('SELECT covered_through FROM research_update_checks WHERE company_id=?',(cid,)).fetchone()
                    water = previous[0] if previous else None
                    anchor = water or (base['as_of'] if base else None)
                    complete = coverage['complete'] and not coverage['gaps'] and bool(coverage['sources']) and all(s['status']=='ok' for s in coverage['sources']) and (anchor is None or _time(coverage['from']) <= _time(anchor))
                    if item['result']=='INCOMPLETE' or not complete:
                        status = 'incomplete'
                    else:
                        status = 'no_change'
                        if item['result']=='UPDATE' and (not base or semantic_hash(base['document']) != semantic_hash(item['document'])):
                            try:
                                old_job = self.conn.execute('SELECT status FROM research_jobs WHERE company_id=?',(cid,)).fetchone()
                                base = self.store.append_baseline(item['document'],now)
                                if old_job and old_job[0] in ('needs_update','review_requested'):
                                    self.conn.execute('UPDATE research_jobs SET status=? WHERE company_id=?',(old_job[0],cid))
                            except (ValueError, TypeError):
                                status,error = 'failed','invalid_document'
                            else:
                                status = 'committed_export_pending'
                                receipt.update(new_version=base['version'],new_hash=base['content_hash'],committed=True)
                        if status != 'failed':
                            if water is None or _time(coverage['through']) > _time(water):
                                water = coverage['through']
                    if status != 'failed':
                        updated = now if receipt['committed'] else None
                        gaps = coverage['gaps'] or ([] if complete else ['coverage_incomplete'])
                        self.conn.execute('INSERT INTO research_update_checks VALUES (?,?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET last_checked=excluded.last_checked,last_updated=COALESCE(excluded.last_updated,research_update_checks.last_updated),covered_through=excluded.covered_through,gaps_json=excluded.gaps_json',(cid,now,updated,water,_json(gaps)))
                        receipt['covered_through'] = water
            self.conn.execute('UPDATE research_update_items SET status=?,error_code=?,receipt_json=?,lease_until=? WHERE id=? AND lease_token=?',(status,error,_json(receipt),until if status=='committed_export_pending' else None,row[0],token))
        if status == 'committed_export_pending':
            self._export(row[0],cid,token,exporter,now,started)
        return True

    def _export(self, item_id, cid, token, exporter, now, started):
        effective_now = _time(now) + timedelta(seconds=time.monotonic()-started)
        with self._transaction(False):
            lease = self.conn.execute('SELECT lease_token,lease_until FROM research_update_items WHERE id=?',(item_id,)).fetchone()
            if not lease or lease[0] != token or not lease[1] or _time(lease[1]) <= effective_now:
                return
        if exporter is None:
            with self._transaction():
                retry_at = (effective_now + timedelta(seconds=60)).isoformat()
                self.conn.execute('UPDATE research_update_items SET lease_until=? WHERE id=? AND lease_token=?',(retry_at,item_id,token))
            return
        try:
            exporter(cid)
        except Exception:
            with self._transaction():
                retry_at = (_time(now) + timedelta(seconds=60)).isoformat()
                self.conn.execute("UPDATE research_update_items SET error_code='export_failed',lease_until=? WHERE id=? AND lease_token=?",(retry_at,item_id,token))
            return
        with self._transaction():
            row = self.conn.execute('SELECT receipt_json FROM research_update_items WHERE id=? AND lease_token=?',(item_id,token)).fetchone()
            if row:
                receipt = json.loads(row[0])
                receipt['exported'] = True
                self.conn.execute("UPDATE research_update_items SET status='completed',error_code=NULL,receipt_json=?,lease_until=NULL WHERE id=? AND lease_token=?",(_json(receipt),item_id,token))
