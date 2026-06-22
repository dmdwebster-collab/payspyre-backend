"""AI/ML data-foundation tooling (Phase 0).

This package holds **offline** export utilities that turn the live platform
loan schema into labeled feature/outcome datasets for risk modelling. Nothing
in here is imported by the production API; it depends only on the Python stdlib
plus the app's SQLAlchemy models, so it adds **no** heavy ML deps
(pandas/sklearn) to the running service. Model *training* happens in a separate
workspace against the CSV this package emits.
"""
