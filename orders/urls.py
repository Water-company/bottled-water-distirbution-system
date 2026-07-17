from django.urls import path

from accounts.views import PendingOrdersJsonView
from orders.views import (
    CancelOrderView,
    ChapaPaymentCallbackView,
    ChapaPaymentReturnView,
    CheckoutView,
    ComplaintAppealCreateView,
    NearbyAgentsPreviewView,
    OrderDetailView,
    OrderListView,
    OrderPaymentView,
    OrderTrackingView,
    OrderTrackingStatusView,
    RefreshDeliveryQRCodeView,
    ReorderOrderView,
    RetryRejectedOrderCheckoutView,
    RefundRequestCreateView,
    SkipDeliveryFeedbackView,
    SubmitDeliveryFeedbackView,
)

app_name = "orders"

urlpatterns = [
    path("checkout/", CheckoutView.as_view(), name="checkout"),
    path("nearby-agents-preview/", NearbyAgentsPreviewView.as_view(), name="nearby_agents_preview"),
    path("pending-orders-json/", PendingOrdersJsonView.as_view(), name="pending_orders_json"),
    path("payments/chapa/callback/", ChapaPaymentCallbackView.as_view(), name="payment_callback"),
    path("", OrderListView.as_view(), name="list"),
    path("<str:order_number>/", OrderDetailView.as_view(), name="detail"),
    path("<str:order_number>/cancel/", CancelOrderView.as_view(), name="cancel"),
    path("<str:order_number>/reorder/", ReorderOrderView.as_view(), name="reorder"),
    path("<str:order_number>/retry-checkout/", RetryRejectedOrderCheckoutView.as_view(), name="retry_checkout"),
    path("<str:order_number>/refresh-qr/", RefreshDeliveryQRCodeView.as_view(), name="refresh_qr"),
    path("<str:order_number>/feedback/", SubmitDeliveryFeedbackView.as_view(), name="submit_feedback"),
    path("<str:order_number>/feedback/skip/", SkipDeliveryFeedbackView.as_view(), name="skip_feedback"),
    path("<str:order_number>/refunds/create/", RefundRequestCreateView.as_view(), name="request_refund"),
    path("complaints/<int:pk>/appeal/", ComplaintAppealCreateView.as_view(), name="appeal_complaint"),
    path("<str:order_number>/payment/", OrderPaymentView.as_view(), name="payment"),
    path("<str:order_number>/payment/return/", ChapaPaymentReturnView.as_view(), name="payment_return"),
    path("payment/success/<str:order_number>/", ChapaPaymentReturnView.as_view(), name="payment_success"),
    path("<str:order_number>/tracking/", OrderTrackingView.as_view(), name="tracking"),
    path("<str:order_number>/tracking/status/", OrderTrackingStatusView.as_view(), name="tracking_status_json"),
]
