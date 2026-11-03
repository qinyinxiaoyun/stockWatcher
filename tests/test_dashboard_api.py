"""Browser portal contract, authentication boundaries, and existing API routes."""
from dataclasses import replace
import io
import json
import re
from urllib.parse import urlencode

import pytest

from stock_watch.api import create_app
from stock_watch.db import connect, init_db
from stock_watch.settings import Settings
from stock_watch.subscriptions import MemberConfigStore, MemberRegistry


@pytest.fixture
def portal(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'portal.db',
        wecom_corp_id='synthetic-org', dashboard_username='operator',
        dashboard_password='synthetic-password-for-tests', dashboard_session_secret='synthetic-session-key-'*3,
        dashboard_origin='https://portal.example')
    db = connect(settings.database_path)
    init_db(db)
    registry = MemberRegistry(db, settings.wecom_corp_id)
    member = registry.add('synthetic-private-member')
    registry.assign_legacy_owner(member.user_id)
    db.close()
    return create_app(settings), settings


def request(app, path='/', method='GET', fields=None, cookie='', origin='https://portal.example', query='', body=None, content_type='application/x-www-form-urlencoded'):
    raw = urlencode(fields or {}).encode() if body is None else body
    env = {'PATH_INFO':path,'REQUEST_METHOD':method,'QUERY_STRING':query,'CONTENT_TYPE':content_type,
           'CONTENT_LENGTH':str(len(raw)),'wsgi.input':io.BytesIO(raw),'HTTP_COOKIE':cookie,
           'HTTP_ORIGIN':origin,'REMOTE_ADDR':'127.0.0.1','wsgi.url_scheme':'https'}
    result = {}
    def start(status, headers):
        result.update(status=int(status.split()[0]), headers=headers)
    result['body'] = b''.join(app(env,start)).decode('utf-8')
    return result


def form_csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"',response['body']).group(1)


def cookies(response):
    return '; '.join(value.split(';',1)[0] for key,value in response['headers'] if key=='Set-Cookie' and 'Max-Age=0' not in value)


def login(app, next_path='/'):
    page = request(app)
    return request(app,'/login','POST',fields={'username':'operator','password':'synthetic-password-for-tests',
        'csrf':form_csrf(page),'next':next_path},cookie=cookies(page))


def test_public_page_never_reads_or_discloses_private_data(portal):
    app, settings = portal
    settings.database_path.unlink()
    for path in ('/','/stocks/603986.SH'):
        page = request(app,path)
        assert page['status']==200 and '登录研究工作台' in page['body']
        assert 'synthetic-private-member' not in page['body']
    assert not settings.database_path.exists()
    assert request(app,'/health')['status']==200


def test_login_follow_stock_and_logout(portal):
    app, _ = portal
    signed = login(app,'/stocks/603986.SH')
    assert signed['status']==303
    assert ('Location','/stocks/603986.SH') in signed['headers']
    session = cookies(signed)
    page = request(app,cookie=session)
    assert page['status']==200 and 'synthetic-private-member' in page['body']
    detail = request(app,'/stocks/603986.SH',cookie=session)
    assert detail['status']==200 and '研究尚待建立' in detail['body']
    assert request(app,'/stocks/unknown',cookie=session)['status']==404
    assert request(app,'/stocks/603986.SH',cookie=session,query='version=1')['status']==404
    assert request(app,'/logout',cookie=session)['status']==405
    out = request(app,'/logout','POST',cookie=session,fields={'csrf':form_csrf(page)})
    assert out['status']==303
    assert any('Max-Age=0' in value for key,value in out['headers'] if key=='Set-Cookie')


def test_security_headers_and_cookie_attributes(portal):
    app, _ = portal
    for page in (request(app),login(app)):
        headers = dict(page['headers'])
        assert 'no-store' in headers['Cache-Control']
        assert headers['X-Frame-Options']=='DENY'
        assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
        assert 'noindex' in headers['X-Robots-Tag']
        for name,value in page['headers']:
            if name=='Set-Cookie':
                assert all(flag in value for flag in ('Secure','HttpOnly','SameSite=Strict','Path=/'))
    css = request(app,'/assets/dashboard.css')
    assert css['status']==200 and dict(css['headers'])['Content-Type'].startswith('text/css')


def test_login_rejects_wrong_csrf_origin_and_password(portal):
    app, _ = portal
    page = request(app)
    fields = {'username':'operator','password':'wrong','csrf':form_csrf(page)}
    assert request(app,'/login','POST',fields=fields,cookie=cookies(page))['status']==401
    fields['password']='synthetic-password-for-tests'
    assert request(app,'/login','POST',fields=fields,cookie=cookies(page),origin='https://evil.example')['status']==403
    fields['csrf']='not-a-token'
    assert request(app,'/login','POST',fields=fields,cookie=cookies(page))['status']==403


@pytest.mark.parametrize('path', ['//evil.example','/\\evil.example','https://evil.example','/stocks/a\r\nLocation:bad'])
def test_no_open_redirect(portal,path):
    app, _ = portal
    result = login(app,path)
    assert ('Location','/') in result['headers']


def test_malformed_search_and_forms_are_bounded(portal):
    app, _ = portal
    session = cookies(login(app))
    for query in ('q=a&q=b','version=1&version=2','q='+'x'*129):
        assert request(app,cookie=session,query=query)['status']==400
    assert request(app,'/login','POST',body=b'username=a&username=b')['status']==400
    assert request(app,'/login','POST',body=b'x'*8193)['status']==400
    assert request(app,'/login','POST',body=b'{}',content_type='application/json')['status']==400
    assert request(app,'/',method='DELETE')['status']==405
    assert request(app,'/.env')['status']==404


def test_unconfigured_dashboard_and_existing_public_apis(portal):
    _, settings = portal
    app = create_app(replace(settings,dashboard_password=''))
    page = request(app)
    assert page['status']==503 and 'synthetic-private-member' not in page['body']
    assert request(app,'/health')['status']==200
    assert request(app,'/ready')['status']==200
    assert request(app,'/push')['status']==405
    assert request(app,'/wecom/callback')['status'] in {400,403,503}


def test_settings_secrets_are_not_in_repr_or_exception(monkeypatch):
    monkeypatch.setenv('DASHBOARD_PASSWORD','super-private-password-value')
    monkeypatch.setenv('DASHBOARD_SESSION_SECRET','super-private-session-value-12345678')
    current = Settings.from_env()
    assert current.dashboard_password and current.dashboard_session_secret
    assert 'super-private' not in repr(current)
