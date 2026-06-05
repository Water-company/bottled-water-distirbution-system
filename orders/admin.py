from django.contrib import admin, messages

from orders.models import DeliveryConfirmation, Order, OrderAgentRequest, OrderItem, OrderStatusHistory, Payment, RefundRequest
from orders.services import accept_agent_request, reject_agent_request


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("product_name", "unit_price", "quantity")


class OrderStatusHistoryInline(admin.TabularInline):
    model = OrderStatusHistory
    extra = 0
    readonly_fields = ("status", "note", "created_at")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("order_number", "customer", "company", "status", "selected_agent", "assigned_driver", "total")
    list_filter = ("status", "company", "created_at")
    search_fields = ("order_number", "customer__email", "customer__first_name", "customer__last_name", "company__name")
    inlines = [OrderItemInline, OrderStatusHistoryInline]


@admin.register(OrderAgentRequest)
class OrderAgentRequestAdmin(admin.ModelAdmin):
    list_display = ("order", "agent", "status", "distance_km", "responded_at")
    list_filter = ("status", "agent__company", "agent")
    search_fields = ("order__order_number", "agent__name", "agent__company__name")
    actions = ("accept_requests", "reject_requests")

    @admin.action(description="Accept selected agent requests")
    def accept_requests(self, request, queryset):
        success_count = 0
        for agent_request in queryset.select_related("order", "agent"):
            try:
                accept_agent_request(agent_request, note="Accepted from Django admin.", accepted_by=request.user)
                success_count += 1
            except Exception as exc:
                self.message_user(request, f"{agent_request}: {exc}", level=messages.ERROR)
        if success_count:
            self.message_user(request, f"Accepted {success_count} request(s).", level=messages.SUCCESS)

    @admin.action(description="Reject selected agent requests")
    def reject_requests(self, request, queryset):
        success_count = 0
        for agent_request in queryset.select_related("order", "agent"):
            try:
                reject_agent_request(agent_request, note="Rejected from Django admin.")
                success_count += 1
            except Exception as exc:
                self.message_user(request, f"{agent_request}: {exc}", level=messages.ERROR)
        if success_count:
            self.message_user(request, f"Rejected {success_count} request(s).", level=messages.SUCCESS)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("order", "provider", "status", "amount", "reference", "paid_at")
    list_filter = ("provider", "status")
    search_fields = ("order__order_number", "reference", "order__customer__email")


@admin.register(DeliveryConfirmation)
class DeliveryConfirmationAdmin(admin.ModelAdmin):
    list_display = ("order", "otp_code", "verified_at", "created_at")
    search_fields = ("order__order_number", "otp_code", "qr_token")


@admin.register(RefundRequest)
class RefundRequestAdmin(admin.ModelAdmin):
    list_display = ("order", "request_type", "status", "requested_amount", "approved_amount", "reviewed_by")
    list_filter = ("request_type", "status")
    search_fields = ("order__order_number", "order__customer__email", "reason")
