"""Optional OAuth resource server. No scheduler, source fetcher or token issuer."""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .settings import Settings


def _https(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('research_mcp_https_configuration_required')
    return value


def _local_json(path: Path, data_dir: Path) -> dict:
    """Local, bounded, reloadable trust material; never follow JWT-provided URLs."""
    absolute = path.absolute()
    if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)()
           for p in (absolute, *absolute.parents)):
        raise ValueError('research_mcp_unsafe_configuration_path')
    if not absolute.resolve().is_relative_to(data_dir.resolve()):
        raise ValueError('research_mcp_configuration_outside_data')
    with absolute.open('rb') as stream:
        raw = stream.read(131073)
    if len(raw) > 131072:
        raise ValueError('research_mcp_configuration_too_large')
    from .research_http import strict_json
    return strict_json(raw)


@dataclass(frozen=True)
class MCPSettings:
    issuer: str
    resource: str
    jwks_file: Path
    policy_file: Path

    def __post_init__(self):
        _https(self.issuer)
        if urlsplit(_https(self.resource)).path != '/mcp/research':
            raise ValueError('research_mcp_resource_path_required')

    @classmethod
    def from_env(cls):
        if os.getenv('RESEARCH_MCP_ENABLED', 'false').lower() != 'true':
            raise ValueError('research_mcp_disabled')
        try:
            return cls(os.environ['RESEARCH_MCP_ISSUER'], os.environ['RESEARCH_MCP_RESOURCE'],
                       Path(os.environ['RESEARCH_MCP_JWKS_FILE']),
                       Path(os.environ['RESEARCH_MCP_POLICY_FILE']))
        except KeyError:
            raise ValueError('research_mcp_not_configured') from None


class ResearchTokenVerifier:
    def __init__(self, settings: Settings, config: MCPSettings):
        self.settings, self.config = settings, config

    async def verify_token(self, token: str):
        import jwt
        from mcp.server.auth.provider import AccessToken
        try:
            if len(token) > 16384:
                return None
            policy = _local_json(self.config.policy_file, self.settings.data_dir)
            if set(policy) - {'subjects', 'revoked_jtis'}:
                return None
            subjects = policy.get('subjects')
            revoked = policy.get('revoked_jtis', [])
            if (not isinstance(subjects, list) or not subjects or len(subjects) > 100
                    or not all(isinstance(s, str) and 0 < len(s) <= 256 for s in subjects)
                    or not isinstance(revoked, list) or len(revoked) > 10000
                    or not all(isinstance(s, str) for s in revoked)):
                return None
            header = jwt.get_unverified_header(token)
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str):
                return None
            keys = _local_json(self.config.jwks_file, self.settings.data_dir).get('keys')
            if not isinstance(keys, list) or len(keys) > 20:
                return None
            matches = [k for k in keys if isinstance(k, dict) and k.get('kid') == header['kid']]
            if len(matches) != 1:
                return None
            jwk = matches[0]
            operations = jwk.get('key_ops', ['verify'])
            if (jwk.get('kty') != 'RSA' or jwk.get('use', 'sig') != 'sig'
                    or jwk.get('alg', 'RS256') != 'RS256'
                    or not isinstance(operations, list) or 'verify' not in operations
                    or not all(isinstance(op, str) for op in operations) or 'd' in jwk):
                return None
            key = jwt.PyJWK.from_dict(jwk, algorithm='RS256').key
            if key.key_size < 2048:
                return None
            claims = jwt.decode(token, key, algorithms=['RS256'], issuer=self.config.issuer,
                                audience=self.config.resource,
                                options={'require': ['exp', 'iat', 'iss', 'aud', 'sub']})
            if (any(type(claims[k]) not in (int, float) for k in ('exp', 'iat'))
                    or not all(math.isfinite(claims[k]) for k in ('exp', 'iat'))
                    or not 0 < claims['exp'] - claims['iat'] <= 3600
                    or claims['sub'] not in subjects or claims.get('jti') in revoked):
                return None
            scopes = claims.get('scope', '')
            if not isinstance(scopes, str) or len(scopes) > 2048:
                return None
            identity = hashlib.sha256((self.config.issuer + '\0' + claims['sub']).encode()).hexdigest()
            return AccessToken(token=token, client_id='research-oauth', subject='oauth-' + identity,
                               scopes=scopes.split(), expires_at=int(claims['exp']),
                               resource=self.config.resource)
        except (ValueError, TypeError, KeyError, OSError, OverflowError,
                AttributeError, RecursionError, jwt.PyJWTError):
            return None


class StrictMCPRequests:
    """Validate JSON before SDK coercion, after authentication and bounded reading."""
    def __init__(self, app, verifier, maximum):
        self.app, self.verifier, self.maximum = app, verifier, maximum

    async def __call__(self, scope, receive, send):
        if (scope['type'] == 'http' and scope['method'] == 'GET'
                and scope['path'] == '/.well-known/oauth-protected-resource/mcp/research'):
            from starlette.responses import JSONResponse
            config = self.verifier.config
            return await JSONResponse({
                'resource': config.resource, 'authorization_servers': [config.issuer],
                'scopes_supported': ['research:read', 'research:write'],
                'bearer_methods_supported': ['header'],
            })(scope, receive, send)
        if (scope['type'] == 'http' and scope['path'] == '/mcp/research'
                and scope['method'] == 'POST'):
            from starlette.datastructures import Headers
            from starlette.responses import JSONResponse
            from .research_http import strict_json
            authorization = Headers(scope=scope).get('authorization', '')
            scheme, _, credential = authorization.partition(' ')
            access = await self.verifier.verify_token(credential) if scheme.lower() == 'bearer' else None
            # Leave unauthenticated challenges to the SDK without consuming a body.
            if access and 'research:read' in access.scopes:
                chunks, length = [], 0
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    chunk = message.get('body', b'')
                    length += len(chunk)
                    if length > self.maximum:
                        return await JSONResponse({'error': 'request_too_large'}, status_code=413)(scope, receive, send)
                    chunks.append(chunk)
                    if not message.get('more_body', False):
                        break
                raw = b''.join(chunks)
                try:
                    strict_json(raw)
                except (ValueError, TypeError, RecursionError, UnicodeError):
                    return await JSONResponse({'error': 'invalid_json'}, status_code=400)(scope, receive, send)
                delivered = False
                original_receive = receive
                async def replay():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {'type': 'http.request', 'body': raw, 'more_body': False}
                    return await original_receive()
                receive = replay
        await self.app(scope, receive, send)


def create_mcp_app(settings: Settings | None = None, config: MCPSettings | None = None):
    """Build only on explicit entry, requiring configured external OAuth trust."""
    from mcp.server import MCPServer
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.auth.middleware.auth_context import get_access_token
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import ToolAnnotations, CallToolResult, TextContent
    from starlette.concurrency import run_in_threadpool
    from .db import connect
    from .research_updates import ResearchUpdates, ResearchUpdateError

    settings = settings or Settings.from_env()
    config = config or MCPSettings.from_env()
    if not settings.research_api_enabled:
        raise ValueError('research_api_disabled')
    # Validate files at startup; read them again per request for rotation/revocation.
    _local_json(config.jwks_file, settings.data_dir)
    _local_json(config.policy_file, settings.data_dir)
    server = MCPServer('Stock Watch Research', token_verifier=ResearchTokenVerifier(settings, config),
                       auth=AuthSettings(issuer_url=config.issuer, resource_server_url=config.resource,
                                         required_scopes=['research:read'], validate_token_resource=True))

    async def invoke(method, *args, write=False, **kwargs):
        token = get_access_token()
        if token is None or not token.subject or (write and 'research:write' not in token.scopes):
            scopes = 'research:read research:write' if write else 'research:read'
            origin = 'https://' + urlsplit(config.resource).netloc
            challenge = (f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource/mcp/research", '
                         'error="insufficient_scope", error_description="Additional research authorization required", '
                         f'scope="{scopes}"')
            return CallToolResult(is_error=True,
                                  content=[TextContent(type='text', text='Additional research authorization required.')],
                                  meta={'mcp/www_authenticate': [challenge]})
        def perform():
            connection = None
            try:
                connection = connect(settings.database_path)
                store = ResearchUpdates(connection, settings.research_allowed_companies)
                if method == 'submit':
                    from datetime import datetime, timezone
                    return store.submit(token.subject, *args, apply_enabled=settings.research_apply_enabled,
                                        now=datetime.now(timezone.utc).isoformat())
                if method == 'get_status':
                    return store.get_status(token.subject, *args)
                return store.snapshot(*args, **kwargs)
            except ResearchUpdateError as exc:
                return {'error': exc.code}
            except Exception:
                return {'error': 'research_unavailable'}
            finally:
                if connection is not None:
                    connection.close()
        return await run_in_threadpool(perform)

    @server.tool(meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['research:read']}]},
                 annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def research_get_snapshot(company_ids: list[str] | None = None, full: bool = False) -> dict:
        """Read current public research, contract and coverage; request full documents only as needed."""
        return await invoke('snapshot', company_ids, full=full)

    @server.tool(meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['research:read', 'research:write']}]},
                 annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                           idempotentHint=True, openWorldHint=False))
    async def research_submit_update(payload: dict) -> dict:
        """Queue UPDATE/NO_CHANGE/INCOMPLETE research. Requires research:write. Query receipt afterwards."""
        return await invoke('submit', payload, write=True)

    @server.tool(meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['research:read']}]},
                 annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def research_get_run_status(run_id: str) -> dict:
        """Read this authenticated identity's receipt; accepted submissions are not completed updates."""
        return await invoke('get_status', run_id)

    origin = 'https://' + urlsplit(config.resource).netloc
    app = server.streamable_http_app(streamable_http_path='/mcp/research', stateless_http=True,
                                     json_response=True, max_request_body_size=settings.research_max_request_bytes,
                                     transport_security=TransportSecuritySettings(
                                         allowed_hosts=[urlsplit(config.resource).netloc], allowed_origins=[origin]))
    return StrictMCPRequests(app, ResearchTokenVerifier(settings, config), settings.research_max_request_bytes)


def main():
    import uvicorn
    uvicorn.run(create_mcp_app(), host='0.0.0.0', port=8001, access_log=False)


if __name__ == '__main__':
    main()
