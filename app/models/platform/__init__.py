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
from app.models.platform.vendor_disbursement import PlatformVendorDisbursement
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanCustomTransaction,
    PlatformLoanDelinquencySnapshot,
    PlatformLoanScheduleItem,
    PlatformLoanPayment,
    PlatformLoanStatement,
    PlatformLoanTransaction,
    PlatformCollectionAttempt,
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
from app.models.platform.decision_rule import PlatformDecisionRule
from app.models.platform.company_info import PlatformCompanyInfo
from app.models.platform.business_calendar import PlatformBusinessCalendarOverride
from app.models.platform.decision_reason import PlatformDecisionReason
from app.models.platform.import_batch import PlatformImportBatch
from app.models.platform.hardship import PlatformHardshipRequest
from app.models.platform.message import (
    PlatformApplicationMessage,
    PlatformApplicationMessageRead,
)
from app.models.platform.communication import PlatformCommunicationLog
from app.models.platform.document_template import (
    PlatformDocumentTemplate,
    PlatformLoanDocument,
)
from app.models.platform.collections_work import (
    PlatformCollectorAssignment,
    PlatformCollectionActionType,
    PlatformCollectionAction,
    PlatformPromiseToPay,
    PlatformInsolvencyMaintenanceFee,
)
from app.models.platform.loan_offer import PlatformLoanOffer
from app.models.platform.scorecard import (
    PlatformScorecard,
    PlatformVendorScorecard,
)
from app.models.platform.flag import (
    PlatformFlagAssignment,
    PlatformFlagDefinition,
)
from app.models.platform.crm import (
    PlatformClinicRole,
    PlatformCustomerBlock,
    PlatformCustomerBlockReason,
    PlatformIndustryCategory,
    PlatformVendorBankAccount,
    PlatformVendorContact,
    PlatformVendorDocument,
    PlatformVendorDocumentExpiryAlert,
    PlatformVendorOnboarding,
)
from app.models.platform.report_schedule import (
    PlatformReportDefinition,
    PlatformReportSchedule,
    PlatformReportScheduleRun,
)
from app.models.platform.blacklist import PlatformBlacklistEntry
from app.models.platform.bureau_batch import PlatformBureauBatch
from app.models.platform.province_compliance import PlatformProvinceComplianceRule
from app.models.platform.borrower_portal import (
    PlatformPatientSecondFactor,
    PlatformPatientBankAccount,
    PlatformPatientIdDocument,
    PlatformPayoutRequest,
)
from app.models.platform.application_process_config import (
    PlatformApplicationProcessConfig,
)

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
    "PlatformVendorDisbursement",
    "PlatformLoan",
    "PlatformLoanCustomTransaction",
    "PlatformLoanDelinquencySnapshot",
    "PlatformLoanScheduleItem",
    "PlatformLoanPayment",
    "PlatformLoanStatement",
    "PlatformLoanTransaction",
    "PlatformCollectionAttempt",
    "PlatformClinicMembership",
    "PlatformMarketplaceListing",
    "PlatformMarketplaceVendorInterest",
    "PlatformVendorProfileChangeRequest",
    "PlatformNotificationOutbox",
    "PlatformNotificationCursor",
    "PlatformNotificationRule",
    "PlatformDecisionRule",
    "PlatformCompanyInfo",
    "PlatformBusinessCalendarOverride",
    "PlatformDecisionReason",
    "PlatformImportBatch",
    "PlatformHardshipRequest",
    "PlatformApplicationMessage",
    "PlatformApplicationMessageRead",
    "PlatformCommunicationLog",
    "PlatformDocumentTemplate",
    "PlatformLoanDocument",
    "PlatformCollectorAssignment",
    "PlatformCollectionActionType",
    "PlatformCollectionAction",
    "PlatformPromiseToPay",
    "PlatformInsolvencyMaintenanceFee",
    "PlatformLoanOffer",
    "PlatformScorecard",
    "PlatformVendorScorecard",
    "PlatformFlagAssignment",
    "PlatformFlagDefinition",
    "PlatformClinicRole",
    "PlatformCustomerBlock",
    "PlatformCustomerBlockReason",
    "PlatformIndustryCategory",
    "PlatformVendorBankAccount",
    "PlatformVendorContact",
    "PlatformVendorDocument",
    "PlatformVendorDocumentExpiryAlert",
    "PlatformVendorOnboarding",
    "PlatformReportDefinition",
    "PlatformReportSchedule",
    "PlatformReportScheduleRun",
    "PlatformBlacklistEntry",
    "PlatformBureauBatch",
    "PlatformPatientSecondFactor",
    "PlatformPatientBankAccount",
    "PlatformPatientIdDocument",
    "PlatformPayoutRequest",
    "PlatformProvinceComplianceRule",
    "PlatformApplicationProcessConfig",
]
