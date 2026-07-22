"""Masked-value types for the vendor (clinic) surface.

Dave, video 10 rule **R2** (verbatim): *"Bank details should have the majority
of the information blocked out by like say stars and just have the last few
digits available… 'your account is with Flinks capital, the account number ends
in 000.' Same with the routing number. The institution number could be fully
blocked out."*

The visibility fence (``tests/test_vendor_visibility_fence.py``) used to ban the
tokens ``bank``/``account``/``routing``/``institution`` outright, which would
have rejected exactly the view Dave asked for — and invited a token allowlist
that reopens the whole category. Instead the fence now enforces a **masked-value
contract**: a clinic field may carry bank vocabulary ONLY if it is declared with
one of the types below, whose validators make an unmasked value *unserializable*.

    class VendorBankDetail(BaseModel):
        bank_name: str                        # "Flinks Capital" — not a number
        account_number_masked: MaskedValue    # "•••• 000"
        routing_number_masked: MaskedValue    # "•••••5"
        institution_number_masked: MaskedValue  # "•••" (fully blocked)
        account_number_last4: Last4           # "0000"

Both types are pure (no DB, no I/O) and are the ONLY sanctioned way to put
account/routing/institution/transit vocabulary on ``/api/clinic/v1``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Optional

from pydantic import AfterValidator

# Characters that count as redaction. ``•`` (bullet) is what the admin CRM
# already emits (``admin_crm_vendors.mask_account`` -> ``•••• 1234``).
MASK_CHARS = "•●*·xX#"

# The most digits a masked value may reveal — Dave's "last few digits".
MAX_REVEALED_DIGITS = 4

_DIGIT_RUN = re.compile(r"\d+")


@dataclass(frozen=True)
class MaskedField:
    """Marker attached via ``Annotated`` so the fence can recognise the type.

    ``kind`` is ``"masked"`` (redacted display string) or ``"last4"`` (a bare
    last-N-digits field). The fence reads it; nothing at runtime depends on it.
    """

    kind: str


def validate_masked(value: Optional[str]) -> Optional[str]:
    """A redacted display value: mask characters present, ≤4 digits revealed.

    Accepts a fully-blocked value (all mask characters, zero digits) — that is
    Dave's institution-number case. Rejects anything that leaks a full number.
    """
    if value is None:
        return value
    if not isinstance(value, str):  # pragma: no cover - pydantic coerces first
        raise ValueError("Masked values must be strings.")
    stripped = value.strip()
    if not stripped:
        raise ValueError("Masked value must not be empty.")
    if not any(ch in MASK_CHARS for ch in stripped):
        raise ValueError(
            "Masked value must contain at least one redaction character "
            f"({MASK_CHARS}); refusing to expose an unmasked number."
        )
    digits = "".join(ch for ch in stripped if ch.isdigit())
    if len(digits) > MAX_REVEALED_DIGITS:
        raise ValueError(
            f"Masked value reveals {len(digits)} digits; at most "
            f"{MAX_REVEALED_DIGITS} may be shown to a vendor."
        )
    longest_run = max((len(m.group()) for m in _DIGIT_RUN.finditer(stripped)), default=0)
    if longest_run > MAX_REVEALED_DIGITS:  # pragma: no cover - implied by the total
        raise ValueError("Masked value contains an unmasked digit run.")
    return stripped


def validate_last4(value: Optional[str]) -> Optional[str]:
    """A bare last-N-digits field: 1–4 digits, nothing else."""
    if value is None:
        return value
    stripped = str(value).strip()
    if not stripped.isdigit() or not 1 <= len(stripped) <= MAX_REVEALED_DIGITS:
        raise ValueError(
            f"A last-4 field must be 1–{MAX_REVEALED_DIGITS} digits, got {value!r}."
        )
    return stripped


MaskedValue = Annotated[str, MaskedField("masked"), AfterValidator(validate_masked)]
Last4 = Annotated[str, MaskedField("last4"), AfterValidator(validate_last4)]


def mask_tail(raw: Optional[str], reveal: int = 3) -> Optional[str]:
    """Build a :data:`MaskedValue` from a raw number: ``"402993000"`` -> ``"••••••000"``.

    ``reveal`` is clamped to :data:`MAX_REVEALED_DIGITS`. ``reveal=0`` fully
    blocks the value (the institution-number case). Raw input never leaves this
    function — only the redacted string does.
    """
    if raw is None:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if not digits:
        return None
    reveal = max(0, min(reveal, MAX_REVEALED_DIGITS))
    tail = digits[-reveal:] if reveal else ""
    return MASK_CHARS[0] * max(len(digits) - reveal, 3) + tail
