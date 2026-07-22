"""Clinic AUTHORIZATION fence (P0) — the 9-role matrix is enforced, not decorative.

Dave's "Create vendor user" screen (video 09/10) grants nine permissions:
``loan_origination · loan_servicing · monitoring · collection ·
assignment_officer · vendor_management · archive · document_verification ·
export``. Before this module they were stored on
``platform_clinic_memberships.roles`` and checked on exactly ONE route
(``POST /disbursements/request``) — so a user granted only *Archive* could
originate loans and download the vendor's whole loan book.

This file makes the matrix STRUCTURAL:

1. ``test_every_clinic_route_declares_a_permission`` — no clinic route may
   exist without a ``require_clinic_permission`` dependency. A new ungated
   endpoint fails here.
2. ``test_route_permission_map_matches_snapshot`` — the exact route→role map
   is pinned; widening a route's grant is a conscious, reviewable diff.
3. ``test_every_clinic_route_still_scopes_to_the_vendor`` — the permission
   layer is ADDITIVE: every route still depends on ``get_current_clinic_user``
   (cross-vendor scoping, R1, is untouched).
4. HTTP-level behaviour: a limited role is 403 on routes outside its grant; a
   LEGACY (``roles IS NULL``) membership keeps full access.

DB-free by construction: the app mounts only ``clinic_router`` and overrides
``get_db`` / ``get_current_clinic_user``, and the 403 is raised in the
dependency layer before any handler touches a session.

Run (only this file — the full suite hits a shared remote DB):
    python -m pytest tests/test_clinic_authz.py -p no:warnings -q
"""
from __future__ import annotations

import importlib
import pkgutil
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.api.clinic.v1.endpoints as endpoints_pkg
from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.api.clinic.v1.router import clinic_router
from app.db.base import get_db
from app.services import clinic_permissions as cp

_BASE = "/api/clinic/v1"
_VENDOR_ID = uuid.uuid4()

# Dev-only seeding helper: unauthenticated by design, never mounted in
# production (router guard). Not part of the vendor authorization surface.
_EXCLUDED_PATHS = {f"{_BASE}/dev/seed-clinic"}


# ---------------------------------------------------------------------------
# THE MAP. (method, path) -> the role(s) that may reach the route (OR-ed).
#
# Derived from Dave's workplace model (docs/turnkey_parity/rewatch_2026-07-21/
# 09-10_tools_vendor.md): Origination originates, Servicing/Collection work the
# book, Monitoring reads dashboards, Vendor management owns the vendor's own
# profile + money position, Export owns downloads.
#
# `assignment_officer` / `document_verification` are ADD-ON roles (they never
# stand alone — see validate_role_assignment) and gate no route of their own.
# `archive` gates nothing yet: the vendor Archive surface is not built
# (09-10_tools_vendor.md §2.3 "Archive — MISSING"). When it lands it takes
# ``archive`` and this snapshot grows.
# ---------------------------------------------------------------------------

EXPECTED_ROUTE_PERMISSIONS: dict[tuple[str, str], tuple[str, ...]] = {
    # --- origination -------------------------------------------------------
    ("GET", f"{_BASE}/products"): ("loan_origination", "monitoring"),
    ("POST", f"{_BASE}/applications"): ("loan_origination",),
    ("POST", f"{_BASE}/applications/preview"): ("loan_origination",),
    (
        "POST",
        f"{_BASE}/applications/{{application_id}}/request-reprocessing",
    ): ("loan_origination",),
    ("POST", f"{_BASE}/financing-links"): ("loan_origination",),
    # --- pipeline reads ----------------------------------------------------
    ("GET", f"{_BASE}/applications"): (
        "loan_origination", "loan_servicing", "monitoring",
    ),
    ("GET", f"{_BASE}/dashboard/summary"): (
        "loan_origination", "loan_servicing", "monitoring",
    ),
    # --- dashboards --------------------------------------------------------
    ("GET", f"{_BASE}/dashboard/overview"): ("monitoring", "loan_servicing"),
    ("GET", f"{_BASE}/dashboard/applications/timeseries"): (
        "monitoring", "loan_servicing",
    ),
    ("GET", f"{_BASE}/dashboard/funnel"): ("monitoring", "loan_servicing"),
    ("GET", f"{_BASE}/dashboard/revenue"): ("monitoring", "loan_servicing"),
    # --- loan book (servicing + collections) -------------------------------
    ("GET", f"{_BASE}/dashboard/loan-book"): (
        "loan_servicing", "collection", "monitoring",
    ),
    # --- messages (R12: the ONLY sanctioned vendor comms channel) ----------
    ("GET", f"{_BASE}/applications/{{application_id}}/messages"): (
        "loan_origination", "loan_servicing", "collection", "monitoring",
    ),
    ("POST", f"{_BASE}/applications/{{application_id}}/messages"): (
        "loan_origination", "loan_servicing", "collection", "monitoring",
    ),
    ("POST", f"{_BASE}/applications/{{application_id}}/messages/read"): (
        "loan_origination", "loan_servicing", "collection", "monitoring",
    ),
    ("GET", f"{_BASE}/messages/unread-count"): (
        "loan_origination", "loan_servicing", "collection", "monitoring",
    ),
    ("GET", f"{_BASE}/messages/threads"): (
        "loan_origination", "loan_servicing", "collection", "monitoring",
    ),
    # --- marketplace (patient acquisition = origination) -------------------
    ("GET", f"{_BASE}/marketplace/leads"): ("loan_origination",),
    ("POST", f"{_BASE}/marketplace/leads/{{listing_id}}/express_interest"): (
        "loan_origination",
    ),
    ("GET", f"{_BASE}/marketplace/listings/{{listing_id}}"): ("loan_origination",),
    ("POST", f"{_BASE}/marketplace/listings/{{listing_id}}/book_appointment"): (
        "loan_origination",
    ),
    ("GET", f"{_BASE}/marketplace/billing/leads"): ("vendor_management", "export"),
    # --- the vendor's own account -----------------------------------------
    ("GET", f"{_BASE}/account/profile"): ("vendor_management", "monitoring"),
    ("POST", f"{_BASE}/account/profile/change-requests"): ("vendor_management",),
    # --- exports -----------------------------------------------------------
    ("GET", f"{_BASE}/reports/exports"): ("export",),
    ("GET", f"{_BASE}/reports/exports/{{report_key}}"): ("export",),
    # --- money position / self-serve disbursements -------------------------
    ("GET", f"{_BASE}/disbursements/wallet"): ("vendor_management", "loan_servicing"),
    ("GET", f"{_BASE}/disbursements"): ("vendor_management", "loan_servicing"),
    # Money-OUT. Pre-existing grant, deliberately NOT widened.
    ("POST", f"{_BASE}/disbursements/request"): ("loan_servicing",),
}


# ---------------------------------------------------------------------------
# Route introspection
# ---------------------------------------------------------------------------


def _mounted_app() -> FastAPI:
    app = FastAPI()
    app.include_router(clinic_router, prefix=_BASE)
    return app


def _clinic_routes() -> list[APIRoute]:
    """Every clinic route, discovered HERMETICALLY (imports only).

    DO NOT "simplify" this back to walking ``app.routes`` / ``clinic_router
    .routes`` — that is exactly the trap ``test_vendor_visibility_fence.py``
    documents. FastAPI 0.139 made ``include_router`` LAZY: the parent holds
    ``_IncludedRouter`` wrappers that are never expanded into ``APIRoute``
    objects (not by startup, not by ``openapi()``), so the walk finds NOTHING
    on CI while passing locally on an older pin — a silently blind fence.
    Verified against both 0.136.3 and 0.139.2.

    Each endpoint module's OWN ``router`` is safe: its routes are added by the
    decorators directly on that object, so they are real ``APIRoute``s (with
    the module router's prefix already applied, and ``.dependant`` built) under
    every FastAPI version. ``test_discovery_matches_the_mounted_app`` pins this
    list against the mounted app's OpenAPI schema so a module that stops being
    mounted — or a mounted route this walk misses — fails loudly.
    """
    routes: list[APIRoute] = []
    for info in pkgutil.walk_packages(
        endpoints_pkg.__path__, prefix=endpoints_pkg.__name__ + "."
    ):
        module = importlib.import_module(info.name)
        router = getattr(module, "router", None)
        if router is None:
            continue
        for route in router.routes:
            if isinstance(route, APIRoute) and _full_path(route) not in _EXCLUDED_PATHS:
                routes.append(route)
    return routes


def _full_path(route: APIRoute) -> str:
    """The route's path as mounted (module-router prefix + the API base)."""
    return f"{_BASE}{route.path}"


def _declared_permissions(route: APIRoute) -> tuple[str, ...] | None:
    """The permissions declared by the route's dependency tree (recursively)."""

    def walk(dependant):
        for dep in dependant.dependencies:
            perms = getattr(dep.call, "__clinic_permissions__", None)
            if perms:
                return perms
            found = walk(dep)
            if found:
                return found
        return None

    return walk(route.dependant)


def _depends_on_clinic_scoping(route: APIRoute) -> bool:
    def walk(dependant):
        for dep in dependant.dependencies:
            if dep.call is get_current_clinic_user:
                return True
            if walk(dep):
                return True
        return False

    return walk(route.dependant)


def _route_keys(route: APIRoute) -> list[tuple[str, str]]:
    return [
        (m, _full_path(route))
        for m in sorted(route.methods)
        if m not in ("HEAD", "OPTIONS")
    ]


# ---------------------------------------------------------------------------
# 1–3: structural
# ---------------------------------------------------------------------------


def test_discovery_is_not_empty():
    """Guard the guard: an empty walk would make every assertion below vacuous.

    This is the assertion that caught FastAPI 0.139's lazy ``include_router``
    turning the first version of this file into a blind fence on CI. Keep it.
    """
    assert len(_clinic_routes()) >= 25


def test_discovery_matches_the_mounted_app():
    """The hermetic walk finds EXACTLY the routes the app actually serves.

    Cross-checked against ``app.openapi()`` — a public API that reflects the
    real mounted surface under every FastAPI version. If a module router stops
    being mounted (or something is mounted that this walk cannot see), the two
    sets diverge and this fails instead of silently under-covering.
    """
    spec = _mounted_app().openapi()
    mounted = {
        (method.upper(), path)
        for path, ops in spec["paths"].items()
        for method in ops
        if path not in _EXCLUDED_PATHS
    }
    discovered = {key for route in _clinic_routes() for key in _route_keys(route)}
    assert discovered == mounted


def test_every_clinic_route_declares_a_permission():
    ungated = [
        f"{m} {path}"
        for route in _clinic_routes()
        for (m, path) in _route_keys(route)
        if _declared_permissions(route) is None
    ]
    assert not ungated, (
        "Clinic route(s) with NO role gate — any authenticated clinic user, "
        "whatever role the admin assigned them, can reach these:\n"
        + "\n".join(sorted(ungated))
    )


def test_route_permission_map_matches_snapshot():
    actual = {
        key: _declared_permissions(route)
        for route in _clinic_routes()
        for key in _route_keys(route)
    }
    assert actual == EXPECTED_ROUTE_PERMISSIONS, (
        "Clinic route→permission map drift. Widening a route's grant must be a "
        "conscious diff: check it against Dave's 9-role matrix before updating."
    )


def test_every_clinic_route_still_scopes_to_the_vendor():
    """R1 (cross-vendor scoping) is untouched — the role gate is ADDITIVE."""
    unscoped = [
        f"{m} {path}"
        for route in _clinic_routes()
        for (m, path) in _route_keys(route)
        if not _depends_on_clinic_scoping(route)
    ]
    assert not unscoped, "Clinic route(s) not bound to get_current_clinic_user:\n" + "\n".join(
        sorted(unscoped)
    )


def test_every_declared_permission_is_a_real_role():
    for perms in EXPECTED_ROUTE_PERMISSIONS.values():
        for p in perms:
            assert p in cp.CLINIC_ROLE_KEYS


def test_addon_roles_gate_no_route_alone():
    """Add-on roles never stand alone, so they must never be a route's ONLY grant."""
    for key, perms in EXPECTED_ROUTE_PERMISSIONS.items():
        assert not set(perms) <= cp.ADDON_ROLE_KEYS, key


# ---------------------------------------------------------------------------
# 4: HTTP behaviour — DB-free
# ---------------------------------------------------------------------------


def _client(roles) -> TestClient:
    app = _mounted_app()
    app.dependency_overrides[get_db] = lambda: MagicMock()
    app.dependency_overrides[get_current_clinic_user] = lambda: ClinicPrincipal(
        user=SimpleNamespace(id=uuid.uuid4()),
        vendor_id=_VENDOR_ID,
        role="staff",
        roles=roles,
    )
    return TestClient(app, raise_server_exceptions=False)


def _call(client: TestClient, method: str, path: str):
    # Path params get any syntactically valid value: authorization is decided
    # before the handler runs, so the value never matters for the 403 assertion.
    concrete = (
        path.replace("{application_id}", str(uuid.uuid4()))
        .replace("{listing_id}", str(uuid.uuid4()))
        .replace("{report_key}", "loan-originations")
    )
    return client.request(method, concrete, json={})


ARCHIVE_ONLY = ("archive",)


@pytest.mark.parametrize(
    "key,perms", sorted(EXPECTED_ROUTE_PERMISSIONS.items())
)
def test_limited_role_is_refused_outside_its_grant(key, perms):
    """A user granted ONLY ``archive`` (which gates no route today) is 403 on
    every clinic route — the intra-vendor privilege-escalation bug."""
    assert "archive" not in perms  # sanity: nothing grants archive yet
    method, path = key
    resp = _call(_client(ARCHIVE_ONLY), method, path)
    assert resp.status_code == 403, (
        f"{method} {path} reachable by an archive-only user (got "
        f"{resp.status_code}) — the role matrix is not enforced here."
    )


@pytest.mark.parametrize(
    "key,perms", sorted(EXPECTED_ROUTE_PERMISSIONS.items())
)
def test_granted_role_is_not_refused(key, perms):
    """The first granted role reaches the route (no 403). The handler may still
    fail on the MagicMock session — we assert only that authorization passed."""
    method, path = key
    resp = _call(_client((perms[0],)), method, path)
    assert resp.status_code != 403, f"{method} {path} refused its own role {perms[0]}"


@pytest.mark.parametrize(
    "key,perms", sorted(EXPECTED_ROUTE_PERMISSIONS.items())
)
def test_legacy_null_roles_membership_keeps_full_access(key, perms):
    """BACK-COMPAT: memberships created before the matrix carry ``roles IS
    NULL`` and mean legacy-full-access. Rolling out enforcement must not lock
    a single existing clinic user out."""
    method, path = key
    resp = _call(_client(None), method, path)
    assert resp.status_code != 403, f"{method} {path} locked out a legacy (NULL-roles) member"


def test_explicit_empty_roles_is_a_deliberate_lockdown():
    """``roles = []`` is an admin locking a user down — distinct from NULL."""
    resp = _call(_client(()), "GET", f"{_BASE}/products")
    assert resp.status_code == 403


def test_no_membership_is_still_403_not_bypassed_by_the_role_gate():
    """The role gate is layered ON TOP of the membership check, so a caller
    with no clinic membership never reaches a route regardless of roles."""
    from fastapi import HTTPException

    app = _mounted_app()

    def _no_membership():
        raise HTTPException(status_code=403, detail="User is not a member of any clinic")

    app.dependency_overrides[get_db] = lambda: MagicMock()
    app.dependency_overrides[get_current_clinic_user] = _no_membership
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get(f"{_BASE}/products").status_code == 403


# ---------------------------------------------------------------------------
# has_any_clinic_permission (pure)
# ---------------------------------------------------------------------------


def test_has_any_clinic_permission_or_semantics():
    assert cp.has_any_clinic_permission(["collection"], ("loan_servicing", "collection"))
    assert not cp.has_any_clinic_permission(["archive"], ("loan_servicing", "collection"))


def test_has_any_clinic_permission_legacy_and_lockdown():
    assert cp.has_any_clinic_permission(None, ("export",)) is True
    assert cp.has_any_clinic_permission([], ("export",)) is False


def test_has_any_clinic_permission_rejects_unknown_and_empty():
    with pytest.raises(ValueError):
        cp.has_any_clinic_permission(["export"], ())
    with pytest.raises(ValueError):
        cp.has_any_clinic_permission(["export"], ("not_a_role",))


def test_require_clinic_permission_rejects_unknown_and_empty():
    with pytest.raises(ValueError):
        cp.require_clinic_permission()
    with pytest.raises(ValueError):
        cp.require_clinic_permission("loan_servicing", "not_a_role")
