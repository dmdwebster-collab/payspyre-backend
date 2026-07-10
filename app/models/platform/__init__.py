from app.models.platform.patient import PlatformPatient
from app.models.platform.patient_field import PlatformPatientField
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.secondary_income import PlatformApplicationSecondaryIncome
from app.models.platform.application_history import (
    PlatformApplicationAddressHistory,
    PlatformApplicationEmploymentHistory,
)
from app.models.platform.verification import PlatformVerification
from app.models.platform.consent import PlatformConsent
from app.models.platform.application_document import PlatformApplicationDocument
from app.models.platform.event import PlatformEvent
from app.models.platform.integration_settings import PlatformIntegrationSettings
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanScheduleItem,
    PlatformLoanPayment,
    PlatformLoanStatement,
)
from app.models.platform.clinic_membership import PlatformClinicMembership
from app.models.platform.marketplace import (
    PlatformMarketplaceListing,
    PlatformMarketplaceVendorInterest,
)
from app.models.platform.profile_change_request import (
    PlatformVendorProfileChangeRequest,
)
from app.models.platform.notification_outbox import PlatformNotificationOutbox
from app.models.platform.notification_cursor import PlatformNotificationCursor
from app.models.platform.notification_rule import PlatformNotificationRule

__all__ = [
    "PlatformPatient",
    "PlatformPatientField",
    "PlatformCreditProduct",
    "PlatformCreditApplication",
    "PlatformApplicationSecondaryIncome",
    "PlatformApplicationAddressHistory",
    "PlatformApplicationEmploymentHistory",
    "PlatformVerification",
    "PlatformConsent",
    "PlatformApplicationDocument",
    "PlatformEvent",
    "PlatformIntegrationSettings",
    "PlatformLoan",
    "PlatformLoanScheduleItem",
    "PlatformLoanPayment",
    "PlatformLoanStatement",
    "PlatformClinicMembership",
    "PlatformMarketplaceListing",
    "PlatformMarketplaceVendorInterest",
    "PlatformVendorProfileChangeRequest",
    "PlatformNotificationOutbox",
    "PlatformNotificationCursor",
    "PlatformNotificationRule",
]
