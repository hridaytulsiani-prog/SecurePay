# Mounted at /adminpanel/ in securepay/urls.py. Everything here except
# login/ requires a valid admin session token (see AdminAPIView).
from django.urls import path

from adminpanel.api.v1.auth_views import (
    AdminAccountCreateView,
    AdminAccountDetailView,
    AdminAccountListView,
    AdminLoginView,
    AdminLogoutView,
    AdminMeView,
)
from adminpanel.api.v1.orders_views import (
    AdminMerchantListView,
    AdminOrderListView,
    AdminEnquiryListView,
    AdminSuspiciousPdfListView,
)
from adminpanel.api.v1.omniware_oversight_views import (
    OmniwareOversightReportView,
    OmniwarePartnerLoginView,
    OmniwarePartnerOversightReportView,
)
from adminpanel.api.v1.customer_issue_views import (
    AdminCustomerIssueDetailView,
    AdminCustomerIssueListView,
    AdminCustomerIssueMessageView,
    AdminCustomerIssueNoteView,
    CustomerIssueSubmitView,
)
from adminpanel.api.v1.merchant_issue_report import AdminMerchantIssueOrderReportView
from adminpanel.api.v1.order_check_views import AdminOrderCheckView
from adminpanel.api.v1.search_views import AdminSearchView
from adminpanel.api.v1.merchant_issue_views import (
    AdminMerchantIssueDetailView,
    AdminMerchantIssueListView,
    AdminMerchantIssueMessageView,
    AdminMerchantIssueNoteView,
)
from adminpanel.api.v1.contact_views import (
    AdminContactMessageDetailView,
    AdminContactMessageListView,
    AdminContactMessageReplyView,
    ContactMessageSubmitView,
)
from adminpanel.api.v1.stats_views import AdminStatsView
from adminpanel.api.v1.audit_views import AdminAuditLogListView
from adminpanel.api.v1.decision_history_views import AdminDecisionHistoryListView
from adminpanel.api.v1.enquiry_actions_views import (
    AdminEnquiryNoteListView,
    AdminEnquiryNoteDetailView,
    AdminEnquiryResolutionView,
)
from adminpanel.api.v1.courier_verification_views import (
    AdminBlueDartOtpCheckView,
    AdminDhlBlueDartOtpCheckView,
    AdminBlueDartLabelOtpEvidenceView,
    AdminBlueDartVerificationDecisionView,
    AdminDelhiveryOtpCheckView,
    AdminDelhiveryVerificationDecisionView,
    AdminXpressbeesOtpCheckView,
    AdminDtdcOtpCheckView,
    AdminShiprocketOtpCheckView,
    AdminEkartOtpCheckView,
    AdminShadowfaxOtpCheckView,
)
from adminpanel.api.v1.pa_control_views import (
    PACapabilityListView,
    PAEvidenceReportView,
    PAProtectedOrderActionView,
    PAProtectedOrderListCreateView,
)
from adminpanel.api.v1.needs_attention_views import (
    AdminNeedsAttentionDecisionView,
    AdminNeedsAttentionPdfDecisionView,
    AdminNeedsAttentionView,
)

urlpatterns = [
    # Session lifecycle
    path("login/", AdminLoginView.as_view(), name="admin-login"),
    path("logout/", AdminLogoutView.as_view(), name="admin-logout"),
    path("me/", AdminMeView.as_view(), name="admin-me"),
    path("accounts/create/", AdminAccountCreateView.as_view(), name="admin-account-create"),
    path("accounts/", AdminAccountListView.as_view(), name="admin-account-list"),
    path("accounts/<int:user_id>/", AdminAccountDetailView.as_view(), name="admin-account-detail"),
    # Read-only, cross-merchant listings
    path("merchants/", AdminMerchantListView.as_view(), name="admin-merchants"),
    path("orders/", AdminOrderListView.as_view(), name="admin-orders"),
    path("enquiries/", AdminEnquiryListView.as_view(), name="admin-enquiries"),
    path("suspicious-pdfs/", AdminSuspiciousPdfListView.as_view(), name="admin-suspicious-pdfs"),
    path("aggregator-oversight/", OmniwareOversightReportView.as_view(), name="admin-aggregator-oversight"),
    path("pa-control/capabilities/", PACapabilityListView.as_view(), name="admin-pa-capabilities"),
    path("pa-control/orders/", PAProtectedOrderListCreateView.as_view(), name="admin-pa-orders"),
    path("needs-attention/", AdminNeedsAttentionView.as_view(), name="admin-needs-attention"),
    path(
        "needs-attention/<str:vaultpay_order_id>/decision/",
        AdminNeedsAttentionDecisionView.as_view(),
        name="admin-needs-attention-decision",
    ),
    path(
        "needs-attention/<str:vaultpay_order_id>/pdf-decision/",
        AdminNeedsAttentionPdfDecisionView.as_view(),
        name="admin-needs-attention-pdf-decision",
    ),
    path("pa-control/evidence/<str:token>/", PAEvidenceReportView.as_view(), name="pa-evidence-report"),
    path(
        "pa-control/orders/<str:vaultpay_order_id>/<str:action>/",
        PAProtectedOrderActionView.as_view(),
        name="admin-pa-order-action",
    ),
    path("partner/aggregator/login/", OmniwarePartnerLoginView.as_view(), name="aggregator-partner-login"),
    path(
        "partner/aggregator/oversight/",
        OmniwarePartnerOversightReportView.as_view(),
        name="aggregator-partner-oversight",
    ),
    path("courier-verification/delhivery/", AdminDelhiveryOtpCheckView.as_view(), name="admin-delhivery-otp-check"),
    path("courier-verification/xpressbees/", AdminXpressbeesOtpCheckView.as_view(), name="admin-xpressbees-otp-check"),
    path("courier-verification/dtdc/", AdminDtdcOtpCheckView.as_view(), name="admin-dtdc-otp-check"),    path("courier-verification/shiprocket/", AdminShiprocketOtpCheckView.as_view(), name="admin-shiprocket-otp-check"),
    path("courier-verification/ekart/", AdminEkartOtpCheckView.as_view(), name="admin-ekart-otp-check"),
    path("courier-verification/shadowfax/", AdminShadowfaxOtpCheckView.as_view(), name="admin-shadowfax-otp-check"),
    path(
        "courier-verification/delhivery/decision/",
        AdminDelhiveryVerificationDecisionView.as_view(),
        name="admin-delhivery-verification-decision",
    ),
    path("courier-verification/bluedart/", AdminBlueDartOtpCheckView.as_view(), name="admin-bluedart-otp-check"),
    path("courier-verification/dhl-bluedart/", AdminDhlBlueDartOtpCheckView.as_view(), name="admin-dhl-bluedart-otp-check"),
    path(
        "courier-verification/bluedart/label-otp-evidence/",
        AdminBlueDartLabelOtpEvidenceView.as_view(),
        name="admin-bluedart-label-otp-evidence",
    ),
    path(
        "courier-verification/bluedart/decision/",
        AdminBlueDartVerificationDecisionView.as_view(),
        name="admin-bluedart-verification-decision",
    ),
    # Per-enquiry notes log (add/edit/delete)
    path("enquiries/<int:enquiry_id>/notes/", AdminEnquiryNoteListView.as_view(), name="admin-enquiry-notes"),
    path(
        "enquiries/<int:enquiry_id>/notes/<int:note_id>/",
        AdminEnquiryNoteDetailView.as_view(),
        name="admin-enquiry-note-detail",
    ),
    # Money-movement resolution (refund vs. pay merchant) + reason
    path(
        "enquiries/<int:enquiry_id>/resolution/",
        AdminEnquiryResolutionView.as_view(),
        name="admin-enquiry-resolution",
    ),
    # Public "Get in touch" form + owner-only inbox for it
    path("contact/", ContactMessageSubmitView.as_view(), name="contact-message-submit"),
    path("search/", AdminSearchView.as_view(), name="admin-search"),
    path("orders/check/", AdminOrderCheckView.as_view(), name="admin-order-check"),
    path("customer-issue/", CustomerIssueSubmitView.as_view(), name="customer-issue-submit"),
    path("customer-issues/", AdminCustomerIssueListView.as_view(), name="admin-customer-issues"),
    path("customer-issues/<int:issue_id>/", AdminCustomerIssueDetailView.as_view(), name="admin-customer-issue-detail"),
    path("customer-issues/<int:issue_id>/notes/", AdminCustomerIssueNoteView.as_view(), name="admin-customer-issue-notes"),
    path("customer-issues/<int:issue_id>/messages/", AdminCustomerIssueMessageView.as_view(), name="admin-customer-issue-messages"),
    path("merchant-issues/", AdminMerchantIssueListView.as_view(), name="admin-merchant-issues"),
    path("merchant-issues/<int:issue_id>/", AdminMerchantIssueDetailView.as_view(), name="admin-merchant-issue-detail"),
    path("merchant-issues/<int:issue_id>/report/", AdminMerchantIssueOrderReportView.as_view(), name="admin-merchant-issue-report"),
    path("merchant-issues/<int:issue_id>/notes/", AdminMerchantIssueNoteView.as_view(), name="admin-merchant-issue-notes"),
    path("merchant-issues/<int:issue_id>/messages/", AdminMerchantIssueMessageView.as_view(), name="admin-merchant-issue-messages"),
    path("contact-messages/", AdminContactMessageListView.as_view(), name="admin-contact-messages"),
    path("contact-messages/<int:message_id>/", AdminContactMessageDetailView.as_view(), name="admin-contact-message-detail"),
    path("contact-messages/<int:message_id>/reply/", AdminContactMessageReplyView.as_view(), name="admin-contact-message-reply"),
    path("stats/", AdminStatsView.as_view(), name="admin-stats"),
    path("audit-logs/", AdminAuditLogListView.as_view(), name="admin-audit-logs"),
    path("decision-history/", AdminDecisionHistoryListView.as_view(), name="admin-decision-history"),
]
