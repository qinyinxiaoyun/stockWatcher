"""Authenticated, bounded research REST transport; processing belongs to worker."""
from __future__ import annotations
import hmac
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs


def strict_json(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    def invalid(value):
        raise ValueError()
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 32 or nodes > 100000:
            raise ValueError()
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError()
        elif isinstance(item, str):
            item.encode('utf-8')
    if not isinstance(value, dict):
        raise ValueError()
    return value


class ResearchHTTP:
    def __init__(self, settings, application):
        self.settings = settings
        self.application = application

    def __call__(self, environ, start_response):
        def reply(status, payload):
            return self.application._json(start_response, status, payload)
        def error(status, code):
            return reply(status, {'code': 1, 'error': code})
        s = self.settings
        if not s.research_api_enabled:
            return error(404, 'not_found')
        if len(s.research_api_token) < 32:
            return error(503, 'research_not_configured')
        try:
            actual = str(environ.get('HTTP_AUTHORIZATION', '')).encode('utf-8')
        except UnicodeError:
            actual = b''
        if not hmac.compare_digest(actual, ('Bearer ' + s.research_api_token).encode('utf-8')):
            return error(401, 'unauthorized')
        path = str(environ.get('PATH_INFO', ''))
        method = environ.get('REQUEST_METHOD')
        try:
            if len(str(environ.get('QUERY_STRING', ''))) > 2048:
                raise ValueError()
            query = parse_qs(str(environ.get('QUERY_STRING', '')), keep_blank_values=True, strict_parsing=True, max_num_fields=4)
            if any(len(v) != 1 for v in query.values()):
                raise ValueError()
            payload = None
            if path == '/api/research/v1/updates':
                if method != 'POST':
                    return error(405, 'method_not_allowed')
                if query:
                    raise ValueError()
                if str(environ.get('CONTENT_TYPE', '')).split(';')[0].strip().lower() != 'application/json':
                    return error(415, 'use_application_json')
                payload = strict_json(self.application._read_body(environ, s.research_max_request_bytes))
            elif path == '/api/research/v1/snapshot':
                if method != 'GET':
                    return error(405, 'method_not_allowed')
                if set(query) - {'company_ids', 'full'} or query.get('full', ['false'])[0] not in {'true', 'false'}:
                    raise ValueError()
                companies = query['company_ids'][0].split(',') if 'company_ids' in query else None
                if companies is not None and (len(companies) != len(set(companies)) or not set(companies) <= set(s.research_allowed_companies)):
                    raise ValueError()
            elif re.fullmatch(r'/api/research/v1/runs/[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}', path):
                if method != 'GET':
                    return error(405, 'method_not_allowed')
                if query:
                    raise ValueError()
            else:
                return error(404, 'not_found')
        except OverflowError:
            return error(413, 'request_too_large')
        except Exception:
            return error(400, 'invalid_request')
        from .db import connect
        from .research_updates import ResearchUpdates, ResearchUpdateError
        conn = None
        try:
            conn = connect(s.database_path)
            service = ResearchUpdates(conn, allowed_companies=s.research_allowed_companies)
            if payload is not None:
                result = service.submit(s.research_api_principal, payload, apply_enabled=s.research_apply_enabled, now=datetime.now(timezone.utc).isoformat())
                return reply(202, result)
            if path.endswith('/snapshot'):
                return reply(200, service.snapshot(company_ids=companies, full=query.get('full', ['false'])[0] == 'true'))
            return reply(200, service.get_status(s.research_api_principal, path.rsplit('/', 1)[1]))
        except ResearchUpdateError as exc:
            return error(exc.status, exc.code)
        except Exception:
            return error(503, 'research_unavailable')
        finally:
            if conn is not None:
                conn.close()
