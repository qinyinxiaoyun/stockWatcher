"""Offline Fetch/Referrer-Policy contract replay, not a real browser test.

For ordinary non-CORS form POST navigation, no-referrer serializes Origin as
null. See https://fetch.spec.whatwg.org/#append-a-request-origin-header
and https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Referrer-Policy
Only same-origin HTTPS submissions are modeled here.
"""
import pytest
from test_dashboard_api import portal, request, cookies, form_csrf


def navigation_origin(page, source='https://portal.example'):
    policy = dict(page['headers'])['Referrer-Policy']
    if policy == 'no-referrer':
        return 'null'
    if policy == 'same-origin':
        return source
    raise AssertionError('Contract replay does not model this policy')


def submit(app, page, cookie=None, origin_override=...):
    origin = navigation_origin(page) if origin_override is ... else origin_override
    target = app
    if origin is None:
        def target(environ, start_response):
            environ.pop('HTTP_ORIGIN', None)
            return app(environ, start_response)
    return request(target, '/login', 'POST', cookie=cookies(page) if cookie is None else cookie,
                   origin=origin, fields={'username':'operator',
                   'password':'synthetic-password-for-tests', 'csrf':form_csrf(page)})


def test_replay_no_referrer_reproduces_null_origin(portal):
    app, _ = portal
    page = request(app)
    page['headers'] = [(key, 'no-referrer' if key == 'Referrer-Policy' else value)
                       for key, value in page['headers']]
    assert navigation_origin(page) == 'null'
    assert submit(app, page)['status'] == 403


def test_navigation_contract_login_and_logout(portal):
    app, _ = portal
    page = request(app)
    assert dict(page['headers'])['Referrer-Policy'] == 'same-origin'
    signed = submit(app, page)
    assert signed['status'] == 303
    session = cookies(signed)
    private = request(app, cookie=session)
    assert private['status'] == 200
    result = request(app, '/logout', 'POST', cookie=session,
                     origin=navigation_origin(private), fields={'csrf':form_csrf(private)})
    assert result['status'] == 303
    assert any('Max-Age=0' in value for key, value in result['headers'] if key == 'Set-Cookie')


@pytest.mark.parametrize('origin', ['null', None, '', 'https://evil.example'])
def test_navigation_rejects_untrusted_origin_without_mutating_data(portal, origin):
    app, settings = portal
    before = settings.database_path.read_bytes()
    page = request(app)
    assert submit(app, page, origin_override=origin)['status'] == 403
    assert settings.database_path.read_bytes() == before


def test_two_tabs_latest_cookie_can_submit_first_form(portal):
    app, _ = portal
    first = request(app)
    second = request(app, cookie=cookies(first))
    assert form_csrf(second) == form_csrf(first)
    assert submit(app, first, cookie=cookies(second))['status'] == 303


def test_expired_cookie_get_rotates_and_old_submission_is_rejected(portal):
    app, _ = portal
    now = [1000]
    app.dashboard.auth.clock = lambda: now[0]
    old = request(app)
    now[0] += 600
    fresh = request(app, cookie=cookies(old))
    assert form_csrf(fresh) != form_csrf(old)
    assert submit(app, old)['status'] == 403
    assert submit(app, fresh)['status'] == 303


def test_tampered_cookie_get_replaces_and_post_rejects(portal):
    app, settings = portal
    before = settings.database_path.read_bytes()
    page = request(app)
    bad_cookie = cookies(page) + 'x'
    assert submit(app, page, cookie=bad_cookie)['status'] == 403
    replacement = request(app, cookie=bad_cookie)
    assert form_csrf(replacement) != form_csrf(page)
    assert settings.database_path.read_bytes() == before


def test_other_valid_token_cannot_replace_cookie_binding(portal):
    app, settings = portal
    before = settings.database_path.read_bytes()
    first = request(app)
    other = request(app)
    assert submit(app, first, cookie=cookies(other))['status'] == 403
    assert settings.database_path.read_bytes() == before
