from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel


class KycState(str, Enum):
    PENDING_KYC = "pending_kyc"
    KYC_IN_PROGRESS = "kyc_in_progress"
    KYC_COMPLETED = "kyc_completed"
    KYC_EXPIRED = "kyc_expired"
    RISK_EVAL_PASSED = "risk_eval_passed"
    RISK_EVAL_FAILED = "risk_eval_failed"
    RISK_EVAL_REVIEW = "risk_eval_review"
    MANUAL_REVIEW = "manual_review"
    APPROVED = "approved"
    REJECTED = "rejected"


KYC_STATE_MACHINE = {
    KycState.PENDING_KYC: {
        "on": {
            "kyc_session_created": KycState.KYC_IN_PROGRESS,
        },
    },
    KycState.KYC_IN_PROGRESS: {
        "on": {
            "kyc_result_received": KycState.KYC_COMPLETED,
        },
        "timeout": KycState.KYC_EXPIRED,
    },
    KycState.KYC_EXPIRED: {
        "on": {
            "kyc_session_recreated": KycState.KYC_IN_PROGRESS,
        },
    },
    KycState.KYC_COMPLETED: {
        "on": {
            "risk_eval_passed": KycState.APPROVED,
            "risk_eval_failed": KycState.REJECTED,
            "risk_eval_review": KycState.MANUAL_REVIEW,
        },
    },
    KycState.MANUAL_REVIEW: {
        "on": {
            "review_approved": KycState.APPROVED,
            "review_rejected": KycState.REJECTED,
            "review_more_info": KycState.PENDING_KYC,
        },
    },
    KycState.APPROVED: {
        "on": {
            "docs_ready": "funding_prep",
        },
    },
    KycState.REJECTED: {
        "terminal": True,
    },
}


@dataclass
class StateTransition:
    event: str
    from_state: KycState
    to_state: KycState | str


class StateMachineEngine:
    def __init__(self):
        self.transitions = KYC_STATE_MACHINE

    async def handle_event(
        self,
        current_state: KycState,
        event: str,
    ) -> StateTransition | None:
        state_config = self.transitions.get(current_state, {})

        if event == "timeout":
            timeout_state = state_config.get("timeout")
            if timeout_state:
                return StateTransition(
                    event=event,
                    from_state=current_state,
                    to_state=timeout_state,
                )
            return None

        on_events = state_config.get("on", {})
        next_state = on_events.get(event)

        if next_state:
            return StateTransition(
                event=event,
                from_state=current_state,
                to_state=next_state,
            )

        return None

    def is_terminal(self, state: KycState | str) -> bool:
        state_config = self.transitions.get(state, {})
        return state_config.get("terminal", False)

    def get_valid_events(self, state: KycState) -> list[str]:
        state_config = self.transitions.get(state, {})
        on_events = state_config.get("on", {})
        return list(on_events.keys())


state_machine = StateMachineEngine()