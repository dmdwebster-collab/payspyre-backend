"""Borrower-portal depth models (WS-J parity, migration 064).

Dave's "bank-level borrower security" mandate (video 11 / GAP mandate #10):

* ``PlatformPatientSecondFactor`` — per-patient 2FA state layered ON TOP of the
  magic-link login. Additive: a patient with no row (and not enforced) sees no
  behaviour change. TOTP secrets never leave the enrollment response; SMS codes
  live only as SHA-256 hashes in ``platform_events`` (magic-link machinery).
* ``PlatformPatientBankAccount`` — the borrower's Flinks-linked (or staff-added)
  payment accounts. Borrowers may ONLY read the masked list and pick the
  default; add/remove is staff-only (Dave: "they cannot delete the bank
  accounts... we do not want them to add bank accounts").
* ``PlatformPatientIdDocument`` — ID images (self + co-borrower). WRITE-ONLY
  for the borrower: uploaded via presigned PUT, listed as metadata, NEVER
  re-downloadable through any borrower endpoint (staff-only presigned GET,
  audited). New uploads supersede — former IDs are retained (ongoing file of
  new + former IDs per account), never deleted.
* ``PlatformPayoutRequest`` — borrower asks for a payout figure for a date
  ≤30 days out. Creates a STAFF task; the borrower never self-serves the
  figure, and a payout inquiry NEVER suspends scheduled payments (Dave's
  explicit messaging requirement). Staff respond via the forward calculator
  (workstream I).

Money is integer cents. Statuses are plain strings + CHECK constraints (no new
Postgres enums — simplest possible downgrade).
"""
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class PlatformPatientSecondFactor(Base):
    """Per-patient second-factor (2FA) enrollment + enforcement state.

    One row per patient. ``method`` is ``totp`` (authenticator app; RFC-6238
    verified server-side, no external dependency) or ``sms`` (code seam —
    Twilio creds pending, so codes go through the simulator sender and are
    persisted only as hashes in ``platform_events``).

    ``status``: ``pending`` (enrollment started, first code not yet verified)
    → ``active`` (verified; step-up now REQUIRED for sensitive actions).

    ``enforced`` is the per-patient enforcement flag (staff-set): when True,
    sensitive actions are refused until the patient enrolls — enrollment
    becomes mandatory, not optional.
    """

    __tablename__ = "platform_patient_second_factor"
    __table_args__ = (
        UniqueConstraint("patient_id", name="uq_platform_patient_2fa_patient"),
        CheckConstraint("method IN ('totp', 'sms')", name="ck_platform_patient_2fa_method"),
        CheckConstraint(
            "status IN ('pending', 'active')", name="ck_platform_patient_2fa_status"
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_patients.id", ondelete="CASCADE"),
        nullable=False,
    )

    method = Column(String, nullable=False)
    # TOTP shared secret (base32). Returned ONCE, in the enrollment response;
    # no API ever reads it back out. NULL for sms enrollments.
    totp_secret = Column(String, nullable=True)
    # Destination for SMS codes. NULL for totp enrollments.
    sms_phone_e164 = Column(String, nullable=True)

    status = Column(String, nullable=False, default="pending", server_default="pending")
    enrolled_at = Column(DateTime(timezone=True), nullable=True)

    # Staff-set per-patient enforcement flag: True = sensitive actions are
    # blocked until 2FA is enrolled and presented.
    enforced = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformPatientSecondFactor(patient_id={self.patient_id}, "
            f"method={self.method}, status={self.status}, enforced={self.enforced})>"
        )


class PlatformPatientBankAccount(Base):
    """A borrower payment account (Flinks-verified or staff-added).

    Stores ONLY masked/displayable identifiers (institution, currency, account
    type, masked routing + account) plus the external verification reference —
    never full account numbers. Exactly one active default per patient
    (partial unique index).

    Mutations are STAFF-ONLY (add/remove); the borrower's single write is
    choosing which active account is the default for payments.
    """

    __tablename__ = "platform_patient_bank_accounts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'removed')",
            name="ck_platform_patient_bank_account_status",
        ),
        CheckConstraint(
            "verified_via IN ('flinks', 'manual_staff')",
            name="ck_platform_patient_bank_account_verified_via",
        ),
        CheckConstraint(
            "source IS NULL OR source IN ('flinks', 'manual')",
            name="ck_platform_patient_bank_account_source",
        ),
        Index("ix_platform_patient_bank_accounts_patient", "patient_id"),
        # At most ONE active default per patient.
        Index(
            "uq_platform_patient_bank_account_default",
            "patient_id",
            unique=True,
            postgresql_where=text("is_default AND status = 'active'"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_patients.id", ondelete="CASCADE"),
        nullable=False,
    )

    institution_name = Column(String, nullable=False)
    currency = Column(String, nullable=False, default="CAD", server_default="CAD")
    account_type = Column(String, nullable=True)  # e.g. "Operations/Chequing"
    # Masked display values only (e.g. "•••77", "••••1000").
    routing_mask = Column(String, nullable=True)
    account_mask = Column(String, nullable=False)

    # Canadian routing identifiers (migration 069, Dave's Add Bank Account
    # dialog). TEXT — NOT integers — so a leading zero survives: institution
    # "003" must never round-trip as 3.
    institution_number = Column(String(3), nullable=True)
    transit_number = Column(String(5), nullable=True)
    account_holder = Column(String, nullable=True)
    # FULL account number, Fernet-encrypted at rest (app.core.secret_crypto).
    # Never returned by any API — display always uses ``account_mask``. Stored
    # because a PAD debit cannot be assembled from a mask.
    account_number_encrypted = Column(String, nullable=True)
    # Dave's literal "Source" column: 'flinks' | 'manual'.
    source = Column(String, nullable=True)

    # Provenance: 'flinks' (bank-verification flow) or 'manual_staff' (void
    # cheque / PAD form reviewed by a human and added in the back end).
    verified_via = Column(String, nullable=False)
    # Flinks login/account ref (or the staff ticket ref for manual adds).
    external_ref = Column(String, nullable=True)

    is_default = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    status = Column(String, nullable=False, default="active", server_default="active")

    added_by = Column(String, nullable=False)  # staff id / "flinks_link"
    removed_by = Column(String, nullable=True)
    removed_reason = Column(String, nullable=True)
    removed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        # Masked values only — safe by construction.
        return (
            f"<PlatformPatientBankAccount(patient_id={self.patient_id}, "
            f"account_mask={self.account_mask}, default={self.is_default}, "
            f"status={self.status})>"
        )


# The ID buckets shown in the portal (video 11 §1.3): borrower + co-borrower,
# front + back. Max one CURRENT document per (patient, bucket); new uploads
# supersede, former uploads are retained.
ID_DOCUMENT_BUCKETS = (
    "borrower_id_front",
    "borrower_id_back",
    "co_borrower_id_front",
    "co_borrower_id_back",
)


class PlatformPatientIdDocument(Base):
    """An ID image uploaded by the borrower (write-only from the portal).

    Bytes live in object storage; this row carries the key + display metadata.
    The borrower NEVER gets a read/download URL — Dave: "restrict this to
    upload new ID... keep an ongoing file of new and former IDs for the
    account." Staff read via an audited admin endpoint.
    """

    __tablename__ = "platform_patient_id_documents"
    __table_args__ = (
        CheckConstraint(
            "bucket IN ('borrower_id_front', 'borrower_id_back', "
            "'co_borrower_id_front', 'co_borrower_id_back')",
            name="ck_platform_patient_id_doc_bucket",
        ),
        CheckConstraint(
            "status IN ('pending', 'uploaded')",
            name="ck_platform_patient_id_doc_status",
        ),
        Index("ix_platform_patient_id_documents_patient", "patient_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_patients.id", ondelete="CASCADE"),
        nullable=False,
    )

    bucket = Column(String, nullable=False)
    object_key = Column(String, nullable=False)
    content_type = Column(String, nullable=True)
    filename = Column(String, nullable=True)
    size_bytes = Column(BigInteger, nullable=True)

    status = Column(String, nullable=False, default="pending", server_default="pending")
    # Current-vs-former: a confirmed upload supersedes the previous current in
    # its bucket. Former rows are never deleted (ongoing ID file).
    is_current = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    superseded_at = Column(DateTime(timezone=True), nullable=True)
    superseded_by_id = Column(
        UUID(as_uuid=True), ForeignKey("platform_patient_id_documents.id"), nullable=True
    )

    uploaded_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<PlatformPatientIdDocument(patient_id={self.patient_id}, "
            f"bucket={self.bucket}, status={self.status}, current={self.is_current})>"
        )


class PlatformPayoutRequest(Base):
    """A borrower's request for a payout figure (staff task, WS-J item 5).

    The borrower picks a date ≤30 days ahead; staff respond with the figure via
    the forward payout calculator (workstream I). The request does NOT compute
    anything and does NOT suspend scheduled payments — only an actual payoff
    does (Dave's explicit messaging rule).
    """

    __tablename__ = "platform_payout_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'responded', 'cancelled')",
            name="ck_platform_payout_request_status",
        ),
        Index("ix_platform_payout_requests_loan", "loan_id"),
        Index("ix_platform_payout_requests_status", "status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=False,
    )

    requested_payout_date = Column(Date, nullable=False)
    note = Column(String, nullable=True)

    status = Column(String, nullable=False, default="open", server_default="open")
    # Staff response (forward calculator output) — filled on respond.
    quoted_amount_cents = Column(BigInteger, nullable=True)
    response_note = Column(String, nullable=True)
    responded_by = Column(String, nullable=True)
    responded_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformPayoutRequest(loan_id={self.loan_id}, "
            f"date={self.requested_payout_date}, status={self.status})>"
        )
