"""Admin API — Dave's canonical Application Status Flow v1.00, as data.

The UI renders workplace queues and per-file action buttons OFF this endpoint
instead of hard-coding a button list per screen: ask for the registry once, then
look up the application's status to get its label, owning workplace(s),
permitted actions and the external API it depends on.

Read-only and stateless (no DB): the registry is a code constant
(``app/services/application_status.py``), the single source of truth shared with
the orchestrator's transitions.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import require_roles
from app.services import application_status as status_model

router = APIRouter(dependencies=[Depends(require_roles("admin"))])


@router.get(
    "/status-model",
    summary="Dave's Application Status Flow v1.00 registry",
)
def get_status_model():
    """The whole registry, ordered by position in the linear flow.

    ``engine_statuses`` on each row lists the persisted
    ``platform_application_status`` values that resolve to that canonical status
    — the UI never needs to know the legacy vocabulary beyond this mapping.
    """
    return {
        "version": "1.00",
        "source": "PaySpyre - Application Status Flow v1,00.pdf (Dave, 2026-07-21)",
        "statuses": status_model.registry_payload(),
        "verification_gates": [s.value for s in status_model.VERIFICATION_GATES],
        "closed_statuses": [s.value for s in status_model.CLOSED_STATUSES],
        "off_model_terminals": [s.value for s in status_model.OFF_MODEL_TERMINALS],
        "legacy_to_canonical": {
            engine: canonical.value
            for engine, canonical in sorted(status_model.LEGACY_TO_CANONICAL.items())
        },
    }


@router.get(
    "/status-model/resolve",
    summary="Resolve one persisted status to its canonical spec + actions",
)
def resolve_status(
    status: str = Query(..., description="A platform_application_status value"),
):
    """404 on an unknown value — callers should pass a real persisted status."""
    spec = status_model.spec_for(status)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown application status {status!r}")
    return {"engine_status": status, **spec.to_dict()}
