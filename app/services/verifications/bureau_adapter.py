"""Real credit-bureau adapter (Equifax / TransUnion) — wires the preserved
``credit_bureau.py`` HTTP client into the P4 flow engine's ``BureauAdapter``
contract so it is a drop-in replacement for ``MockBureauAdapter``.

Design
------
* **Drop-in for the mock.** Conforms to ``app.services.adapters.base.BureauAdapter``
  (``soft_pull`` / ``hard_pull`` -> ``BureauResult``). The flow engine
  (``flow_engine.py``) reads ``BureauResult.score`` / ``.result`` / ``.bankruptcy``
  / ``.fraud_signals["identity_high_risk"]`` and applies its own decision banding,
  so this adapter does NOT threshold — a successful pull is ``result="passed"``.
* **Credentials are constructor-injected**, sourced from
  ``integration_settings.get(db, "equifax")`` (or ``"transunion"``) by the caller
  — the adapter never touches the DB or ``settings`` directly. This keeps it pure
  and testable, and lets ops rotate creds via the admin area.
* **Error classification.** Network/timeout/5xx/rate-limit are *transient* and map
  to ``result="unknown"`` (the engine treats unknown as manual_review, never a
  decline — per the adapter timeout policy). 4xx (bad request / not found / auth)
  are *permanent* and map to ``result="failed"``.
* **No PII in logs / errors.** We never put SIN, DOB, name, postal code, or the
  raw bureau response into the ``BureauResult`` or any raised message. Only the
  bureau name, pull type, and an HTTP status class are surfaced.

The pure ``PatientProfile`` the engine carries is PII-light and does NOT include
SIN-last-3 or postal code (those live in source-tagged patient fields). The
caller must therefore supply a ``BureauPullRequest`` with the identifiers the
bureau API requires; see :class:`BureauPullRequest`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.services.adapters.base import (
    BureauAdapter,
    BureauResult,
    PatientProfile,
)
from app.services.credit_bureau import EquifaxClient, TransUnionClient

logger = logging.getLogger(__name__)


class BureauCredentialsError(Exception):
    """Raised when the injected integration-settings row lacks an API key."""


@dataclass(frozen=True)
class BureauPullRequest:
    """The borrower identifiers the bureau API needs, assembled by the caller
    from source-tagged patient fields. Kept separate from ``PatientProfile``
    because the pure engine profile is intentionally PII-light and lacks
    SIN/postal-code. None of these values are ever logged."""

    sin_last_3: str
    date_of_birth: str  # YYYY-MM-DD
    postal_code: str
    first_name: str
    last_name: str


def _api_key_from_settings(setting: Any, bureau: str) -> str:
    """Extract the API key from a PlatformIntegrationSettings-like row.

    The secret is stored under ``secrets["api_key"]`` (see integration_settings).
    Raises BureauCredentialsError if missing — never logs the value.
    """
    secrets = getattr(setting, "secrets", None) or {}
    api_key = secrets.get("api_key")
    if not api_key:
        raise BureauCredentialsError(
            f"No 'api_key' secret configured for bureau provider '{bureau}'."
        )
    return api_key


def _client_for(bureau: str, api_key: str, settings_row: Any = None):
    """Build the bureau HTTP client, consuming the row's typed behaviour config.

    For Equifax that is the non-secret half of Dave's quad (member number,
    customer code, environment) plus the request/response logging switches. The
    values come from the SAME ``settings_row`` the api_key does, so the admin
    Integrations page is the single source of truth. A row with no config (or
    ``settings_row=None``) reproduces the previous hard-coded behaviour:
    production base URL, no subscriber block, no logging.
    """
    if bureau == "equifax":
        from app.schemas.integration_config import EquifaxConfig

        raw = (getattr(settings_row, "config", None) or {}) if settings_row else {}
        try:
            cfg = EquifaxConfig.model_validate(raw)
        except Exception:  # noqa: BLE001 — tolerant read; writes validate
            cfg = EquifaxConfig()
        return EquifaxClient(
            api_key=api_key,
            # Only an explicitly-stored environment moves the base URL; an
            # absent config keeps the historical production origin.
            environment=(
                cfg.environment.value if "environment" in raw else "production"
            ),
            member_number=cfg.member_number,
            customer_code=cfg.customer_code,
            log_request=cfg.log_request,
            log_response=cfg.log_response,
        )
    if bureau == "transunion":
        return TransUnionClient(api_key=api_key)
    raise BureauCredentialsError(f"Unsupported bureau provider '{bureau}'.")


class RealBureauAdapter(BureauAdapter):
    """Wraps the Equifax/TransUnion HTTP client behind the flow's BureauAdapter."""

    def __init__(
        self,
        *,
        bureau: str,
        settings_row: Any,
        pull_request: BureauPullRequest,
        use_cache: bool = True,
    ) -> None:
        """
        Args:
            bureau: "equifax" or "transunion".
            settings_row: the ``integration_settings.get(db, bureau)`` row
                (constructor-injected — the adapter does NOT read the DB).
            pull_request: borrower identifiers the bureau API requires.
            use_cache: pass through to the client's in-process cache.
        """
        self._bureau = bureau
        self._api_key = _api_key_from_settings(settings_row, bureau)
        self._client = _client_for(bureau, self._api_key, settings_row)
        self._req = pull_request
        self._use_cache = use_cache

    # ---- BureauAdapter contract ------------------------------------------

    async def soft_pull(self, patient: PatientProfile) -> BureauResult:
        return await self._pull("soft")

    async def hard_pull(self, patient: PatientProfile) -> BureauResult:
        return await self._pull("hard")

    # ---- internals -------------------------------------------------------

    async def _pull(self, pull_type: str) -> BureauResult:
        try:
            report = await self._client.get_credit_report(
                sin_last_3=self._req.sin_last_3,
                date_of_birth=self._req.date_of_birth,
                postal_code=self._req.postal_code,
                first_name=self._req.first_name,
                last_name=self._req.last_name,
                use_cache=self._use_cache,
            )
        except httpx.HTTPStatusError as exc:
            return self._classify_http_error(pull_type, exc)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # Network-level failure — transient. No PII; log status class only.
            logger.warning(
                "bureau %s %s pull transient network error: %s",
                self._bureau, pull_type, type(exc).__name__,
            )
            return self._unknown(pull_type)
        except Exception as exc:  # noqa: BLE001 — incl. rate-limit / "No bureaus configured"
            # ValueError("API key not configured") and Exception("rate limit
            # exceeded") from the client land here. Rate limit is transient;
            # anything else we conservatively treat as transient (unknown) so a
            # bad pull becomes manual_review, never an erroneous auto-decline.
            msg = str(exc).lower()
            if "rate limit" in msg:
                logger.warning("bureau %s %s pull rate-limited", self._bureau, pull_type)
            else:
                logger.warning(
                    "bureau %s %s pull error: %s",
                    self._bureau, pull_type, type(exc).__name__,
                )
            return self._unknown(pull_type)

        return self._map_report(pull_type, report)

    def _classify_http_error(
        self, pull_type: str, exc: httpx.HTTPStatusError
    ) -> BureauResult:
        status = exc.response.status_code
        # 4xx (except 429) = permanent: bad request / unauthorized / not found.
        # 429 + 5xx = transient.
        if 400 <= status < 500 and status != 429:
            logger.warning(
                "bureau %s %s pull permanent HTTP %s", self._bureau, pull_type, status
            )
            return self._failed(pull_type)
        logger.warning(
            "bureau %s %s pull transient HTTP %s", self._bureau, pull_type, status
        )
        return self._unknown(pull_type)

    def _map_report(self, pull_type: str, report: dict[str, Any]) -> BureauResult:
        """Map the client's parsed report onto the flow's rich_payload contract.

        The client returns ``{"bureau","score","has_bankruptcy","has_collections",
        ...,"raw_response",...}``. The engine reads ``score`` (-> band decision),
        ``bankruptcy``, and ``fraud_signals["identity_high_risk"]``. ``raw_response``
        (full PII report) is deliberately dropped here.
        """
        raw_score = report.get("score")
        if raw_score is None:
            # A 2xx with no score is not a usable pull — manual review, not decline.
            logger.warning(
                "bureau %s %s pull returned no score", self._bureau, pull_type
            )
            return self._unknown(pull_type)

        score = int(raw_score)
        bankruptcy = bool(report.get("has_bankruptcy", False))

        # Surface bureau-derived risk into the engine's fraud_signals shape.
        # identity_high_risk is the key the engine reads to force manual review.
        fraud_signals: dict[str, object] = {
            "identity_high_risk": bool(report.get("has_collections", False))
            or bankruptcy,
            "has_collections": bool(report.get("has_collections", False)),
            "delinquency_count": int(report.get("delinquency_count", 0) or 0),
            "utilization_percent": float(report.get("utilization_percent", 0.0) or 0.0),
            "inquiry_count_6m": int(report.get("inquiry_count_6m", 0) or 0),
        }

        return BureauResult(
            pull_type=pull_type,  # type: ignore[arg-type]
            score=score,
            result="passed",  # successful pull; engine applies the score band
            bankruptcy=bankruptcy,
            fraud_signals=fraud_signals,
            confidence=1.0,
            vendor=self._bureau,
        )

    def _unknown(self, pull_type: str) -> BureauResult:
        return BureauResult(
            pull_type=pull_type,  # type: ignore[arg-type]
            score=0,
            result="unknown",
            vendor=self._bureau,
        )

    def _failed(self, pull_type: str) -> BureauResult:
        return BureauResult(
            pull_type=pull_type,  # type: ignore[arg-type]
            score=0,
            result="failed",
            vendor=self._bureau,
        )
