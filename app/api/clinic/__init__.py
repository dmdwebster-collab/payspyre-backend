"""Clinic / practice-facing API surface (staff-authenticated). Prefix: /api/clinic.

Mirrors the structure of ``app/api/applicant`` but serves the practice console
rather than the patient flow. Endpoints are read-mostly dashboards plus a
"create financing link" action that pre-fills a patient application via the
orchestrator.

AUTH NOTE (follow-up): every clinic endpoint is gated by the existing platform
JWT (``app.core.auth.get_current_user``) as a reasonable default. There is no
clinic-specific principal or clinic<->application scoping modeled yet, so the
list/summary endpoints currently return platform-wide data. See
``app/api/clinic/v1/endpoints/applications.py`` for the scoping TODO.
"""
