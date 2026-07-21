"""System documents (WS-B, Turnkey parity): versioned merge-field templates +
per-loan generated document snapshots.

Turnkey's "Settings > Application process > System documents" holds loan
agreements, PAD agreements, amortization/fee schedules, T&Cs and privacy policy
as VERSIONED templates — global defaults plus per-credit-product and per-vendor
overrides (the BC1180/AB4464-style per-clinic library in video 08). This module
is that store:

* ``PlatformDocumentTemplate`` — one row per template VERSION. A new version is
  a NEW row (old rows are immutable history — never edited, never deleted);
  ``active`` is the only mutable flag (deactivate/restore). Resolution
  precedence at generation time: vendor-scoped > product-scoped > global,
  highest active version wins within a scope.
* ``PlatformLoanDocument`` — a generated document snapshot for one loan: the
  rendered HTML (merge fields resolved) frozen at generation time, plus the
  template id/version it came from and the merge context used. The booking-time
  loan agreement lives here (what the borrower sees/signs), as do on-demand
  admin regenerations and borrower statements.

``kind`` / ``scope`` are Strings (closed sets validated at the API/service
layer), mirroring the ``platform_application_documents`` convention rather than
PG enums.
"""
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.base import Base

#: Closed set of template kinds (String column — validated at the service/API
#: layer). ``account_statement`` extends the spec's six kinds so the borrower
#: on-demand statement renders through the same engine.
DOCUMENT_KINDS = (
    "loan_agreement",
    "pad_agreement",
    "amortization_schedule",
    "fee_schedule",
    "terms_and_conditions",
    "privacy_policy",
    "account_statement",
)

#: Template scopes. Resolution precedence: vendor > product > global.
DOCUMENT_SCOPES = ("global", "product", "vendor")


class PlatformDocumentTemplate(Base):
    """One VERSION of a system-document template (append-only history)."""

    __tablename__ = "platform_document_templates"

    __table_args__ = (
        # Scope coherence: the scoping FK matches the declared scope, exactly.
        CheckConstraint(
            "(scope = 'global' AND product_id IS NULL AND vendor_id IS NULL) OR "
            "(scope = 'product' AND product_id IS NOT NULL AND vendor_id IS NULL) OR "
            "(scope = 'vendor' AND vendor_id IS NOT NULL AND product_id IS NULL)",
            name="ck_document_templates_scope_coherent",
        ),
        CheckConstraint("version >= 1", name="ck_document_templates_version_positive"),
        # One row per (kind, scope-key, version). Split into three partial
        # unique indexes because NULL scope FKs would defeat a plain unique
        # constraint (Postgres treats NULLs as distinct).
        Index(
            "uq_document_templates_global_kind_version",
            "kind",
            "version",
            unique=True,
            postgresql_where=text("scope = 'global'"),
        ),
        Index(
            "uq_document_templates_product_kind_version",
            "kind",
            "product_id",
            "version",
            unique=True,
            postgresql_where=text("scope = 'product'"),
        ),
        Index(
            "uq_document_templates_vendor_kind_version",
            "kind",
            "vendor_id",
            "version",
            unique=True,
            postgresql_where=text("scope = 'vendor'"),
        ),
        # The resolution query: active templates of a kind.
        Index("ix_document_templates_kind_active", "kind", "active"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    kind = Column(String, nullable=False)
    scope = Column(String, nullable=False, default="global")
    product_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_products.id"),
        nullable=True,
    )
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=True)

    version = Column(Integer, nullable=False, default=1)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    # HTML with {{MergeField}} / {{Table:...}} placeholders (see
    # app/services/document_engine.py for the canonical dictionary).
    body_html = Column(Text, nullable=False)

    # The ONLY mutable field. True = this version participates in resolution
    # (highest active version of the best-matching scope wins). Old versions
    # are deactivated, never deleted.
    active = Column(Boolean, nullable=False, default=True)

    created_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<PlatformDocumentTemplate(kind={self.kind}, scope={self.scope}, "
            f"version={self.version}, active={self.active})>"
        )


class PlatformLoanDocument(Base):
    """A generated document frozen for one loan (booking snapshot or on-demand).

    ``body_html`` is the fully-rendered output — re-rendering the template later
    (new template version, changed loan data) NEVER mutates this row; a
    regeneration is a new row. ``merge_data`` snapshots the scalar merge context
    used, for audit ("what did the agreement say the principal was?").
    """

    __tablename__ = "platform_loan_documents"

    __table_args__ = (
        Index("ix_loan_documents_loan_created", "loan_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=False,
    )

    kind = Column(String, nullable=False)
    # Template provenance. Nullable: engine-built documents (e.g. a statement
    # rendered with the built-in layout because no template row exists) have no
    # template id.
    template_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_document_templates.id"),
        nullable=True,
    )
    template_version = Column(Integer, nullable=True)

    title = Column(String, nullable=False)
    body_html = Column(Text, nullable=False)
    merge_data = Column(JSONB, nullable=True)

    # 'booking' (generated by book_loan) | 'on_demand' (admin) | 'borrower'
    # (borrower-requested statement). String, closed set, service-validated.
    generated_via = Column(String, nullable=False, default="on_demand")
    created_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    loan = relationship("PlatformLoan")

    def __repr__(self) -> str:
        return (
            f"<PlatformLoanDocument(loan_id={self.loan_id}, kind={self.kind}, "
            f"generated_via={self.generated_via})>"
        )
