"""System mode — tells the cockpit whether it's running Simulated or Live.

PaySpyre is one codebase with two modes, switched by feature flags
(USE_REAL_ADAPTERS / USE_REAL_NOTIFICATIONS): SIMULATED uses mock vendors (no
creds, full demo), LIVE connects to the real integrations. The cockpit reads this
to show an unambiguous Test/Live banner so an operator always knows which world
they're in. Read-only, admin/staff.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import require_roles
from app.core.config import settings

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])


class SystemMode(BaseModel):
    environment: str
    mode: str                 # "simulation" | "live"
    label: str                # human banner text
    real_adapters: bool       # bureau / KYC / bank / disbursement vendors live?
    real_notifications: bool   # email / SMS live?
    simulation_enabled: bool   # demo/seed helpers mounted?


@router.get("/mode", response_model=SystemMode)
def get_mode():
    real = bool(settings.USE_REAL_ADAPTERS)
    sim_helpers = settings.ENVIRONMENT in ("development", "test") or settings.ENABLE_DEV_TOOLS
    return SystemMode(
        environment=settings.ENVIRONMENT,
        mode="live" if real else "simulation",
        label="Live — connected to real integrations" if real
        else "Simulation — mock integrations, no real money or data",
        real_adapters=real,
        real_notifications=bool(settings.USE_REAL_NOTIFICATIONS),
        simulation_enabled=bool(sim_helpers) and settings.ENVIRONMENT != "production",
    )
