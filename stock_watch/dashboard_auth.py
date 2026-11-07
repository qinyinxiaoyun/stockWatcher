"""Small, bounded, stateless authentication for the private dashboard."""
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

SESSION_COOKIE = 'sw_portal_session'
CSRF_COOKIE = 'sw_portal_csrf'
SESSION_LIFETIME = 8 * 60 * 60
REMEMBERED_SESSION_LIFETIME = 30 * 24 * 60 * 60

class Auth:
    def __init__(self, settings, clock=time.time):
        self.settings, self.clock = settings, clock
        origin = settings.dashboard_origin
        try:
            parsed = urlsplit(origin)
            origin_valid = bool(parsed.scheme == 'https' and parsed.hostname and parsed.netloc and not parsed.username and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment and not any(ord(c) <= 32 or ord(c) == 127 for c in origin))
            parsed.port  # Reject malformed ports.
        except ValueError:
            origin_valid = False
        placeholders = ('CHANGE_ME', 'PLACEHOLDER', 'EXAMPLE')
        credentials_valid = not any(value.upper().startswith(placeholders) for value in (settings.dashboard_username, settings.dashboard_password, settings.dashboard_session_secret))
        self.configured = bool(origin_valid and credentials_valid and settings.dashboard_username and len(settings.dashboard_password) >= 16 and len(settings.dashboard_session_secret) >= 32)
        self.key = hashlib.sha256((settings.dashboard_session_secret + '\0' + settings.dashboard_username + '\0' + settings.dashboard_password).encode()).digest()
        self._failures = {}
        self._lock = threading.Lock()

    def _issue(self, kind, lifetime, binding=''):
        payload = json.dumps([kind, int(self.clock()), lifetime, secrets.token_hex(16), binding], separators=(',', ':')).encode()
        body = base64.urlsafe_b64encode(payload).decode().rstrip('=')
        return body + '.' + hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()

    def _verify(self, token, kind, lifetime, binding=''):
        if not self.configured or not isinstance(token, str) or len(token) > 2048:
            return False
        try:
            body, sig = token.split('.')
            if not hmac.compare_digest(sig, hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()):
                return False
            data = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
            return (isinstance(data, list) and len(data) == 5 and data[0] == kind and type(data[1]) is int and data[2] == lifetime and data[4] == binding and data[1] <= self.clock() < data[1] + lifetime)
        except (ValueError, TypeError, UnicodeError):
            return False

    @staticmethod
    def _cookie(environ, name):
        raw = environ.get('HTTP_COOKIE', '')
        if len(raw) > 8192:
            return None
        parts = raw.split(';')
        if sum(p.strip().split('=', 1)[0] == name for p in parts) != 1:
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(raw)
            return cookies[name].value if name in cookies else None
        except Exception:
            return None

    def same_origin(self, environ):
        return environ.get('HTTP_ORIGIN') == self.settings.dashboard_origin.rstrip('/')

    def issue_login_csrf(self, environ=None):
        existing = self._cookie(environ, CSRF_COOKIE) if environ is not None else None
        if existing and self._verify(existing, 'login', 600):
            # Opening another tab must not invalidate a still-live form.
            return existing, existing
        token = self._issue('login', 600)
        return token, token

    def validate_login_csrf(self, environ, formtoken):
        cookie = self._cookie(environ, CSRF_COOKIE)
        return bool(self.same_origin(environ) and isinstance(formtoken, str) and cookie and hmac.compare_digest(cookie, formtoken) and self._verify(formtoken, 'login', 600))

    def login(self, username, password, remote_addr, *, remember=False):
        if not self.configured or not isinstance(username, str) or not isinstance(password, str) or len(username) > 256 or len(password) > 4096:
            return None
        ip = str(remote_addr)[:256]
        now = self.clock()
        with self._lock:
            self._failures = {k: v for k, v in self._failures.items() if now - v[0] < 600}
            start, count = self._failures.get(ip, (now, 0))
            if count >= 8:
                return None
            valid = hmac.compare_digest(username.encode(), self.settings.dashboard_username.encode()) & hmac.compare_digest(password.encode(), self.settings.dashboard_password.encode())
            if valid:
                self._failures.pop(ip, None)
                lifetime = REMEMBERED_SESSION_LIFETIME if remember is True else SESSION_LIFETIME
                return self._issue('session', lifetime)
            if len(self._failures) >= 4096 and ip not in self._failures:
                self._failures.pop(next(iter(self._failures)))
            self._failures[ip] = (start, count + 1)
            return None

    def session_lifetime(self, cookie):
        # The signed payload must match a server-approved lifetime. Keep
        # existing eight-hour sessions valid across this release.
        for lifetime in (SESSION_LIFETIME, REMEMBERED_SESSION_LIFETIME):
            if self._verify(cookie, 'session', lifetime):
                return lifetime
        return None

    def valid_session(self, cookie):
        return self.session_lifetime(cookie) is not None

    def session_from_environ(self, environ):
        token = self._cookie(environ, SESSION_COOKIE)
        return token if self.valid_session(token) else None

    def issue_logout_csrf(self, session):
        lifetime = self.session_lifetime(session)
        if lifetime is None:
            raise ValueError('invalid_session')
        return self._issue('logout', lifetime, hashlib.sha256(session.encode()).hexdigest())

    def validate_logout_csrf(self, environ, formtoken):
        session = self._cookie(environ, SESSION_COOKIE)
        lifetime = self.session_lifetime(session)
        return bool(lifetime and self.same_origin(environ) and self._verify(formtoken, 'logout', lifetime, hashlib.sha256(session.encode()).hexdigest()))
