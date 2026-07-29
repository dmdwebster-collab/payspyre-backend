"""Fill the gaps a legacy loan book leaves in a borrower profile.

A servicing export is a LOAN book, not a CRM: it carries the borrower's name and
almost nothing else. PaySpyre needs a complete profile (address, email, phone) to
exercise servicing, statements, dunning and the borrower portal, so the importer
can synthesize the missing contact fields.

SAFETY — WHY THE PLACEHOLDER SHAPE IS A RUN PARAMETER, NOT A PRODUCT DEFAULT
---------------------------------------------------------------------------
Synthetic contact details are only safe if they CANNOT reach a real person. The
shape that guarantees that (an unresolvable e-mail TLD, an unroutable phone area
code) is a property of a specific one-time load, not a property of the platform:
a future portfolio may arrive with real, deliverable contact details that must be
kept verbatim. So :class:`PlaceholderPolicy` is passed INTO an import run and is
DISABLED by default — nothing is ever invented unless the operator asks for it,
and the shape they ask for is recorded on the run's report.

Generation is DETERMINISTIC (seeded by the borrower key), so re-running an import
produces the same values and stays idempotent.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

#: Canadian province codes, used to place a synthetic address plausibly.
_PROVINCE_CITIES: dict[str, tuple[str, str]] = {
    "BC": ("Kelowna", "V1Y"),
    "AB": ("Calgary", "T2P"),
    "SK": ("Saskatoon", "S7K"),
    "MB": ("Winnipeg", "R3C"),
    "ON": ("Toronto", "M5H"),
    "QC": ("Montreal", "H3B"),
    "NB": ("Moncton", "E1C"),
    "NS": ("Halifax", "B3J"),
    "PE": ("Charlottetown", "C1A"),
    "NL": ("St. John's", "A1C"),
    "YT": ("Whitehorse", "Y1A"),
    "NT": ("Yellowknife", "X1A"),
    "NU": ("Iqaluit", "X0A"),
}

_STREET_NAMES = (
    "Gordon", "Bernard", "Harvey", "Pandosy", "Richter", "Ellis", "Glenmore",
    "Springfield", "Lakeshore", "Sutherland", "Clement", "Rutland", "Dilworth",
    "Cawston", "Leon", "Doyle", "Water", "Abbott", "Knox", "Ethel",
)
_STREET_TYPES = ("St", "Ave", "Rd", "Dr", "Cres", "Way", "Blvd", "Pl")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class PlaceholderPolicy:
    """How (and whether) to synthesize missing borrower contact details.

    ``enabled`` is False by default: an import invents NOTHING unless the
    operator explicitly turns this on for that run.

    * ``email_domain`` — the full domain synthetic addresses are minted under.
      For a testing load this MUST be an unresolvable TLD so a stray send cannot
      leave the building (e.g. ``portfolio-import.kom``).
    * ``phone_area_code`` — the NANP area code synthetic numbers use. ``555`` is
      the conventional unroutable choice.
    * ``default_province`` — used when the source gives no geographic hint.
    """

    enabled: bool = False
    email_domain: str = "portfolio-import.invalid"
    phone_area_code: str = "555"
    fill_email: bool = True
    fill_phone: bool = True
    fill_address: bool = True
    default_province: str = "BC"
    #: Free-text note stamped on every synthesized value's provenance record.
    note: str = "synthesized by portfolio import (no source value)"

    def describe(self) -> dict:
        """What the run report states about this policy (operator-auditable)."""
        return {
            "enabled": self.enabled,
            "email_domain": self.email_domain,
            "email_tld": self.email_domain.rsplit(".", 1)[-1] if "." in self.email_domain else "",
            "phone_area_code": self.phone_area_code,
            "fill_email": self.fill_email,
            "fill_phone": self.fill_phone,
            "fill_address": self.fill_address,
            "default_province": self.default_province,
            "note": self.note,
        }

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.enabled:
            return problems
        if self.fill_email and not self.email_domain:
            problems.append("email_domain is required when fill_email is set")
        if self.fill_phone and not re.fullmatch(r"\d{3}", self.phone_area_code or ""):
            problems.append("phone_area_code must be exactly 3 digits")
        if self.fill_address and self.default_province not in _PROVINCE_CITIES:
            problems.append(
                f"default_province must be a Canadian province code "
                f"({', '.join(sorted(_PROVINCE_CITIES))})"
            )
        return problems


#: The one-time testing shape the owner specified for the first portfolio load:
#: every synthetic e-mail ends ``.kom`` and every synthetic phone starts 555, so
#: no imported record can produce a real e-mail or SMS. This is NOT a default —
#: an operator opts into it per run.
UNROUTABLE_TEST_POLICY = PlaceholderPolicy(
    enabled=True,
    email_domain="payspyre-import.kom",
    phone_area_code="555",
    note="one-time test load: unroutable .kom e-mail / 555 phone",
)

#: Nothing is invented. The default for any run that does not say otherwise.
NO_PLACEHOLDERS = PlaceholderPolicy(enabled=False)


@dataclass
class GeneratedContact:
    """The synthesized values for one borrower + which fields were invented."""

    email: Optional[str] = None
    phone_e164: Optional[str] = None
    street: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None
    postal_code: Optional[str] = None
    generated_fields: list[str] = field(default_factory=list)

    def address_dict(self) -> Optional[dict]:
        if not any((self.street, self.city, self.province, self.postal_code)):
            return None
        return {
            "street": self.street,
            "city": self.city,
            "province": self.province,
            "postal_code": self.postal_code,
        }


def _digest(*parts: object) -> int:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return int.from_bytes(hashlib.sha256(raw.encode("utf-8")).digest()[:8], "big")


def _slug(value: Optional[str], fallback: str) -> str:
    s = _NON_ALNUM.sub(".", (value or "").casefold()).strip(".")
    return s or fallback


def province_hint(vendor_code: Optional[str], default: str) -> str:
    """Many vendor codes lead with the province (``BC4906``). Use it when present."""
    code = (vendor_code or "").strip().upper()
    if len(code) >= 2 and code[:2] in _PROVINCE_CITIES:
        return code[:2]
    return default


def generate_contact(
    *,
    key: str,
    first_name: Optional[str],
    last_name: Optional[str],
    policy: PlaceholderPolicy,
    existing_email: Optional[str] = None,
    existing_phone: Optional[str] = None,
    existing_address: Optional[dict] = None,
    vendor_code: Optional[str] = None,
) -> GeneratedContact:
    """Synthesize ONLY the contact fields the source left empty.

    ``key`` seeds the deterministic generator — pass something stable and unique
    per borrower (the legacy account/customer id), so a re-import reproduces
    exactly the same values instead of minting new ones.
    """
    out = GeneratedContact()
    if not policy.enabled:
        return out

    seed = _digest(key, first_name, last_name)

    if policy.fill_email and not existing_email:
        local = f"{_slug(first_name, 'borrower')}.{_slug(last_name, key)}"
        # A short stable suffix keeps namesakes distinct without a DB round-trip.
        out.email = f"{local}.{seed % 10_000:04d}@{policy.email_domain}"
        out.generated_fields.append("email")

    if policy.fill_phone and not existing_phone:
        exchange = 200 + (seed // 7) % 800       # NANP exchange codes are 200-999
        line = (seed // 13) % 10_000
        out.phone_e164 = f"+1{policy.phone_area_code}{exchange:03d}{line:04d}"
        out.generated_fields.append("phone")

    if policy.fill_address and not existing_address:
        prov = province_hint(vendor_code, policy.default_province)
        city, fsa = _PROVINCE_CITIES.get(prov, _PROVINCE_CITIES[policy.default_province])
        number = 100 + (seed // 3) % 9_900
        street = _STREET_NAMES[(seed // 11) % len(_STREET_NAMES)]
        st_type = _STREET_TYPES[(seed // 17) % len(_STREET_TYPES)]
        out.street = f"{number} {street} {st_type}"
        out.city = city
        out.province = prov
        # A syntactically valid Canadian postal code seeded off the FSA.
        letters = "ABCEGHJKLMNPRSTVWXYZ"
        out.postal_code = (
            f"{fsa} {(seed // 19) % 10}"
            f"{letters[(seed // 23) % len(letters)]}"
            f"{(seed // 29) % 10}"
        )
        out.generated_fields.append("address")

    return out
