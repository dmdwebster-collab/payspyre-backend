"""Payments / funding-disbursement adapters (Zumrails).

Zumrails is the business's long-standing payments + funding-disbursement
provider. ``zumrails_adapter.ZumrailsAdapter`` wraps its REST API to disburse
funds for a funded loan and to collect payments, plus poll transaction status
and verify signed webhooks.

The adapter takes its credentials (``api_key`` / ``api_secret`` / ``base_url``)
as constructor params so it is injectable and unit-testable; it never reads
``integration_settings`` directly. Wiring code resolves the ``"zumrails"``
provider row and passes the values in (see WIRING NEEDED in the PR notes).
"""
