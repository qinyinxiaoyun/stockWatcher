import io
import json
from dataclasses import replace
import pytest
from stock_watch.settings import Settings
from stock_watch.api import create_app
from stock_watch.research_http import strict_json

@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"x":NaN}', b'[]', ('{"x":' + '['*40 + '0' + ']'*40 + '}').encode()])
def test_strict_json_rejects(raw):
    with pytest.raises(ValueError):
        strict_json(raw)

def test_auth_precedes_body_and_database(tmp_path):
    settings = Settings(tmp_path, tmp_path/'missing.db', research_api_enabled=True, research_api_token='x'*32)
    class Unreadable:
        def read(self, size):
            raise AssertionError('unauthorized body read')
    result = []
    body = create_app(settings)({'PATH_INFO':'/api/research/v1/updates', 'REQUEST_METHOD':'POST', 'wsgi.input':Unreadable()}, lambda status, headers: result.append((status,headers)))
    assert result[0][0].startswith('401')
    assert not settings.database_path.exists()
    assert ('Cache-Control','no-store') in result[0][1]
    assert b'x'*32 not in b''.join(body)

@pytest.mark.parametrize('enabled,token,status', [(False,'','404'),(True,'','503')])
def test_disabled_and_unconfigured(tmp_path,enabled,token,status):
    settings=Settings(tmp_path,tmp_path/'missing.db',research_api_enabled=enabled,research_api_token=token)
    result=[]
    create_app(settings)({'PATH_INFO':'/api/research/v1/snapshot','REQUEST_METHOD':'GET'},lambda code,headers:result.append(code))
    assert result[0].startswith(status)
    assert not settings.database_path.exists()


def test_settings_guards(tmp_path):
    s=Settings(tmp_path,tmp_path/'db')
    assert s.research_report_dir == tmp_path/'research'/'reports'
    for changes in ({'research_allowed_companies':('other',)}, {'research_api_token':'short'}, {'research_report_dir':tmp_path.parent/'outside'}, {'research_max_request_bytes':0}):
        with pytest.raises(ValueError):
            replace(s,**changes)
    assert 'secret-value'*4 not in repr(replace(s,research_api_token='secret-value'*4))


def test_real_http_intake_process_snapshot_status(tmp_path):
    from stock_watch.db import connect, init_db
    from stock_watch.research import SECTIONS
    from stock_watch.research_updates import ResearchUpdates
    s = Settings(tmp_path,tmp_path/'db', research_api_enabled=True, research_apply_enabled=True,research_api_token='x'*32)
    conn=connect(s.database_path)
    init_db(conn)
    app=create_app(s)
    def request(path, payload=None, settings=None):
        raw=json.dumps(payload).encode() if payload is not None else b''
        route,_,query=path.partition('?')
        environ={'PATH_INFO':route,'QUERY_STRING':query,'REQUEST_METHOD':'POST' if payload is not None else 'GET','HTTP_AUTHORIZATION':'Bearer '+s.research_api_token,'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(raw)),'wsgi.input':io.BytesIO(raw)}
        result=[]
        body=(create_app(settings) if settings else app)(environ,lambda status,headers:result.append(status))
        return int(result[0].split()[0]),json.loads(b''.join(body))
    stamp='2026-11-01T00:00:00+00:00'
    doc={'schema_version':1,'company':{'company_id':'gigadevice','name':'GigaDevice'},'securities':[{'symbol':'SH:603986','market':'SH','currency':'CNY'}], 'as_of':stamp,'sections':{name:{'statements':[{'kind':'unknown','text':'Not verified','evidence_ids':[]}]} for name in SECTIONS},'sources':[],'assumptions':[],'monitor_mappings':[]}
    coverage={'from':stamp,'through':stamp,'complete':True,'sources':[{'url':'https://example.org/report','status':'ok'}],'gaps':[]}
    item={'company_id':'gigadevice','expected_base_hash':None,'result':'UPDATE','coverage':coverage,'summary':'research','document':doc}
    bad={**item,'company_id':'smic','document':{'invalid':True}}
    payload={'contract_version':1,'run_id':'integration.1','as_of':stamp,'items':[item,bad]}
    code,accepted=request('/api/research/v1/updates',payload)
    assert code == 202,accepted
    assert accepted['status'] in {'queued','running'}
    core=ResearchUpdates(conn)
    for _ in range(2):
        assert core.process_one(apply_enabled=True,exporter=lambda cid:None)
    code,status=request('/api/research/v1/runs/integration.1')
    assert code==200
    assert [i['status'] for i in status['items']]==['completed','failed']
    code,snap=request('/api/research/v1/snapshot?company_ids=gigadevice&full=true')
    baseline=snap['companies'][0]
    assert baseline['document']==doc
    assert request('/api/research/v1/runs/integration.1',settings=replace(s,research_api_principal='other'))[0]==404
    nochange={k:v for k,v in item.items() if k!='document'}
    nochange.update(result='NO_CHANGE',expected_base_hash=baseline['hash'])
    payload.update(run_id='integration.2',items=[nochange])
    assert request('/api/research/v1/updates',payload)[0]==202
    core.process_one(apply_enabled=True)
    assert request('/api/research/v1/runs/integration.2')[1]['items'][0]['status']=='no_change'
    payload.update(run_id='integration.3')
    assert request('/api/research/v1/updates',payload,replace(s,research_apply_enabled=False))[0]==202
    core.process_one(apply_enabled=True)
    assert request('/api/research/v1/runs/integration.3')[1]['items'][0]['status']=='validated'
    assert request('/api/research/v1/snapshot?company_ids=gigadevice')[1]['companies'][0]['version']==1
    conn.close()




@pytest.mark.parametrize("principal", ["用户", "-start", "a"*101, "space value"])
def test_research_principal_matches_core(tmp_path, principal):
    with pytest.raises(ValueError):
        Settings(tmp_path, tmp_path/'db', research_api_principal=principal)


def test_research_principal_accepts_core_punctuation(tmp_path):
    Settings(tmp_path, tmp_path/'db', research_api_principal='cloud.research:1-user')
