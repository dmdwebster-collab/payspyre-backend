"""E-signature adapters (SignNow).

This package holds the outbound e-sign vendor integration used to send loan
agreements / loan documents for signature and to track signing status. The
public surface is :class:`app.services.esign.signnow_adapter.SignNowAdapter`.

Like the verification adapters under ``app.services.adapters``, these adapters
are **injectable**: credentials are passed to the constructor (not read from
``integration_settings`` directly), so callers wire creds at the seam and tests
construct them with fakes.
"""
from __future__ import annotations

from app.services.esign.signnow_adapter import (
    SignNowAdapter,
    SignNowAPIError,
    SignNowPermanentError,
    SignNowTransientError,
    SignNowWebhookError,
    SignNowSendResult,
    SignNowStatusResult,
    SignNowWebhookEvent,
    SignerInput,
    FieldValue,
)

__all__ = [
    "SignNowAdapter",
    "SignNowAPIError",
    "SignNowPermanentError",
    "SignNowTransientError",
    "SignNowWebhookError",
    "SignNowSendResult",
    "SignNowStatusResult",
    "SignNowWebhookEvent",
    "SignerInput",
    "FieldValue",
]
