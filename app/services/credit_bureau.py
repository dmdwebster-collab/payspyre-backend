import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)


class CreditBureauClient:
    """Base client for credit bureau integrations with caching and rate limiting."""

    def __init__(
        self,
        bureau_name: str,
        api_key: str,
        base_url: str,
        log_request: bool = False,
        log_response: bool = False,
    ):
        self.bureau_name = bureau_name
        self.api_key = api_key
        self.base_url = base_url
        # Settings-area "Log request" / "Log response" knobs. Off by default —
        # unchanged behaviour. Only NON-PII envelope metadata is ever logged:
        # bureau, endpoint, HTTP status, request id. Never the consumer block,
        # never the bureau file (Hard Rule #6).
        self.log_request = log_request
        self.log_response = log_response
        self._cache: dict[str, tuple[dict, datetime]] = {}
        self._rate_limit_tracker: dict[str, list[datetime]] = {}
        self.cache_ttl_hours = 24
        self.rate_limit_per_minute = 10

    async def _make_request(
        self,
        endpoint: str,
        method: str = "POST",
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to bureau API."""
        url = f"{self.base_url}{endpoint}"
        request_id = str(uuid4())
        request_headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        if headers:
            request_headers.update(headers)

        if self.log_request:
            logger.info(
                "bureau_request bureau=%s endpoint=%s method=%s request_id=%s",
                self.bureau_name, endpoint, method, request_id,
            )

        async with httpx.AsyncClient(timeout=30.0) as client:
            if method == "GET":
                response = await client.get(url, headers=request_headers, params=data)
            else:
                response = await client.post(url, headers=request_headers, json=data)

            if self.log_response:
                logger.info(
                    "bureau_response bureau=%s endpoint=%s request_id=%s status=%s bytes=%d",
                    self.bureau_name, endpoint, request_id,
                    response.status_code, len(response.content or b""),
                )

            response.raise_for_status()
            return response.json()

    def _generate_cache_key(self, sin_last_3: str, date_of_birth: str) -> str:
        """Generate deterministic cache key from borrower identifiers."""
        key_data = f"{self.bureau_name}:{sin_last_3}:{date_of_birth}"
        return hashlib.sha256(key_data.encode()).hexdigest()

    def _get_from_cache(self, cache_key: str) -> dict[str, Any] | None:
        """Retrieve cached report if not expired."""
        if cache_key not in self._cache:
            return None

        cached_data, cached_at = self._cache[cache_key]
        if datetime.utcnow() - cached_at > timedelta(hours=self.cache_ttl_hours):
            del self._cache[cache_key]
            return None

        return cached_data

    def _store_in_cache(self, cache_key: str, data: dict[str, Any]) -> None:
        """Store report in cache with timestamp."""
        self._cache[cache_key] = (data, datetime.utcnow())

    async def _check_rate_limit(self, client_id: str | None = None) -> bool:
        """Check if rate limit exceeded for client or globally."""
        key = client_id or "global"
        now = datetime.utcnow()
        one_minute_ago = now - timedelta(minutes=1)

        if key not in self._rate_limit_tracker:
            self._rate_limit_tracker[key] = []

        self._rate_limit_tracker[key] = [
            ts for ts in self._rate_limit_tracker[key] if ts > one_minute_ago
        ]

        if len(self._rate_limit_tracker[key]) >= self.rate_limit_per_minute:
            return False

        self._rate_limit_tracker[key].append(now)
        return True

    async def get_credit_report(
        self,
        sin_last_3: str,
        date_of_birth: str,
        postal_code: str,
        first_name: str,
        last_name: str,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Fetch credit report with caching and rate limiting."""
        if not self.api_key:
            raise ValueError(f"{self.bureau_name} API key not configured")

        cache_key = self._generate_cache_key(sin_last_3, date_of_birth)

        if use_cache:
            cached = self._get_from_cache(cache_key)
            if cached:
                return cached

        if not await self._check_rate_limit():
            raise Exception(f"{self.bureau_name} rate limit exceeded")

        report = await self._fetch_report_from_bureau(
            sin_last_3=sin_last_3,
            date_of_birth=date_of_birth,
            postal_code=postal_code,
            first_name=first_name,
            last_name=last_name,
        )

        if use_cache:
            self._store_in_cache(cache_key, report)

        return report

    async def _fetch_report_from_bureau(
        self,
        sin_last_3: str,
        date_of_birth: str,
        postal_code: str,
        first_name: str,
        last_name: str,
    ) -> dict[str, Any]:
        """Override in subclass for bureau-specific implementation."""
        raise NotImplementedError


class EquifaxClient(CreditBureauClient):
    """Equifax Canada credit bureau client.

    The settings-area Equifax block (``app.schemas.integration_config.
    EquifaxConfig``) is consumed here: ``environment`` selects the base URL,
    and ``member_number`` / ``customer_code`` are sent on every request as the
    subscriber identifiers Equifax requires. The fourth member of Dave's quad —
    the security code — is a credential and reaches the client as part of
    ``api_key``/secrets, never through this config.
    """

    #: environment -> API origin. "test" is the default because
    #: ``EquifaxConfig.environment`` defaults to test, and no production
    #: subscriber agreement exists yet.
    BASE_URLS = {
        "test": "https://api.sandbox.equifax.ca/v1",
        "production": "https://api.equifax.ca/v1",
    }

    def __init__(
        self,
        api_key: str,
        environment: str = "production",
        member_number: Optional[str] = None,
        customer_code: Optional[str] = None,
        log_request: bool = False,
        log_response: bool = False,
    ):
        # Default ``environment="production"`` preserves the previous
        # hard-coded base URL for every existing caller that passes only
        # api_key (the two unit-test suites and any un-migrated code path).
        super().__init__(
            bureau_name="equifax",
            api_key=api_key,
            base_url=self.BASE_URLS.get(environment, self.BASE_URLS["production"]),
            log_request=log_request,
            log_response=log_response,
        )
        self.environment = environment
        self.member_number = member_number
        self.customer_code = customer_code

    def _subscriber_block(self) -> dict[str, Any]:
        """Equifax subscriber identifiers, omitted entirely when unconfigured."""
        block = {}
        if self.member_number:
            block["member_number"] = self.member_number
        if self.customer_code:
            block["customer_code"] = self.customer_code
        return {"subscriber": block} if block else {}

    async def _fetch_report_from_bureau(
        self,
        sin_last_3: str,
        date_of_birth: str,
        postal_code: str,
        first_name: str,
        last_name: str,
    ) -> dict[str, Any]:
        """Fetch credit report from Equifax."""
        payload = {
            "consumer": {
                "sin_last_3": sin_last_3,
                "date_of_birth": date_of_birth,
                "postal_code": postal_code,
                "first_name": first_name,
                "last_name": last_name,
            },
            "product": "credit_score_plus",
            "consent": True,
            "purpose": "credit_application",
            **self._subscriber_block(),
        }

        response = await self._make_request("/credit/report", data=payload)
        return self._parse_equifax_response(response)

    def _parse_equifax_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Parse Equifax response into standardized format."""
        score_info = raw.get("score", {})
        bureau_data = raw.get("bureau_data", {})
        trades = bureau_data.get("trades", [])
        inquiries = bureau_data.get("inquiries", [])

        utilization = self._calculate_utilization(trades)
        delinquencies = self._count_delinquencies(trades)
        credit_history_months = self._calculate_history_months(trades)

        return {
            "bureau": "equifax",
            "score": score_info.get("value"),
            "score_range": {
                "min": score_info.get("min", 300),
                "max": score_info.get("max", 900),
            },
            "utilization_percent": utilization,
            "delinquency_count": delinquencies,
            "credit_history_months": credit_history_months,
            "trade_count": len(trades),
            "inquiry_count_6m": len([i for i in inquiries if self._is_recent_inquiry(i, months=6)]),
            "inquiry_count_12m": len([i for i in inquiries if self._is_recent_inquiry(i, months=12)]),
            "inquiries_last_6m": [
                {"date": i.get("date"), "type": i.get("type")}
                for i in inquiries
                if self._is_recent_inquiry(i, months=6)
            ],
            "has_bankruptcy": any(
                t.get("public_record", {}).get("type") == "bankruptcy"
                for t in bureau_data.get("public_records", [])
            ),
            "has_collections": any(
                t.get("status") in ("collection", "charge_off")
                for t in trades
            ),
            "raw_response": raw,
            "fetched_at": datetime.utcnow().isoformat(),
        }

    def _calculate_utilization(self, trades: list[dict]) -> float:
        """Calculate credit utilization percentage."""
        total_balance = 0
        total_limit = 0

        for trade in trades:
            if trade.get("account_type") == "revolving":
                total_balance += float(trade.get("balance", 0))
                total_limit += float(trade.get("credit_limit", 0))

        if total_limit == 0:
            return 0.0

        return round((total_balance / total_limit) * 100, 2)

    def _count_delinquencies(self, trades: list[dict]) -> int:
        """Count delinquent accounts (30+ days past due)."""
        count = 0
        for trade in trades:
            payment_history = trade.get("payment_history", "")
            if any(marker in payment_history for marker in ["2", "3", "4", "5", "6", "7", "8", "9"]):
                count += 1
        return count

    def _calculate_history_months(self, trades: list[dict]) -> int:
        """Calculate months of credit history."""
        if not trades:
            return 0

        oldest_date = None
        for trade in trades:
            opened = trade.get("date_opened")
            if opened:
                if oldest_date is None or opened < oldest_date:
                    oldest_date = opened

        if not oldest_date:
            return 0

        oldest = datetime.strptime(oldest_date, "%Y-%m-%d")
        return max(0, (datetime.utcnow() - oldest).days // 30)

    def _is_recent_inquiry(self, inquiry: dict, months: int) -> bool:
        """Check if inquiry is within specified months."""
        inquiry_date = inquiry.get("date")
        if not inquiry_date:
            return False

        try:
            inquiry_dt = datetime.strptime(inquiry_date, "%Y-%m-%d")
            cutoff = datetime.utcnow() - timedelta(days=months * 30)
            return inquiry_dt > cutoff
        except ValueError:
            return False


class TransUnionClient(CreditBureauClient):
    """TransUnion Canada credit bureau client."""

    def __init__(self, api_key: str):
        super().__init__(
            bureau_name="transunion",
            api_key=api_key,
            base_url="https://api.transunion.ca/v1",
        )

    async def _fetch_report_from_bureau(
        self,
        sin_last_3: str,
        date_of_birth: str,
        postal_code: str,
        first_name: str,
        last_name: str,
    ) -> dict[str, Any]:
        """Fetch credit report from TransUnion."""
        payload = {
            "subject": {
                "identification": {
                    "sin_last_3": sin_last_3,
                    "dob": date_of_birth,
                    "postal_code": postal_code,
                },
                "name": {
                    "first": first_name,
                    "last": last_name,
                },
            },
            "report_type": "full",
            "consent": {
                "given": True,
                "timestamp": datetime.utcnow().isoformat(),
            },
        }

        response = await self._make_request("/reports/credit", data=payload)
        return self._parse_transunion_response(response)

    def _parse_transunion_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Parse TransUnion response into standardized format."""
        score_info = raw.get("creditScore", {})
        tradelines = raw.get("tradelines", [])
        hard_inquiries = raw.get("inquiries", {}).get("hard", [])

        utilization = self._calculate_utilization(tradelines)
        delinquencies = self._count_delinquencies(tradelines)
        credit_history_months = self._calculate_history_months(tradelines)

        return {
            "bureau": "transunion",
            "score": score_info.get("score"),
            "score_range": {
                "min": score_info.get("range", {}).get("min", 300),
                "max": score_info.get("range", {}).get("max", 900),
            },
            "utilization_percent": utilization,
            "delinquency_count": delinquencies,
            "credit_history_months": credit_history_months,
            "trade_count": len(tradelines),
            "inquiry_count_6m": len([
                i for i in hard_inquiries
                if self._is_recent_inquiry(i.get("date"), months=6)
            ]),
            "inquiry_count_12m": len([
                i for i in hard_inquiries
                if self._is_recent_inquiry(i.get("date"), months=12)
            ]),
            "inquiries_last_6m": [
                {"date": i.get("date"), "subscriber": i.get("subscriber")}
                for i in hard_inquiries
                if self._is_recent_inquiry(i.get("date"), months=6)
            ],
            "has_bankruptcy": any(
                p.get("type") == "bankruptcy"
                for p in raw.get("publicRecords", [])
            ),
            "has_collections": any(
                t.get("accountStatus") in ("collection", "charge_off")
                for t in tradelines
            ),
            "raw_response": raw,
            "fetched_at": datetime.utcnow().isoformat(),
        }

    def _calculate_utilization(self, tradelines: list[dict]) -> float:
        """Calculate credit utilization percentage."""
        total_balance = 0.0
        total_limit = 0.0

        for trade in tradelines:
            if trade.get("accountType") == "revolving":
                total_balance += float(trade.get("currentBalance", 0))
                total_limit += float(trade.get("creditLimit", 0))

        if total_limit == 0:
            return 0.0

        return round((total_balance / total_limit) * 100, 2)

    def _count_delinquencies(self, tradelines: list[dict]) -> int:
        """Count delinquent accounts."""
        count = 0
        for trade in tradelines:
            status = trade.get("accountStatus", "").lower()
            payment_rating = trade.get("paymentRating", "")
            if "collection" in status or "charge" in status or payment_rating in ["2", "3", "4", "5", "6", "7", "8", "9"]:
                count += 1
        return count

    def _calculate_history_months(self, tradelines: list[dict]) -> int:
        """Calculate months of credit history."""
        if not tradelines:
            return 0

        oldest_date = None
        for trade in tradelines:
            opened = trade.get("dateOpened")
            if opened:
                if oldest_date is None or opened < oldest_date:
                    oldest_date = opened

        if not oldest_date:
            return 0

        oldest = datetime.strptime(oldest_date, "%Y-%m-%d")
        return max(0, (datetime.utcnow() - oldest).days // 30)

    def _is_recent_inquiry(self, inquiry_date: str | None, months: int) -> bool:
        """Check if inquiry is within specified months."""
        if not inquiry_date:
            return False

        try:
            inquiry_dt = datetime.strptime(inquiry_date[:10], "%Y-%m-%d")
            cutoff = datetime.utcnow() - timedelta(days=months * 30)
            return inquiry_dt > cutoff
        except ValueError:
            return False


class CreditBureauService:
    """Service for orchestrating credit bureau queries."""

    def __init__(self, db=None):
        # Prefer creds from the settings area (Dave's mandate); env fallback.
        # db=None (e.g. unit tests / call sites without a session) -> env only,
        # preserving prior behavior. Bureau is mock-only today, so this stays
        # graceful: an unconfigured bureau simply yields a None client.
        from app.services.integration_creds import resolve

        eq = resolve(
            db, "equifax",
            secret_keys=["api_key"],
            env={"api_key": "EQUIFAX_API_KEY"},
        )
        tu = resolve(
            db, "transunion",
            secret_keys=["api_key"],
            env={"api_key": "TRANSUNION_API_KEY"},
        )
        self.equifax_client = EquifaxClient(eq["api_key"]) if eq["api_key"] else None
        self.transunion_client = TransUnionClient(tu["api_key"]) if tu["api_key"] else None

    def _get_bureau_clients(self) -> list[CreditBureauClient]:
        """Return list of configured and enabled bureau clients."""
        clients = []
        if self.equifax_client and self.equifax_client.api_key:
            clients.append(self.equifax_client)
        if self.transunion_client and self.transunion_client.api_key:
            clients.append(self.transunion_client)
        return clients

    async def pull_credit(
        self,
        sin_last_3: str,
        date_of_birth: str,
        postal_code: str,
        first_name: str,
        last_name: str,
        bureaus: list[str] | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """
        Pull credit reports from specified bureaus.

        Args:
            sin_last_3: Last 3 digits of SIN
            date_of_birth: Date of birth (YYYY-MM-DD)
            postal_code: Postal code
            first_name: First name
            last_name: Last name
            bureaus: List of bureaus to query (equifax, transunion). If None, queries all configured.
            use_cache: Whether to use cached reports

        Returns:
            Dict containing reports from each bureau and aggregated metrics
        """
        clients = self._get_bureau_clients()

        if not clients:
            raise ValueError("No credit bureaus configured")

        if bureaus:
            clients = [
                c for c in clients
                if c.bureau_name.lower() in [b.lower() for b in bureaus]
            ]

        reports = {}
        errors = {}

        for client in clients:
            try:
                report = await client.get_credit_report(
                    sin_last_3=sin_last_3,
                    date_of_birth=date_of_birth,
                    postal_code=postal_code,
                    first_name=first_name,
                    last_name=last_name,
                    use_cache=use_cache,
                )
                reports[client.bureau_name] = report
            except Exception as e:
                errors[client.bureau_name] = str(e)

        if not reports:
            raise Exception(f"Failed to pull credit from any bureau: {errors}")

        return {
            "reports": reports,
            "aggregated": self._aggregate_reports(reports),
            "errors": errors,
            "fetched_at": datetime.utcnow().isoformat(),
        }

    def _aggregate_reports(self, reports: dict[str, dict]) -> dict[str, Any]:
        """Aggregate metrics from multiple bureau reports."""
        if not reports:
            return {}

        scores = [r.get("score") for r in reports.values() if r.get("score")]
        utilizations = [r.get("utilization_percent", 0) for r in reports.values()]
        delinquencies = [r.get("delinquency_count", 0) for r in reports.values()]
        histories = [r.get("credit_history_months", 0) for r in reports.values()]

        return {
            "average_score": round(sum(scores) / len(scores)) if scores else None,
            "min_score": min(scores) if scores else None,
            "max_score": max(scores) if scores else None,
            "average_utilization": round(sum(utilizations) / len(utilizations), 2) if utilizations else 0,
            "total_delinquencies": sum(delinquencies),
            "average_history_months": round(sum(histories) / len(histories)) if histories else 0,
            "has_any_bankruptcy": any(r.get("has_bankruptcy") for r in reports.values()),
            "has_any_collections": any(r.get("has_collections") for r in reports.values()),
            "bureau_count": len(reports),
        }