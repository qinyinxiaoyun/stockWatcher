import base64
import json
from types import SimpleNamespace
from stock_watch.dashboard_auth import Auth


def test_session_expiry_tamper_and_rotation():
    settings = SimpleNamespace(dashboard_username='admin', dashboard_password='a'*16, dashboard_session_secret='b'*32, dashboard_origin='https://example.org')
    now = [1000]
    auth = Auth(settings, lambda: now[0])
    token = auth.login('admin', 'a'*16, 'local')
    assert auth.valid_session(token)
    assert not auth.valid_session(token + 'x')
    csrf, cookie = auth.issue_login_csrf()
    assert not auth.valid_session(csrf)
    env = {'HTTP_ORIGIN': settings.dashboard_origin, 'HTTP_COOKIE': 'sw_portal_csrf='+cookie}
    assert auth.validate_login_csrf(env, csrf)
    env['HTTP_COOKIE'] += '; sw_portal_csrf='+cookie
    assert not auth.validate_login_csrf(env, csrf)
    settings.dashboard_password = 'c'*16
    assert not Auth(settings).valid_session(token)
    now[0] = 999
    assert not auth.valid_session(token)
    now[0] = 29800
    assert not auth.valid_session(token)


def test_rate_limit_expires():
    settings = SimpleNamespace(dashboard_username='admin', dashboard_password='a'*16, dashboard_session_secret='b'*32, dashboard_origin='https://example.org')
    now = [1000]
    auth = Auth(settings, lambda: now[0])
    for _ in range(8):
        assert auth.login('admin', 'wrong', 'local') is None
    assert auth.login('admin', 'a'*16, 'local') is None
    now[0] += 600
    assert auth.login('admin', 'a'*16, 'local')

import pytest

@pytest.fixture
def context():
    settings = SimpleNamespace(dashboard_username='admin', dashboard_password='a'*16, dashboard_session_secret='b'*32, dashboard_origin='https://example.org')
    now = [1000]
    return Auth(settings, lambda: now[0]), settings, now

@pytest.mark.parametrize('field,value', [('dashboard_username',''), ('dashboard_password',''), ('dashboard_password','short'), ('dashboard_session_secret',''), ('dashboard_session_secret','short')])
def test_disabled_configuration(context, field, value):
    _, settings, now = context
    setattr(settings, field, value)
    auth = Auth(settings, lambda: now[0])
    assert not auth.configured
    assert auth.login(settings.dashboard_username, settings.dashboard_password, 'ip') is None
    assert not auth.valid_session(auth._issue('session', 28800))


def test_token_types_and_logout_binding(context):
    auth, settings, _ = context
    session = auth.login('admin', settings.dashboard_password, 'ip')
    other = auth.login('admin', settings.dashboard_password, 'ip')
    token = auth.issue_logout_csrf(session)
    env = {'HTTP_ORIGIN': settings.dashboard_origin, 'HTTP_COOKIE': 'sw_portal_session='+session}
    assert auth.validate_logout_csrf(env, token)
    assert not auth.valid_session(token)
    assert not auth.validate_logout_csrf(env, session)
    env['HTTP_COOKIE'] = 'sw_portal_session='+other
    assert not auth.validate_logout_csrf(env, token)
    assert auth.login('admin', session, 'ip') is None

@pytest.mark.parametrize('origin', [None, '', 'https://evil.org', 'https://example.org.evil.org'])
def test_origin_not_replaced_by_forwarded_headers(context, origin):
    auth, _, _ = context
    token, cookie = auth.issue_login_csrf()
    env = {'HTTP_COOKIE':'sw_portal_csrf='+cookie, 'HTTP_HOST':'example.org', 'HTTP_X_FORWARDED_HOST':'example.org', 'HTTP_X_FORWARDED_FOR':'127.0.0.1'}
    if origin is not None:
        env['HTTP_ORIGIN'] = origin
    assert not auth.validate_login_csrf(env, token)


def test_csrf_mismatch_and_expiry(context):
    auth, settings, now = context
    token, cookie = auth.issue_login_csrf()
    other, _ = auth.issue_login_csrf()
    env = {'HTTP_ORIGIN':settings.dashboard_origin, 'HTTP_COOKIE':'sw_portal_csrf='+cookie}
    assert not auth.validate_login_csrf(env, other)
    now[0] += 600
    assert not auth.validate_login_csrf(env, token)


def test_session_cookie_bounds_and_duplicates(context):
    auth, settings, _ = context
    token = auth.login('admin', settings.dashboard_password, 'ip')
    assert auth.session_from_environ({'HTTP_COOKIE':'sw_portal_session='+token}) == token
    assert auth.session_from_environ({'HTTP_COOKIE':'sw_portal_session='+token+'; sw_portal_session='+token}) is None
    assert auth.session_from_environ({'HTTP_COOKIE':'sw_portal_session='+('x'*9000)}) is None
    assert not auth.valid_session('x'*2049)


def test_success_resets_failure_counter(context):
    auth, settings, _ = context
    for _ in range(7):
        auth.login('admin', 'wrong', 'ip')
    assert auth.login('admin', settings.dashboard_password, 'ip')
    for _ in range(7):
        auth.login('admin', 'wrong', 'ip')
    assert auth.login('admin', settings.dashboard_password, 'ip')


def test_parallel_failures_are_atomic(context):
    from concurrent.futures import ThreadPoolExecutor
    auth, settings, _ = context
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(result is None for result in executor.map(lambda _: auth.login('admin', 'wrong', 'ip'), range(40)))
    assert auth._failures['ip'][1] == 8
    assert auth.login('admin', settings.dashboard_password, 'ip') is None


def test_failure_dictionary_is_bounded_and_other_clients_work(context):
    auth, settings, now = context
    for index in range(4200):
        auth.login('admin', 'wrong', str(index))
    assert len(auth._failures) <= 4096
    assert auth.login('admin', settings.dashboard_password, 'fresh-ip')
    now[0] += 600
    assert auth.login('admin', settings.dashboard_password, '4199')
    assert not auth._failures

@pytest.mark.parametrize('origin', ['', 'http://example.org', 'https://example.org/', 'https://example.org/path', 'https://example.org?q=x', 'https://user@example.org', 'https://example.org\n', 'https://example.org:bad'])
def test_invalid_origin_disables_auth(context, origin):
    _, settings, _ = context
    settings.dashboard_origin = origin
    assert not Auth(settings).configured

@pytest.mark.parametrize('prefix', ['CHANGE_ME', 'PLACEHOLDER', 'EXAMPLE'])
def test_placeholder_credentials_disabled(context, prefix):
    _, settings, _ = context
    settings.dashboard_password = prefix + 'x'*32
    assert not Auth(settings).configured


def test_username_rotation_revokes_sessions(context):
    auth, settings, _ = context
    token = auth.login('admin', settings.dashboard_password, 'ip')
    settings.dashboard_username = 'new-admin'
    assert not Auth(settings).valid_session(token)


def test_idle_twenty_minutes_can_logout(context):
    auth, settings, now = context
    session = auth.login('admin', settings.dashboard_password, 'ip')
    csrf = auth.issue_logout_csrf(session)
    env = {'HTTP_ORIGIN':settings.dashboard_origin, 'HTTP_COOKIE':'sw_portal_session='+session}
    now[0] += 1200
    assert auth.validate_logout_csrf(env, csrf)
    now[0] += 28800
    assert not auth.validate_logout_csrf(env, csrf)


@pytest.mark.parametrize('remember,lifetime', [(False, 28800), (True, 2592000)])
def test_session_duration_is_fixed_at_eight_hours_or_thirty_days(context, remember, lifetime):
    auth, settings, now = context
    issued_at = now[0]
    session = auth.login('admin', settings.dashboard_password, 'ip', remember=remember)
    assert auth.valid_session(session)
    now[0] = issued_at + lifetime - 1
    assert auth.valid_session(session)
    now[0] += 1
    assert not auth.valid_session(session)
    now[0] = issued_at - 1
    assert not auth.valid_session(session)


@pytest.mark.parametrize('lifetime', [600, 86400, 28801, 2592001, 31536000])
def test_even_signed_sessions_cannot_choose_arbitrary_lifetimes(context, lifetime):
    auth, _, _ = context
    assert not auth.valid_session(auth._issue('session', lifetime))


def test_changing_signed_duration_cannot_extend_an_eight_hour_session(context):
    auth, settings, now = context
    session = auth.login('admin', settings.dashboard_password, 'ip')
    body, signature = session.split('.')
    payload = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
    payload[2] = 2592000
    changed = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
    now[0] += 28801
    assert not auth.valid_session(changed + '.' + signature)


@pytest.mark.parametrize('remember', [False, True])
@pytest.mark.parametrize('field,value', [
    ('dashboard_username', 'new-admin'),
    ('dashboard_password', 'new-password-for-tests'),
    ('dashboard_session_secret', 'new-session-key-for-tests-' * 3),
])
def test_credential_rotation_revokes_both_session_durations(context, remember, field, value):
    auth, settings, now = context
    session = auth.login('admin', settings.dashboard_password, 'ip', remember=remember)
    now[0] += 60
    assert auth.valid_session(session)
    setattr(settings, field, value)
    rotated = Auth(settings, lambda: now[0])
    assert not rotated.valid_session(session)
    replacement = rotated.login(settings.dashboard_username, settings.dashboard_password, 'ip', remember=remember)
    assert replacement and rotated.valid_session(replacement)


def test_remembered_session_can_logout_from_original_page_after_eight_hours(context):
    auth, settings, now = context
    issued_at = now[0]
    session = auth.login('admin', settings.dashboard_password, 'ip', remember=True)
    logout = auth.issue_logout_csrf(session)
    env = {'HTTP_ORIGIN': settings.dashboard_origin, 'HTTP_COOKIE': 'sw_portal_session=' + session}
    now[0] = issued_at + 28801
    assert auth.valid_session(session)
    assert auth.validate_logout_csrf(env, logout)
    other = auth.login('admin', settings.dashboard_password, 'ip', remember=True)
    assert not auth.validate_logout_csrf({**env, 'HTTP_COOKIE': 'sw_portal_session=' + other}, logout)
    now[0] = issued_at + 2592000
    assert not auth.validate_logout_csrf(env, logout)
