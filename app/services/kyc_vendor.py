import httpx
from uuid import UUID, uuid4

from app.core.config import settings
from app.schemas.kyc import KycSessionResponse


class DiditClient:
    BASE_URL = "https://api.didit.me/v1"

    def __init__(self):
        self.api_key = settings.DIDIT_API_KEY

    async def create_verification_session(
        self,
        borrower_id: UUID,
        loan_application_id: UUID,
        external_id: UUID,
    ) -> KycSessionResponse:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/verifications",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "external_id": str(external_id),
                    "template_id": "payspyre_dental_patient",
                    "redirect_url": f"https://payspyre.com/verify/{external_id}",
                },
            )
            response.raise_for_status()
            data = response.json()

            return KycSessionResponse(
                kyc_session_id=external_id,
                verification_url=data["verification_url"],
                expires_at=data["expires_at"],
            )

    async def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        from app.core.security import verify_webhook_signature
        return verify_webhook_signature(payload, signature, settings.DIDIT_WEBHOOK_SECRET)


class PersonaClient:
    BASE_URL = "https://withpersona.com/api/v1"

    def __init__(self):
        self.api_key = settings.PERSONA_API_KEY

    async def create_verification_session(
        self,
        borrower_id: UUID,
        loan_application_id: UUID,
        external_id: UUID,
    ) -> KycSessionResponse:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/inquiries",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "template_id": "itmpl_PAYSPYRE_DENTAL_PATIENT",
                    "reference_id": str(external_id),
                },
            )
            response.raise_for_status()
            data = response.json()

            return KycSessionResponse(
                kyc_session_id=external_id,
                verification_url=data["inquiry_url"],
                expires_at=data["expires_at"],
            )

    async def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        from app.core.security import verify_webhook_signature
        return verify_webhook_signature(payload, signature, settings.PERSONA_WEBHOOK_SECRET)


def get_vendor_client(vendor: str) -> DiditClient | PersonaClient:
    if vendor == "didit":
        return DiditClient()
    elif vendor == "persona":
        return PersonaClient()
    else:
        raise ValueError(f"Unknown vendor: {vendor}")