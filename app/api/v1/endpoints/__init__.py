# P8.1 (2026-06-19): stopped eagerly importing the un-mounted legacy V1 lending
# modules (credit, funding, loan, underwriting, vendor, analytics). Leaving them here
# would re-import the unauthenticated surface at package load (and break the test suite
# once the files are deleted in the follow-up commit). credit_products and patients are
# imported directly by api.py, not via this package __init__.
from app.api.v1.endpoints import auth, health

__all__ = ["auth", "health"]