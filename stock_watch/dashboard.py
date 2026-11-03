"""Private, read-only WSGI portal; no scheduling or external service calls."""
from http import HTTPStatus
import re
from urllib.parse import parse_qs

from .dashboard_auth import Auth, CSRF_COOKIE, SESSION_COOKIE
from .dashboard_data import Unavailable, load_dashboard, load_stock
from .dashboard_views import CSS, render_error, render_login, render_overview, render_stock

_STOCK_PATH = re.compile(r'/stocks/([A-Z0-9_.:-]{1,64})\Z')


def handles(path):
    return path in {'/', '/login', '/logout', '/assets/dashboard.css'} or path.startswith('/stocks/')


class Dashboard:
    def __init__(self, settings):
        self.settings = settings
        self.auth = Auth(settings)

    @staticmethod
    def _response(start_response, status, text='', *, headers=(), css=False):
        body = text.encode('utf-8')
        common = [('Content-Type', 'text/css; charset=utf-8' if css else 'text/html; charset=utf-8'),
                  ('Content-Length', str(len(body))), ('Cache-Control', 'no-store, private'),
                  ('X-Content-Type-Options', 'nosniff'), ('X-Frame-Options', 'DENY'),
                  # Suppress cross-site referrers while allowing native same-
                  # origin form POSTs to retain their Origin header.
                  ('Referrer-Policy', 'same-origin'), ('X-Robots-Tag', 'noindex, nofollow, noarchive'),
                  ('Content-Security-Policy', "default-src 'none'; style-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")]
        start_response(str(status) + ' ' + HTTPStatus(status).phrase, common + list(headers))
        return [body]

    @staticmethod
    def _cookie(name, value, age):
        return ('Set-Cookie', f'{name}={value}; Path=/; Max-Age={age}; HttpOnly; Secure; SameSite=Strict')

    def _login_page(self, start_response, message='', next_path='/', status=200, username='', environ=None):
        csrf, cookie = self.auth.issue_login_csrf(environ)
        return self._response(start_response, status, render_login(message, csrf, next_path, username),
                              headers=[self._cookie(CSRF_COOKIE, cookie, 600)])

    @staticmethod
    def _destination(value):
        if value == '/' or _STOCK_PATH.fullmatch(value):
            return value
        return '/'

    @staticmethod
    def _query(environ):
        raw = str(environ.get('QUERY_STRING', ''))
        if len(raw) > 2048:
            raise ValueError('invalid_query')
        parsed = parse_qs(raw, keep_blank_values=True, max_num_fields=4, encoding='utf-8', errors='strict')
        if any(len(values) != 1 for values in parsed.values()):
            raise ValueError('duplicate_field')
        return {key: values[0] for key, values in parsed.items()}

    @staticmethod
    def _form(environ):
        if str(environ.get('CONTENT_TYPE', '')).split(';', 1)[0].strip().lower() != 'application/x-www-form-urlencoded':
            raise ValueError('unsupported_form')
        length = int(environ.get('CONTENT_LENGTH', '0'))
        if length <= 0 or length > 8192:
            raise ValueError('invalid_form_length')
        raw = environ['wsgi.input'].read(length)
        if not isinstance(raw, bytes) or len(raw) != length:
            raise ValueError('incomplete_form')
        form = parse_qs(raw.decode('utf-8'), keep_blank_values=True, max_num_fields=4, encoding='utf-8', errors='strict')
        if any(len(values) != 1 for values in form.values()) or set(form) - {'username', 'password', 'csrf', 'next'}:
            raise ValueError('invalid_form_fields')
        return {key: values[0] for key, values in form.items()}

    def __call__(self, environ, start_response):
        path = str(environ.get('PATH_INFO', ''))
        method = str(environ.get('REQUEST_METHOD', 'GET'))
        try:
            if path == '/assets/dashboard.css':
                if method != 'GET':
                    return self._response(start_response, 405, render_error('请使用页面中的导航。', 405))
                return self._response(start_response, 200, CSS, css=True)
            if method not in {'GET', 'POST'}:
                return self._response(start_response, 405, render_error('此页面仅支持查看与登录。', 405))
            if not self.auth.configured:
                return self._response(start_response, 503, render_error('管理页面尚未配置访问口令，请联系管理员。'))
            session = self.auth.session_from_environ(environ)
            if path == '/logout':
                if method != 'POST':
                    return self._response(start_response, 405, render_error('请使用页面中的退出按钮。', 405))
                form = self._form(environ)
                if not self.auth.validate_logout_csrf(environ, form.get('csrf', '')):
                    return self._response(start_response, 403, render_error('页面已失效，请刷新后再退出。', 403))
                return self._response(start_response, 303, headers=[('Location', '/login'), self._cookie(SESSION_COOKIE, '', 0), self._cookie(CSRF_COOKIE, '', 0)])
            if path == '/login' and method == 'POST':
                form = self._form(environ)
                destination = self._destination(form.get('next', '/'))
                if not self.auth.validate_login_csrf(environ, form.get('csrf', '')):
                    return self._login_page(start_response, '登录页面已失效，请重新输入。', destination, 403, environ=environ)
                token = self.auth.login(form.get('username', ''), form.get('password', ''), environ.get('REMOTE_ADDR', 'unknown'))
                if token is None:
                    return self._login_page(start_response, '登录未成功，请核对访问凭证，稍后再试。', destination, 401, environ=environ)
                return self._response(start_response, 303, headers=[('Location', destination), self._cookie(SESSION_COOKIE, token, 28800), self._cookie(CSRF_COOKIE, '', 0)])
            if method != 'GET':
                return self._response(start_response, 405, render_error('此页面仅供查看。', 405))
            query = self._query(environ)
            if path == '/login':
                if session:
                    return self._response(start_response, 303, headers=[('Location', '/')])
                return self._login_page(start_response, next_path=self._destination(query.get('next', '/')), environ=environ)
            if not session:
                return self._login_page(start_response, next_path=self._destination(path), environ=environ)
            csrf = self.auth.issue_logout_csrf(session)
            if path == '/':
                q = query.get('q', '')
                if len(q) > 128:
                    raise ValueError('search_too_long')
                view = load_dashboard(self.settings, q)
                view['query'] = q
                return self._response(start_response, 200, render_overview(view, csrf))
            match = _STOCK_PATH.fullmatch(path)
            if match:
                version = query.get('version')
                if version is not None and (not re.fullmatch('[0-9]{1,6}', version) or int(version) < 1):
                    raise ValueError('invalid_version')
                view = load_stock(self.settings, match[1], version)
                if view is None or (version is not None and view['research'].get('baseline') is None):
                    return self._response(start_response, 404, render_error('未找到该关注股票或研究版本。', 404))
                return self._response(start_response, 200, render_stock(view, csrf))
            return self._response(start_response, 404, render_error('未找到此页面。', 404))
        except (ValueError, UnicodeError, KeyError, OverflowError):
            return self._response(start_response, 400, render_error('请求格式不正确，请返回页面重试。', 400))
        except Unavailable:
            return self._response(start_response, 503, render_error('资料暂时无法读取，请稍后刷新。'))
        except Exception:
            # Never interpolate member data, credentials, request bodies or DB errors.
            return self._response(start_response, 503, render_error('页面暂时不可用，请稍后刷新。'))
