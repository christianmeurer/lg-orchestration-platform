"""Tests for py/src/lg_orch/auth.py — JWT/RBAC middleware."""
from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from jose import jwt

from lg_orch.auth import (
    AuthError,
    JWTSettings,
    TokenClaims,
    _clear_jwks_cache,
    _route_policy,
    authorize_stdlib,
    get_current_user,
    require_roles,
    verify_token,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRET = "test-hs256-secret-that-is-long-enough"
_ENABLED = JWTSettings(jwt_secret=_SECRET, jwks_url=None)
_DISABLED = JWTSettings(jwt_secret=None, jwks_url=None)


def _make_token(
    *,
    sub: str = "user-1",
    roles: list[str] | None = None,
    exp_offset: int = 3600,
    secret: str = _SECRET,
    algorithm: str = "HS256",
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "roles": roles if roles is not None else ["viewer"],
        "iat": now,
        "exp": now + exp_offset,
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def _expired_token(*, sub: str = "user-expired", roles: list[str] | None = None) -> str:
    return _make_token(sub=sub, roles=roles or ["viewer"], exp_offset=-3600)


# ---------------------------------------------------------------------------
# Minimal FastAPI app for dependency-layer tests
# ---------------------------------------------------------------------------


def _make_app(*, settings: JWTSettings) -> FastAPI:
    """Build a tiny FastAPI app whose routes use the auth dependencies."""
    app = FastAPI()

    @app.get("/open")
    async def open_route() -> dict[str, str]:
        return {"ok": "true"}

    @app.get("/me")
    async def me(claims: TokenClaims = Depends(get_current_user(settings=settings))) -> dict[str, Any]:
        return {"sub": claims.sub, "roles": claims.roles}

    @app.get("/reader")
    async def reader(
        claims: TokenClaims = Depends(require_roles("viewer", "operator", "admin", settings=settings)),
    ) -> dict[str, Any]:
        return {"sub": claims.sub}

    @app.post("/operator")
    async def operator_route(
        claims: TokenClaims = Depends(require_roles("operator", "admin", settings=settings)),
    ) -> dict[str, Any]:
        return {"sub": claims.sub}

    @app.delete("/admin-only")
    async def admin_only(
        claims: TokenClaims = Depends(require_roles("admin", settings=settings)),
    ) -> dict[str, Any]:
        return {"sub": claims.sub}

    return app


# ---------------------------------------------------------------------------
# verify_token unit tests
# ---------------------------------------------------------------------------


class TestVerifyToken:
    def test_valid_hs256(self) -> None:
        token = _make_token(sub="alice", roles=["operator"])
        claims = verify_token(token, _ENABLED)
        assert claims.sub == "alice"
        assert "operator" in claims.roles

    def test_expired_raises_401(self) -> None:
        token = _expired_token()
        with pytest.raises(AuthError) as exc_info:
            verify_token(token, _ENABLED)
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail

    def test_invalid_signature_raises_401(self) -> None:
        token = _make_token(secret="wrong-secret")
        with pytest.raises(AuthError) as exc_info:
            verify_token(token, _ENABLED)
        assert exc_info.value.status_code == 401

    def test_auth_disabled_raises_401(self) -> None:
        token = _make_token()
        with pytest.raises(AuthError) as exc_info:
            verify_token(token, _DISABLED)
        assert exc_info.value.status_code == 401
        assert "auth_not_configured" in exc_info.value.detail

    def test_missing_sub_raises_401(self) -> None:
        now = int(time.time())
        payload: dict[str, Any] = {"iat": now, "exp": now + 3600, "roles": []}
        token = jwt.encode(payload, _SECRET, algorithm="HS256")
        with pytest.raises(AuthError) as exc_info:
            verify_token(token, _ENABLED)
        assert exc_info.value.status_code == 401
        assert "sub" in exc_info.value.detail

    def test_roles_defaults_to_empty_list(self) -> None:
        now = int(time.time())
        payload: dict[str, Any] = {"sub": "noone", "iat": now, "exp": now + 3600}
        token = jwt.encode(payload, _SECRET, algorithm="HS256")
        claims = verify_token(token, _ENABLED)
        assert claims.roles == []


# ---------------------------------------------------------------------------
# authorize_stdlib unit tests
# ---------------------------------------------------------------------------


class TestAuthorizeStdlib:
    def test_valid_token_no_role_check(self) -> None:
        token = _make_token(roles=["viewer"])
        auth_header = f"Bearer {token}"
        claims = authorize_stdlib(authorization=auth_header, settings=_ENABLED)
        assert claims.sub == "user-1"

    def test_missing_header_raises_401(self) -> None:
        with pytest.raises(AuthError) as exc_info:
            authorize_stdlib(authorization=None, settings=_ENABLED)
        assert exc_info.value.status_code == 401

    def test_wrong_role_raises_403(self) -> None:
        token = _make_token(roles=["viewer"])
        auth_header = f"Bearer {token}"
        with pytest.raises(AuthError) as exc_info:
            authorize_stdlib(
                authorization=auth_header,
                settings=_ENABLED,
                required_roles=("admin",),
            )
        assert exc_info.value.status_code == 403

    def test_correct_role_passes(self) -> None:
        token = _make_token(roles=["admin"])
        auth_header = f"Bearer {token}"
        claims = authorize_stdlib(
            authorization=auth_header,
            settings=_ENABLED,
            required_roles=("admin",),
        )
        assert claims.sub == "user-1"

    def test_auth_disabled_passes_without_header(self) -> None:
        claims = authorize_stdlib(
            authorization=None,
            settings=_DISABLED,
            required_roles=("admin",),
        )
        assert claims.sub == "anonymous"

    def test_one_of_multiple_roles_passes(self) -> None:
        token = _make_token(roles=["operator"])
        auth_header = f"Bearer {token}"
        claims = authorize_stdlib(
            authorization=auth_header,
            settings=_ENABLED,
            required_roles=("operator", "admin"),
        )
        assert "operator" in claims.roles


# ---------------------------------------------------------------------------
# FastAPI dependency tests via httpx.AsyncClient + ASGITransport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFastAPIDepEnabled:
    """Auth enabled (JWT_SECRET set) — FastAPI dependency layer."""

    @pytest.fixture()
    def app(self) -> FastAPI:
        return _make_app(settings=_ENABLED)

    async def test_valid_token_grants_access(self, app: FastAPI) -> None:
        token = _make_token(sub="bob", roles=["viewer"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["sub"] == "bob"

    async def test_expired_token_returns_401(self, app: FastAPI) -> None:
        token = _expired_token(sub="carol")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    async def test_missing_header_returns_401(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/me")
        assert resp.status_code == 401

    async def test_wrong_role_returns_403(self, app: FastAPI) -> None:
        token = _make_token(sub="dave", roles=["viewer"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/operator", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    async def test_correct_role_passes(self, app: FastAPI) -> None:
        token = _make_token(sub="eve", roles=["operator"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/operator", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["sub"] == "eve"

    async def test_admin_role_also_passes_operator_route(self, app: FastAPI) -> None:
        token = _make_token(sub="frank", roles=["admin"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/operator", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    async def test_require_roles_multiple_allowed_any_match(self, app: FastAPI) -> None:
        """viewer role is one of the allowed roles for /reader."""
        token = _make_token(sub="grace", roles=["viewer"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/reader", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    async def test_admin_only_blocked_for_operator(self, app: FastAPI) -> None:
        token = _make_token(sub="hank", roles=["operator"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/admin-only", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    async def test_admin_only_allowed_for_admin(self, app: FastAPI) -> None:
        token = _make_token(sub="irene", roles=["admin"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/admin-only", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestFastAPIDepDisabled:
    """Auth disabled (neither JWT_SECRET nor JWKS_URL) — all requests pass."""

    @pytest.fixture()
    def app(self) -> FastAPI:
        return _make_app(settings=_DISABLED)

    async def test_no_header_passes_get_me(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/me")
        assert resp.status_code == 200
        assert resp.json()["sub"] == "anonymous"

    async def test_no_header_passes_operator_route(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/operator")
        assert resp.status_code == 200

    async def test_no_header_passes_admin_route(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/admin-only")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# _route_policy unit tests
# ---------------------------------------------------------------------------


class TestRoutePolicy:
    def test_healthz_is_open(self) -> None:
        result = _route_policy(
            route="/healthz", method="GET", path_parts=["healthz"], jwt_enabled=True
        )
        assert result == ()

    def test_metrics_admin_when_jwt_enabled(self) -> None:
        result = _route_policy(
            route="/metrics", method="GET", path_parts=["metrics"], jwt_enabled=True
        )
        assert "admin" in result

    def test_metrics_open_when_jwt_disabled(self) -> None:
        result = _route_policy(
            route="/metrics", method="GET", path_parts=["metrics"], jwt_enabled=False
        )
        assert result == ()

    def test_post_runs_requires_operator(self) -> None:
        result = _route_policy(
            route="/runs", method="POST", path_parts=["runs"], jwt_enabled=True
        )
        assert "operator" in result
        assert "admin" in result
        assert "viewer" not in result

    def test_get_runs_requires_viewer(self) -> None:
        result = _route_policy(
            route="/runs", method="GET", path_parts=["runs"], jwt_enabled=True
        )
        assert "viewer" in result

    def test_runs_search_requires_viewer(self) -> None:
        result = _route_policy(
            route="/runs/search", method="GET", path_parts=["runs", "search"], jwt_enabled=True
        )
        assert "viewer" in result

    def test_approve_requires_operator(self) -> None:
        result = _route_policy(
            route="/runs/abc/approve",
            method="POST",
            path_parts=["runs", "abc", "approve"],
            jwt_enabled=True,
        )
        assert "operator" in result
        assert "viewer" not in result

    def test_delete_run_requires_admin(self) -> None:
        result = _route_policy(
            route="/runs/abc",
            method="DELETE",
            path_parts=["runs", "abc"],
            jwt_enabled=True,
        )
        assert result == ("admin",)
