import copy
import pytest
from stock_watch.db import connect, init_db
from stock_watch.research import SECTIONS, ResearchStore
from stock_watch.research_updates import ResearchUpdates, ResearchUpdateError

OLD='2026-11-04T10:00:00Z'
NOW='2026-11-05T10:00:00Z'

@pytest.fixture
def queue(tmp_path):
    conn=connect(tmp_path/'test.sqlite')
    init_db(conn)
    doc=dict(schema_version=1,company=dict(company_id='gigadevice',name='GigaDevice'),securities=[dict(symbol='603986.SH',market='SH',currency='CNY')],as_of=OLD,sections={s:dict(statements=[dict(kind='unknown',text='Unknown',evidence_ids=[])]) for s in SECTIONS},sources=[],assumptions=[],monitor_mappings=[])
    ResearchStore(conn).append_baseline(doc,OLD)
    return ResearchUpdates(conn)

def payload(q,run='r1',result='UPDATE'):
    base=q.store.get_baseline('gigadevice')
    i=dict(company_id='gigadevice',expected_base_hash=base['content_hash'],result=result,coverage={'from':OLD,'through':NOW,'complete':True,'sources':[dict(url='https://example.com',status='ok')],'gaps':[]},summary='Review')
    if result=='UPDATE':
        i['document']=copy.deepcopy(base['document'])
        i['document']['as_of']=NOW
        i['document']['sections']['risks']['statements'][0]['text']='New risk'
    return dict(contract_version=1,run_id=run,as_of=NOW,items=[i])

def test_idempotence_and_principal(queue):
    p=payload(queue)
    queue.submit('svc',p,now=NOW)
    assert queue.submit('svc',p,now=NOW)['status']=='queued'
    p['items'][0]['summary']='Changed'
    with pytest.raises(ResearchUpdateError) as exc: queue.submit('svc',p,now=NOW)
    assert exc.value.status==409
    with pytest.raises(ResearchUpdateError) as exc: queue.get_status('other','r1')
    assert exc.value.status==404
    queue.submit('other',p,now=NOW)

def test_cas_across_connections(queue):
    p=payload(queue)
    queue.submit('svc',p,now=NOW,apply_enabled=True)
    other=ResearchUpdates(connect(queue.conn.execute('PRAGMA database_list').fetchone()[2]))
    p['run_id']='r2'
    other.submit('svc',p,now=NOW,apply_enabled=True)
    queue.process_one(now=NOW,apply_enabled=True,exporter=lambda c:None)
    other.process_one(now=NOW,apply_enabled=True)
    assert other.get_status('svc','r2')['status']=='conflict'
    assert len(queue.store.list_baselines())==2

def test_export_recovery(queue):
    queue.submit('svc',payload(queue),now=NOW,apply_enabled=True)
    calls=[]
    def fail(c): raise RuntimeError('sensitive error')
    queue.process_one(now=NOW,apply_enabled=True,before_apply=lambda c:calls.append(c),exporter=fail)
    assert queue.get_status('svc','r1')['status']=='committed_export_pending'
    queue.process_one(now='2026-11-05T10:01:01Z',apply_enabled=True,exporter=lambda c:None)
    assert queue.get_status('svc','r1')['status']=='completed'
    assert len(queue.store.list_baselines())==2
    assert calls==['gigadevice']

@pytest.mark.parametrize('submit_on,worker_on',[(False,True),(True,False)])
def test_dryrun(queue,submit_on,worker_on):
    queue.submit('svc',payload(queue),now=NOW,apply_enabled=submit_on)
    queue.process_one(now=NOW,apply_enabled=worker_on)
    assert queue.get_status('svc','r1')['status']=='validated'
    assert len(queue.store.list_baselines())==1

def test_failed_source_and_repair_watermark(queue):
    p=payload(queue,result='NO_CHANGE')
    p['items'][0]['coverage']['sources'][0]['status']='failed'
    queue.submit('svc',p,now=NOW,apply_enabled=True)
    queue.process_one(now=NOW,apply_enabled=True)
    assert queue.snapshot(['gigadevice'])['companies'][0]['covered_through'] is None
    queue.submit('svc',payload(queue,'r2','NO_CHANGE'),now=NOW,apply_enabled=True)
    queue.process_one(now=NOW,apply_enabled=True)
    assert queue.snapshot(['gigadevice'])['companies'][0]['covered_through']==NOW
    assert len(queue.store.list_baselines())==1

def test_time_only_and_expired_lease(queue):
    p=payload(queue)
    p['items'][0]['document']=copy.deepcopy(queue.store.get_baseline('gigadevice')['document'])
    p['items'][0]['document']['as_of']=NOW
    queue.submit('svc',p,now=NOW,apply_enabled=True)
    queue.conn.execute("UPDATE research_update_items SET status='running',lease_token='dead',lease_until=?",(OLD,))
    queue.process_one(now=NOW,apply_enabled=True)
    assert queue.get_status('svc','r1')['status']=='no_change'
    assert len(queue.store.list_baselines())==1

def test_bad_document(queue):
    p=payload(queue)
    p['items'][0]['document']['shell']='unsafe'
    queue.submit('svc',p,now=NOW,apply_enabled=True)
    queue.process_one(now=NOW,apply_enabled=True)
    assert queue.get_status('svc','r1')['status']=='failed'

def test_export_failure_does_not_starve_queued(queue):
    queue.submit('svc',payload(queue),now=NOW,apply_enabled=True)
    def fail(c): raise OSError('private error')
    queue.process_one(now=NOW,apply_enabled=True,exporter=fail)
    queue.submit('other',payload(queue,'r2','NO_CHANGE'),now=NOW,apply_enabled=True)
    queue.process_one(now=NOW,apply_enabled=True,exporter=fail)
    assert queue.get_status('other','r2')['status']=='no_change'
    assert not queue.process_one(now=NOW,apply_enabled=True,exporter=fail)

def test_slow_backup_loses_fence(queue,monkeypatch):
    clock=[0]
    monkeypatch.setattr('stock_watch.research_updates.time.monotonic',lambda:clock[0])
    queue.submit('svc',payload(queue),now=NOW,apply_enabled=True)
    def slow(c):
        assert not queue.conn.in_transaction
        clock[0]=301
    queue.process_one(now=NOW,apply_enabled=True,before_apply=slow)
    assert len(queue.store.list_baselines())==1
    assert queue.get_status('svc','r1')['status']=='running'
    queue.process_one(now='2026-11-05T10:06:00Z',apply_enabled=True,exporter=lambda c:None)
    assert len(queue.store.list_baselines())==2

def test_review_requested_survives_append(queue):
    queue.conn.execute("UPDATE research_jobs SET status='needs_update' WHERE company_id='gigadevice'")
    queue.submit('svc',payload(queue),now=NOW,apply_enabled=True)
    queue.process_one(now=NOW,apply_enabled=True)
    assert queue.store.list_jobs()[0]['status']=='needs_update'

def test_partial_company_failure(queue):
    p=payload(queue)
    p['items'][0]['document']['shell']='unsafe'
    other=copy.deepcopy(p['items'][0])
    other['company_id']='alibaba'
    other['expected_base_hash']=None
    other['document'].pop('shell')
    other['document']['company']=dict(company_id='alibaba',name='Alibaba')
    other['document']['securities']=[dict(symbol='9988.HK',market='HK',currency='HKD')]
    p['items'].append(other)
    queue.submit('svc',p,now=NOW,apply_enabled=True)
    queue.process_one(now=NOW,apply_enabled=True)
    queue.process_one(now=NOW,apply_enabled=True,exporter=lambda c:None)
    assert queue.get_status('svc','r1')['status']=='partial_success'
    assert queue.store.get_baseline('alibaba')

def test_export_keeps_lease_during_callback(queue):
    queue.submit('svc',payload(queue),now=NOW,apply_enabled=True)
    other=ResearchUpdates(connect(queue.conn.execute('PRAGMA database_list').fetchone()[2]))
    def export(cid):
        assert not queue.conn.in_transaction
        assert not other.process_one(now=NOW,apply_enabled=True,exporter=lambda c:None)
    queue.process_one(now=NOW,apply_enabled=True,exporter=export)
    assert queue.get_status('svc','r1')['status']=='completed'

def test_schema_five_upgrade_keeps_history_and_configuration(tmp_path):
    from stock_watch.research import SCHEMA_SQL
    conn=connect(tmp_path/'old.sqlite')
    init_db(conn)
    # Remove only v6 tables to reproduce the actual v5 schema, retain all v5 data.
    for table in ('research_update_items','research_update_runs','research_update_checks'):
        conn.execute('DROP TABLE '+table)
    conn.execute('PRAGMA user_version=5')
    conn.execute("UPDATE app_meta SET value='5' WHERE key='schema_version'")
    conn.execute("INSERT INTO research_companies VALUES ('gigadevice','Original')")
    conn.execute("INSERT INTO research_baselines(company_id,version,as_of,created_at,content_hash,document_json,readiness_json) VALUES ('gigadevice',1,?,?,?,'{}','{}')",(OLD,OLD,'a'*64))
    config=tuple(conn.execute('SELECT * FROM config_state').fetchone())
    history=tuple(conn.execute('SELECT * FROM research_baselines').fetchone())
    init_db(conn)
    assert conn.execute('PRAGMA user_version').fetchone()[0]==6
    assert tuple(conn.execute('SELECT * FROM config_state').fetchone())==config
    assert tuple(conn.execute('SELECT * FROM research_baselines').fetchone())==history
    assert conn.execute('SELECT COUNT(*) FROM research_update_items').fetchone()[0]==0

def test_mcp_payload_depth_and_url_bounds(queue):
    p=payload(queue)
    deep={}
    for _ in range(45): deep={'nested':deep}
    p['items'][0]['document']=deep
    with pytest.raises(ResearchUpdateError): queue.submit('svc',p,now=NOW)
    p=payload(queue)
    p['items'][0]['coverage']['sources'][0]['url']='https://example.com/'+('x'*4096)
    with pytest.raises(ResearchUpdateError): queue.submit('svc',p,now=NOW)

def test_report_paths_and_snapshot_types(queue):
    assert queue.snapshot(['gigadevice'])['companies'][0]['report_path']=='/stocks/603986.SH'
    result=queue.submit('svc',payload(queue),now=NOW)
    assert result['items'][0]['report_path']=='/stocks/603986.SH'
    for ids in ('gigadevice', 7, {}, ['gigadevice','gigadevice'], [None], [['gigadevice']]):
        with pytest.raises(ResearchUpdateError): queue.snapshot(ids)
    for full in (None,1,'true'):
        with pytest.raises(ResearchUpdateError): queue.snapshot(full=full)
    with pytest.raises(ResearchUpdateError): ResearchUpdates(queue.conn,['gigadevice','gigadevice'])
