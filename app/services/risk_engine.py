from typing import Any

from app.schemas.kyc import RiskEvaluationResponse


RISK_RULES = {
    "identity_match": {
        "description": "Name/DOB match ID document",
        "threshold": 0.9,
        "on_fail": "manual_review",
        "severity": "high",
    },
    "address_match": {
        "description": "Address on ID matches application",
        "threshold": "exact",
        "on_fail": "manual_review",
        "severity": "medium",
    },
    "liveness_confidence": {
        "description": "Face liveness score",
        "threshold": 0.95,
        "on_fail": "reject",
        "severity": "critical",
    },
    "aml_hits": {
        "description": "AML / sanctions / PEP matches",
        "threshold": 0,
        "on_fail": "reject",
        "severity": "critical",
    },
    "thin_file_hygiene": {
        "description": "Low credit history + high loan amount",
        "rule": "credit_history_months < 12 and loan_amount > 5000",
        "on_match": "manual_review",
        "severity": "medium",
    },
    "multiple_applications": {
        "description": "Same identity, multiple pending apps (fraud signal)",
        "rule": "count(pending_apps with same_name/dob) > 1",
        "on_match": "reject",
        "severity": "critical",
    },
    "canada_address_required": {
        "description": "PIPEDA — must have Canadian address",
        "rule": "address.country != 'CA'",
        "on_match": "reject",
        "severity": "critical",
    },
}


class RiskRulesEngine:
    def __init__(self):
        self.rules = RISK_RULES

    async def evaluate(
        self,
        kyc_result: dict[str, Any],
        loan_app: dict[str, Any],
        credit_report: dict[str, Any] | None = None,
    ) -> RiskEvaluationResponse:
        flags = []
        risk_score = 1.0

        # Critical checks = auto-reject
        liveness_score = self._get_check_score(kyc_result, "liveness")
        if liveness_score and liveness_score < 0.95:
            return RiskEvaluationResponse(
                decision="reject",
                reason="Liveness check failed",
                risk_score=0.0,
                flags_applied=["liveness_failed"],
            )

        aml_hits = self._get_aml_hits(kyc_result)
        if any(aml_hits):
            return RiskEvaluationResponse(
                decision="reject",
                reason="AML / sanctions hit",
                risk_score=0.0,
                flags_applied=["aml_hit"],
            )

        if loan_app.get("address", {}).get("country") != "CA":
            return RiskEvaluationResponse(
                decision="reject",
                reason="Non-Canadian address (PIPEDA)",
                risk_score=0.0,
                flags_applied=["non_ca_address"],
            )

        # Identity match = high severity
        identity_score = self._get_check_score(kyc_result, "identity")
        if identity_score and identity_score < 0.9:
            flags.append("identity_mismatch")
            risk_score -= 0.3

        # Address mismatch = medium severity
        address_status = self._get_check_status(kyc_result, "address")
        if address_status and address_status != "exact":
            flags.append("address_mismatch")
            risk_score -= 0.2

        # Credit-based risk assessment
        credit_score = loan_app.get("credit_score")
        credit_history = loan_app.get("credit_history_months", 0)
        loan_amount = loan_app.get("loan_amount", 0)

        if credit_report:
            aggregated = credit_report.get("aggregated", {})
            credit_score = aggregated.get("average_score", credit_score)
            credit_history = aggregated.get("average_history_months", credit_history)
            utilization = aggregated.get("average_utilization", 0)
            delinquencies = aggregated.get("total_delinquencies", 0)
            has_bankruptcy = aggregated.get("has_any_bankruptcy", False)
            has_collections = aggregated.get("has_any_collections", False)

            if has_bankruptcy:
                flags.append("bankruptcy_on_file")
                risk_score -= 0.5

            if has_collections:
                flags.append("collections_on_file")
                risk_score -= 0.3

            if delinquencies > 0:
                flags.append(f"delinquencies_found_{delinquencies}")
                risk_score -= min(0.4, delinquencies * 0.1)

            if utilization > 80:
                flags.append("high_utilization")
                risk_score -= 0.2
            elif utilization > 50:
                flags.append("elevated_utilization")
                risk_score -= 0.1

        # Thin file + high loan = medium severity
        if credit_history < 12 and loan_amount > 5000:
            flags.append("thin_file_high_amount")
            risk_score -= 0.15

        # Low credit score penalty
        if credit_score:
            if credit_score < 500:
                flags.append("very_low_credit_score")
                risk_score -= 0.4
            elif credit_score < 600:
                flags.append("low_credit_score")
                risk_score -= 0.25
            elif credit_score < 650:
                flags.append("below_average_credit_score")
                risk_score -= 0.1

        # Multiple applications = critical
        # TODO: Implement duplicate_check against database
        # duplicate_check = await check_duplicate_applications(loan_app)
        # if duplicate_check["count"] > 1:
        #     return RiskEvaluationResponse(...)

        # Decision
        if risk_score >= 0.85:
            decision = "approve"
            reason = "All checks passed"
        elif risk_score >= 0.6:
            decision = "manual_review"
            reason = "Requires human review: " + ", ".join(flags)
        else:
            decision = "reject"
            reason = "Risk score too low: " + ", ".join(flags)

        return RiskEvaluationResponse(
            decision=decision,
            reason=reason,
            risk_score=max(0.0, risk_score),
            flags_applied=flags,
        )

    def _get_check_score(self, kyc_result: dict[str, Any], check_type: str) -> float | None:
        for check in kyc_result.get("checks", []):
            if check.get("type") == check_type:
                return check.get("details", {}).get("score")
        return None

    def _get_check_status(self, kyc_result: dict[str, Any], check_type: str) -> str | None:
        for check in kyc_result.get("checks", []):
            if check.get("type") == check_type:
                return check.get("details", {}).get("match_status")
        return None

    def _get_aml_hits(self, kyc_result: dict[str, Any]) -> list[dict]:
        for check in kyc_result.get("checks", []):
            if check.get("type") == "aml":
                return check.get("details", {}).get("hits", [])
        return []