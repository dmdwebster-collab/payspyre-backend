# P8.1 (2026-06-19): dropped the legacy V1 lending schemas (credit, funding, loan,
# underwriting, vendor) alongside the un-mounted+deleted V1 endpoints. document/stripe
# remain as P7.1-deferred orphans (separate cleanup).
from app.schemas import auth, document, kyc, patient, stripe

__all__ = ["auth", "document", "kyc", "patient", "stripe"]