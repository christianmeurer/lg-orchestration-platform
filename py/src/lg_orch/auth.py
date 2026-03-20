from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from jose import ExpiredSignatureError, JWTError, jwt
from jose.exceptions import JWKError

# ---------------------------------------------------------------------------
# Internal exception
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Raised when JWT verification or role-checking fails."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JWTSettings:
    """JWT verification settings loaded from environment variables."""

    jwt_secret: str | None  # HS256 shared secret (JWT_SECRET)
    jwks_url: str | None  # RS256 JWKS endpoint URL (JWKS_URL)

    @property
    def enabled(self) -> bool:
        return bool(self.jwt_secret or self.jwks_url)

    @classmethod
    def from_env(cls) -> JWTSettings:
        secret = os.environ.get("JWT_SECRET") or None
        jwks = os.environ.get("JWKS_URL") or None
        return cls(jwt_secret=secret, jwks_url=jwks)


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


@dataclass
class TokenClaims:
    """Decoded, validated JWT payload."""

    sub: str
    roles: list[str]
    exp: int
    iat: int


# ---------------------------------------------------------------------------
# JWKS cache (module-level, simple in-memory)
# ---------------------------------------------------------------------------

_jwks_cache: dict[str, Any] = {}


def _fetch_jwks(url: str) -> dict[str, Any]:
    import json
    import urllib.request

    if url in _jwks_cache:
        return _jwks_cache[url]  # type: ignore[no-any-return]
    with urllib.request.urlopen(url, timeout=10) as resp:
        data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
    _jwks_cache[url] = data
    return data


def _clear_jwks_cache() -> None:
    """Clear the JWKS cache (useful in tests)."""
    _jwks_cache.clear()


# ---------------------------------------------------------------------------
# Core verification
# ---------------------------------------------------------------------------


def verify_token(token: str, settings: JWTSettings) -> TokenClaims:
    """Validate *token* against *settings*.

    Raises :class:`AuthError` with status 401 on any verification failure.
    """
    if not settings.enabled:
        raise AuthError(401, "auth_not_configured")

    try:
        if settings.jwt_secret:
            payload: dict[str, Any] = jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=["HS256"],
            )
        elif settings.jwks_url:
            jwks = _fetch_jwks(settings.jwks_url)
            payload = jwt.decode(
                token,
                jwks,
                algorithms=["RS256"],
            )
        else:
            raise AuthError(401, "auth_not_configured")
    except ExpiredSignatureError:
        raise AuthError(401, "token_expired") from None
    except (JWTError, JWKError) as exc:
        raise AuthError(401, f"invalid_token: {exc}") from exc

    sub = str(payload.get("sub", "")).strip()
    if not sub:
        raise AuthError(401, "missing_sub_claim")

    roles_raw = payload.get("roles", [])
    if isinstance(roles_raw, list):
        roles = [str(r) for r in roles_raw if isinstance(r, str)]
    else:
        roles = []

    exp_raw = payload.get("exp")
    iat_raw = payload.get("iat")
    if not isinstance(exp_raw, (int, float)):
        raise AuthError(401, "missing_exp_claim")
    if not isinstance(iat_raw, (int, float)):
        raise AuthError(401, "missing_iat_claim")

    return TokenClaims(
        sub=sub,
        roles=roles,
        exp=int(exp_raw),
        iat=int(iat_raw),
    )


# ---------------------------------------------------------------------------
# Helpers used by both FastAPI dependencies and the stdlib handler
# ---------------------------------------------------------------------------


def _extract_bearer_token(authorization: str | None) -> str:
    """Extract the raw token string from an ``Authorization: Bearer <token>`` header.

    Raises :class:`AuthError` (401) if the header is absent or malformed.
    """
    if not authorization:
        raise AuthError(401, "missing_authorization_header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthError(401, "invalid_authorization_header")
    return parts[1].strip()


def _check_roles(claims: TokenClaims, required: tuple[str, ...]) -> None:
    """Raise :class:`AuthError` (403) if *claims* does not contain any of *required*."""
    if required and not any(r in claims.roles for r in required):
        raise AuthError(403, f"insufficient_roles: need one of {list(required)}")


# ---------------------------------------------------------------------------
# FastAPI-compatible dependency factories
# ---------------------------------------------------------------------------
# FastAPI is imported lazily so the module can be used without it installed.
# ---------------------------------------------------------------------------


def get_current_user(settings: JWTSettings | None = None) -> Any:
    """Return a FastAPI dependency that validates the Bearer token and returns
    :class:`TokenClaims`.

    Usage::

        @app.get("/protected")
        async def route(claims: TokenClaims = Depends(get_current_user())):
            ...

    If *settings* is ``None`` the dependency reads JWT_SECRET / JWKS_URL from
    environment at call time.
    """
    from fastapi import Depends, HTTPException
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    bearer_scheme = HTTPBearer(auto_error=False)

    async def _dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
        _settings: JWTSettings = Depends(lambda: settings or JWTSettings.from_env()),
    ) -> TokenClaims:
        if not _settings.enabled:
            return TokenClaims(sub="anonymous", roles=[], exp=0, iat=0)
        if credentials is None:
            raise HTTPException(status_code=401, detail="missing_authorization_header")
        try:
            return verify_token(credentials.credentials, _settings)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return _dependency


def require_roles(*roles: str, settings: JWTSettings | None = None) -> Any:
    """Return a FastAPI dependency that enforces that the caller holds at least
    one of the specified *roles*.

    Usage::

        @app.post("/runs")
        async def create_run(
            claims: TokenClaims = Depends(require_roles("operator", "admin"))
        ):
            ...

    If *settings* is ``None`` the dependency reads JWT_SECRET / JWKS_URL from
    environment at call time.  When auth is disabled (neither env var is set)
    the dependency passes every request through without a role check.
    """
    from fastapi import Depends, HTTPException
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    required: tuple[str, ...] = roles
    bearer_scheme = HTTPBearer(auto_error=False)

    async def _dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
        _settings: JWTSettings = Depends(lambda: settings or JWTSettings.from_env()),
    ) -> TokenClaims:
        if not _settings.enabled:
            return TokenClaims(sub="anonymous", roles=list(required), exp=0, iat=0)
        if credentials is None:
            raise HTTPException(status_code=401, detail="missing_authorization_header")
        try:
            claims = verify_token(credentials.credentials, _settings)
            _check_roles(claims, required)
            return claims
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return _dependency


# ---------------------------------------------------------------------------
# Stdlib integration helper (used by remote_api.py)
# ---------------------------------------------------------------------------


def authorize_stdlib(
    *,
    authorization: str | None,
    settings: JWTSettings,
    required_roles: tuple[str, ...] = (),
) -> TokenClaims:
    """Validate a request inside the stdlib HTTP handler.

    Raises :class:`AuthError` with the appropriate status code so the caller
    can convert it to an HTTP response.  When auth is disabled this is a no-op
    and returns a synthetic anonymous :class:`TokenClaims`.
    """
    if not settings.enabled:
        return TokenClaims(sub="anonymous", roles=list(required_roles), exp=0, iat=0)
    token = _extract_bearer_token(authorization)
    claims = verify_token(token, settings)
    _check_roles(claims, required_roles)
    return claims


# ---------------------------------------------------------------------------
# Route → required-roles mapping for the stdlib remote_api handler
# ---------------------------------------------------------------------------

#: Sentinel — route is always public.
_OPEN: tuple[str, ...] = ()
#: Read-only access.
_READERS: tuple[str, ...] = ("viewer", "operator", "admin")
#: Mutation access.
_OPERATORS: tuple[str, ...] = ("operator", "admin")
#: Admin-only access.
_ADMINS: tuple[str, ...] = ("admin",)


def _route_policy(
    *,
    route: str,
    method: str,
    path_parts: list[str],
    jwt_enabled: bool,
) -> tuple[str, ...]:
    """Return the required-roles tuple for the given request.

    Returns ``_OPEN`` for routes that are always public.
    """
    # Always public
    if route == "/healthz":
        return _OPEN
    if route in {"/", "/ui"}:
        return _OPEN

    # /metrics: public when JWT disabled; admin-only when enabled
    if route == "/metrics":
        return _ADMINS if jwt_enabled else _OPEN

    # SPA static files
    if path_parts and path_parts[0] == "app":
        return _OPEN

    # DELETE /runs/{run_id} (v1 or unprefixed)
    if method == "DELETE":
        if len(path_parts) == 2 and path_parts[0] == "runs":
            return _ADMINS
        if len(path_parts) == 4 and path_parts[:2] == ["v1", "runs"]:
            return _ADMINS

    # POST /v1/runs or POST /runs — create run
    if method == "POST" and route in {"/v1/runs", "/runs", "/runs/"}:
        return _OPERATORS

    # GET /v1/runs or GET /runs — list runs
    if method == "GET" and route in {"/v1/runs", "/runs", "/runs/"}:
        return _READERS

    # GET /runs/search
    if route == "/runs/search" and method == "GET":
        return _READERS

    # GET /v1/runs/{run_id}
    if method == "GET" and len(path_parts) == 3 and path_parts[:2] == ["v1", "runs"]:
        return _READERS

    # GET /runs/{run_id} (unprefixed, no sub-path)
    if method == "GET" and len(path_parts) == 2 and path_parts[0] == "runs":
        return _READERS

    # GET /runs/{run_id}/stream — SPA SSE
    if (
        method == "GET"
        and len(path_parts) == 3
        and path_parts[0] == "runs"
        and path_parts[2] == "stream"
    ):
        return _READERS

    # Logs and cancel (readers)
    if len(path_parts) >= 3 and path_parts[-1] in {"logs", "cancel"}:
        return _READERS

    # approve / reject — operator or admin
    if method == "POST" and path_parts and path_parts[-1] in {"approve", "reject"}:
        return _OPERATORS

    # Everything else: open (healing, vote, approval-policy, …)
    return _OPEN


def jwt_settings_from_config(
    *,
    jwt_secret: str | None,
    jwks_url: str | None,
) -> JWTSettings:
    """Construct :class:`JWTSettings` from config-layer values."""
    return JWTSettings(jwt_secret=jwt_secret, jwks_url=jwks_url)


__all__ = [
    "_ADMINS",
    "_OPEN",
    "_OPERATORS",
    "_READERS",
    "AuthError",
    "JWTSettings",
    "TokenClaims",
    "_clear_jwks_cache",
    "_route_policy",
    "authorize_stdlib",
    "get_current_user",
    "jwt_settings_from_config",
    "require_roles",
    "verify_token",
]
