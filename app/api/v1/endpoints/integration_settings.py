"""HTTP admin API for platform integration settings (P8.2).

Wraps `app.services.integration_settings` behind FastAPI routes so admins / a
future admin UI can enter and manage per-provider credentials and config
(Didit, Flinks, SendGrid, Twilio, Zumrails, SignNow, Equifax, Google
Analytics) without the developer hardcoding them.

Conventions mirrored from `app/api/v1/endpoints/credit_products.py`:
- Pydantic schemas live in `app/api/schemas/integration_settings.py`
- ValueError from service layer -> HTTP 400
- All routes require the admin role.

SECURITY: secret credential VALUES are never returned. Reads/lists go through
the service `redact()` helper, which exposes only `secret_keys` (which keys are
set), never the values.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.schemas.integration_settings import (
    IntegrationSettingsRead,
    IntegrationSettingsUpsert,
)
from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.services import connection_test
from app.services import integration_settings as service

router = APIRouter()


class ConnectionTestResponse(BaseModel):
    provider: str
    ok: bool
    reason: str
    checked_at: datetime


@router.get(
    "",
    response_model=list[IntegrationSettingsRead],
    summary="List integration settings (redacted)",
    description=(
        "Admin-only. Returns every configured integration. Secret credential "
        "values are redacted — only `secret_keys` (which keys are set) is shown."
    ),
    dependencies=[Depends(require_roles("admin"))],
)
async def list_integration_settings(
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    return [service.redact(s) for s in service.list_all(db)]


@router.get(
    "/{provider}",
    response_model=IntegrationSettingsRead,
    summary="Get one provider's integration settings (redacted)",
    description=(
        "Admin-only. Secret values are redacted to `secret_keys`. 404 if the "
        "provider has not been configured."
    ),
    dependencies=[Depends(require_roles("admin"))],
)
async def get_integration_settings(
    provider: str,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    setting = service.get(db, provider)
    if setting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No integration settings configured for provider '{provider}'",
        )
    return service.redact(setting)


@router.put(
    "/{provider}",
    response_model=IntegrationSettingsRead,
    summary="Create or update a provider's integration settings",
    description=(
        "Admin-only. Upserts config + secrets + enabled for the provider. "
        "`secrets` is accepted on write only and is never echoed back — the "
        "response is redacted to `secret_keys`."
    ),
    dependencies=[Depends(require_roles("admin"))],
)
async def upsert_integration_settings(
    provider: str,
    data: IntegrationSettingsUpsert,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    updated_by = getattr(user, "id", None)
    try:
        setting = service.upsert(
            db,
            provider=provider,
            config=data.config,
            secrets=data.secrets,
            enabled=data.enabled,
            updated_by=updated_by,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return service.redact(setting)


@router.post(
    "/{provider}/test",
    response_model=ConnectionTestResponse,
    summary="Validate a provider's stored credentials",
    description=(
        "Admin-only. Runs a cheap, auth-only, side-effect-free call against the "
        "provider using the credentials currently in the settings area, so the team "
        "can confirm a key works right after entering it. Sends/charges/pulls "
        "nothing and never echoes secret values — only an ok flag + a redacted reason."
    ),
    dependencies=[Depends(require_roles("admin"))],
)
async def test_integration_connection(
    provider: str,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    result = connection_test.test_connection(db, provider)
    return ConnectionTestResponse(
        provider=provider,
        ok=result.ok,
        reason=result.reason,
        checked_at=result.checked_at,
    )
