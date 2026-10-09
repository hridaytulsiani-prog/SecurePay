# merchant/urls.py
# Mounted at /merchants/ in securepay/urls.py. Two kinds of routes: static
# HTML pages (server-rendered checkout/tracking/dashboard demo pages served
# straight from merchant/static/) and the merchant account API
# (create_merchant/login, in v1/create_merchant.py).
#
# NOTE: GenerateOrder/VerifyPayment/CreatePayment are imported here but never
# referenced below — looks like leftover from an earlier version of this file.
from django.urls import path
from django.views.generic import TemplateView
from payments.api.v1.generate_order import GenerateOrder, VerifyPayment, CreatePayment
from .v1.issues import MerchantIssueDetail, MerchantIssueMessages, MerchantIssues
from .v1.create_merchant import (
    CreateMerchant,
    LoginMerchant,
    MerchantCheckoutFieldMapping,
    MerchantCourierPreferences,
    SendMerchantRegistrationOtp,
    VerifyMerchantRegistrationOtp,
)

urlpatterns = [
    path("store/", TemplateView.as_view(template_name="merchant/static/index.html"), name="store"),
    path("track/", TemplateView.as_view(template_name="merchant/static/tracking.html"), name="track"),
    path("dashboard/", TemplateView.as_view(template_name="merchant/static/dashboard.html"), name="dashboard"),
    path("login_test/", TemplateView.as_view(template_name="merchant/static/auth.html"), name="login"),
    path('create_merchant/', CreateMerchant.as_view(), name='create_merchant'),
    path('register/send-otp/', SendMerchantRegistrationOtp.as_view(), name='merchant_register_send_otp'),
    path('register/verify-otp/', VerifyMerchantRegistrationOtp.as_view(), name='merchant_register_verify_otp'),
    path('login/', LoginMerchant.as_view(), name='login_merchant'),
    path('courier-preferences/', MerchantCourierPreferences.as_view(), name='merchant_courier_preferences'),
    path('issues/', MerchantIssues.as_view(), name='merchant_issues'),
    path('issues/<int:issue_id>/', MerchantIssueDetail.as_view(), name='merchant_issue_detail'),
    path('issues/<int:issue_id>/messages/', MerchantIssueMessages.as_view(), name='merchant_issue_messages'),
    path('checkout-field-mapping/', MerchantCheckoutFieldMapping.as_view(), name='merchant_checkout_field_mapping'),
]
