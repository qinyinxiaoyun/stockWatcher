"""Real SDK transport and locally signed synthetic OAuth tokens, fully offline."""
import json
import time
from dataclasses import replace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.testclient import TestClient

from stock_watch.db import connect, init_db
from stock_watch.settings import Settings
from stock_watch.research_mcp import MCPSettings, ResearchTokenVerifier, create_mcp_app


@pytest.fixture
def oauth(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key())) | {'kid': 'test-key', 'alg': 'RS256'}
    jwks = tmp_path / 'jwks.json'
    policy = tmp_path / 'policy.json'
    jwks.write_text(json.dumps({'keys': [jwk]}))
    policy.write_text(json.dumps({'subjects': ['researcher']}))
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'test.db', research_api_enabled=True)
    conn = connect(settings.database_path)
    init_db(conn)
    conn.close()
    config = MCPSettings('https://id.example', 'https://stock.example/mcp/research', jwks, policy)
    def sign(**overrides):
        now = int(time.time())
        return jwt.encode({'iss': config.issuer, 'aud': config.resource, 'sub': 'researcher',
                           'iat': now, 'exp': now+300, 'scope': 'research:read research:write',
                           'jti': 'test-token', **overrides}, key, algorithm='RS256', headers={'kid': 'test-key'})
    return settings, config, sign


def rpc(client, token, method, params=None, rpc_id=1):
    headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    return client.post('/mcp/research', headers=headers,
                       json={'jsonrpc': '2.0', 'id': rpc_id, 'method': method, 'params': params or {}})


def test_real_sdk_auth_discovery_tools_and_call(oauth):
    settings, config, sign = oauth
    with TestClient(create_mcp_app(settings, config), base_url='https://stock.example') as client:
        denied = rpc(client, None, 'tools/list')
        assert denied.status_code == 401
        assert 'resource_metadata=' in denied.headers['www-authenticate']
        metadata = client.get('/.well-known/oauth-protected-resource/mcp/research').json()
        assert metadata['resource'] == config.resource
        assert metadata['authorization_servers'] == [config.issuer]
        assert metadata['scopes_supported'] == ['research:read', 'research:write']
        assert metadata['bearer_methods_supported'] == ['header']
        initialized = rpc(client, sign(), 'initialize', {'protocolVersion': '2025-11-25',
                          'capabilities': {}, 'clientInfo': {'name': 'offline', 'version': '1'}})
        assert initialized.status_code == 200
        tools = rpc(client, sign(), 'tools/list').json()['result']['tools']
        assert {t['name'] for t in tools} == {'research_get_snapshot', 'research_submit_update', 'research_get_run_status'}
        assert next(t for t in tools if t['name'] == 'research_submit_update')['annotations']['readOnlyHint'] is False
        response = rpc(client, sign(), 'tools/call', {'name': 'research_get_snapshot', 'arguments': {}})
        assert response.status_code == 200
        result = response.json()['result']
        payload = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        assert len(payload['companies']) == 3
        assert 'document' not in payload['companies'][0]


@pytest.mark.parametrize('claims', [
    {'aud': 'wrong'}, {'iss': 'https://wrong.example'}, {'sub': 'outsider'},
    {'exp': 1}, {'iat': 1}, {'scope': 'research:write'},
])
def test_invalid_identity_audience_expiry_scope_rejected(oauth, claims):
    settings, config, sign = oauth
    with TestClient(create_mcp_app(settings, config), base_url='https://stock.example') as client:
        assert rpc(client, sign(**claims), 'tools/list').status_code in (401, 403)


def test_read_token_cannot_submit_and_revocation_reloads(oauth):
    settings, config, sign = oauth
    with TestClient(create_mcp_app(settings, config), base_url='https://stock.example') as client:
        response = rpc(client, sign(scope='research:read'), 'tools/call',
                       {'name': 'research_submit_update', 'arguments': {'payload': {}}})
        assert response.status_code == 200
        result = response.json()['result']
        assert result['isError'] is True
        challenge = result['_meta']['mcp/www_authenticate'][0]
        for value in ('insufficient_scope', 'error_description=', 'resource_metadata=', 'research:read research:write'):
            assert value in challenge
        tools = rpc(client, sign(scope='research:read'), 'tools/list').json()['result']['tools']
        for tool in tools:
            scopes = tool['_meta']['securitySchemes'][0]['scopes']
            assert scopes == (['research:read', 'research:write'] if tool['name'] == 'research_submit_update' else ['research:read'])
        read = rpc(client, sign(scope='research:read'), 'tools/call',
                   {'name': 'research_get_snapshot', 'arguments': {}})
        assert read.status_code == 200 and not read.json()['result'].get('isError')
        assert rpc(client, 'invalid-token', 'tools/list').status_code == 401
        conn = connect(settings.database_path)
        assert conn.execute('SELECT COUNT(*) FROM research_update_runs').fetchone()[0] == 0
        conn.close()
        config.policy_file.write_text(json.dumps({'subjects': ['researcher'], 'revoked_jtis': ['test-token']}))
        assert rpc(client, sign(), 'tools/list').status_code == 401


def test_bearer_rest_secret_is_not_accepted_for_mcp(oauth):
    settings, config, _ = oauth
    settings = replace(settings, research_api_token='offline-synthetic-rest-secret-32-bytes')
    with TestClient(create_mcp_app(settings, config), base_url='https://stock.example') as client:
        assert rpc(client, settings.research_api_token, 'tools/list').status_code == 401
        assert rpc(client, 'malformed.secret', 'tools/list').status_code == 401


def test_mcp_configuration_fail_closed(oauth):
    settings, config, _ = oauth
    with pytest.raises(ValueError, match='disabled'):
        create_mcp_app(replace(settings, research_api_enabled=False), config)
    with pytest.raises(ValueError):
        replace(config, resource='http://stock.example/mcp/research')
    with pytest.raises(ValueError):
        replace(config, resource='https://stock.example/arbitrary')


def tool_payload(response):
    assert response.status_code == 200, response.text
    result=response.json()['result']
    return result.get('structuredContent') or json.loads(result['content'][0]['text'])


def test_real_mcp_update_roundtrip(oauth):
    from stock_watch.research import SECTIONS
    from stock_watch.research_updates import ResearchUpdates
    settings, config, sign=oauth
    settings=replace(settings,research_apply_enabled=True)
    stamp='2026-11-01T00:00:00+00:00'
    document={'schema_version':1,'company':{'company_id':'gigadevice','name':'GigaDevice'},'securities':[{'symbol':'SH:603986','market':'SH','currency':'CNY'}],'as_of':stamp,'sections':{name:{'statements':[{'kind':'unknown','text':'Not verified','evidence_ids':[]}]} for name in SECTIONS},'sources':[],'assumptions':[],'monitor_mappings':[]}
    payload={'contract_version':1,'run_id':'mcp-roundtrip','as_of':stamp,'items':[{'company_id':'gigadevice','expected_base_hash':None,'result':'UPDATE','coverage':{'from':stamp,'through':stamp,'complete':True,'sources':[{'url':'https://example.org/research','status':'ok'}],'gaps':[]},'summary':'synthetic offline research','document':document}]}
    with TestClient(create_mcp_app(settings,config),base_url='https://stock.example') as client:
        accepted=tool_payload(rpc(client,sign(),'tools/call',{'name':'research_submit_update','arguments':{'payload':payload}}))
        assert accepted['status']=='queued'
        conn=connect(settings.database_path)
        assert ResearchUpdates(conn).process_one(apply_enabled=True,exporter=lambda cid:None)
        conn.close()
        receipt=tool_payload(rpc(client,sign(),'tools/call',{'name':'research_get_run_status','arguments':{'run_id':'mcp-roundtrip'}}))
        assert receipt['items'][0]['status']=='completed'
        assert receipt['items'][0]['committed'] is True
        snap=tool_payload(rpc(client,sign(),'tools/call',{'name':'research_get_snapshot','arguments':{'company_ids':['gigadevice'],'full':True}}))
        assert snap['companies'][0]['document']==document


@pytest.mark.parametrize('raw',[b'{',b'{"jsonrpc":"2.0","jsonrpc":"2.0"}',b'{"x":NaN}',b'{"x":Infinity}',b'{"x":'+b'['*40+b'0'+b']'*40+b'}'])
def test_mcp_strict_json_body(oauth,raw):
    settings,config,sign=oauth
    with TestClient(create_mcp_app(settings,config),base_url='https://stock.example') as client:
        response=client.post('/mcp/research',content=raw,headers={'Authorization':'Bearer '+sign(),'Content-Type':'application/json','Accept':'application/json, text/event-stream'})
        assert response.status_code==400


@pytest.mark.parametrize('claims',[{'exp':float('inf')},{'exp':int(time.time())+7200},{'iat':float('nan')}])
def test_signed_malformed_lifetime_failclosed(oauth,claims):
    settings,config,sign=oauth
    with TestClient(create_mcp_app(settings,config),base_url='https://stock.example') as client:
        assert rpc(client,sign(**claims),'tools/list').status_code==401


@pytest.mark.parametrize('ops',['verify',[],['sign'],None])
def test_bad_jwks_key_ops_failclosed_and_reload(oauth,ops):
    settings,config,sign=oauth
    with TestClient(create_mcp_app(settings,config),base_url='https://stock.example') as client:
        assert rpc(client,sign(),'tools/list').status_code==200
        original=json.loads(config.jwks_file.read_text())
        changed=json.loads(config.jwks_file.read_text())
        changed['keys'][0]['key_ops']=ops
        config.jwks_file.write_text(json.dumps(changed))
        assert rpc(client,sign(),'tools/list').status_code==401
        config.jwks_file.write_text(json.dumps(original))
        assert rpc(client,sign(),'tools/list').status_code==200
