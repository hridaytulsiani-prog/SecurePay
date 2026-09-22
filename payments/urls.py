# payments/urls.py
# Mounted at /payments/ in securepay/urls.py. Merchant-facing endpoints below
# (create_payment/verify_payment/shipments) are authenticated with a merchant
# session token (see payments.auth), not Django's session/user auth.
from django.urls import path
from payments.api.v1.generate_order import (
    GenerateOrder,
    VerifyPayment,
    CreatePayment,
    CreateManualOrder,
    ShipmentListView,
    MerchantNotificationListView,
)
from payments.api.v1.phonepe import PhonePeCallbackView, PhonePeInitiateView, PhonePeReturnView
from payments.api.v1.payment_lifecycle import PaymentWebhookView, SettlementRunView
from payments.api.v1.refunds import RefundRequestView, RefundWebhookView
from payments.api.v1.checkout_sessions import CheckoutSessionDetailView, CheckoutSessionView
from payments.api.v1.button_config import ButtonConfigView

urlpatterns = [
    path('create_payment/', CreatePayment.as_view(), name='create_payment'),
    path('verify_payment/', VerifyPayment.as_view(), name='verify_payment'),
    path('v1/shipments/', ShipmentListView.as_view(), name='shipment-list'),
    path('v1/notifications/', MerchantNotificationListView.as_view(), name='merchant-notifications'),
    path('v1/orders/create/', CreateManualOrder.as_view(), name='manual-order-create'),
    path("v1/checkout-sessions/", CheckoutSessionView.as_view(), name="checkout-session-create"),
    path("v1/checkout-sessions/<str:session_id>/", CheckoutSessionDetailView.as_view(), name="checkout-session-detail"),
    path("v1/button-config/", ButtonConfigView.as_view(), name="button-config"),
    path("phonepe/initiate/", PhonePeInitiateView.as_view(), name="phonepe-initiate"),
    path("phonepe/return/", PhonePeReturnView.as_view(), name="phonepe-return"),
    path("phonepe/callback/", PhonePeCallbackView.as_view(), name="phonepe-callback"),
    path("webhooks/<str:provider>/", PaymentWebhookView.as_view(), name="payment-webhook"),
    path("v1/refunds/", RefundRequestView.as_view(), name="refund-request"),
    path("webhooks/<str:provider>/refunds/", RefundWebhookView.as_view(), name="refund-webhook"),
    path("v1/settlements/run/", SettlementRunView.as_view(), name="settlement-run"),
]
