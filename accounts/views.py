import json
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Avg, Count, Prefetch, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View
from django.views.generic import DetailView, FormView, TemplateView, UpdateView
from django_ratelimit.core import is_ratelimited

from accounts.forms import (
    AgentBatchSaleApprovalForm,
    AgentBatchSaleCancellationForm,
    AgentBatchSalePaymentForm,
    AgentBatchSaleReceiptForm,
    AgentBatchSaleRequestForm,
    AgentForm,
    AgentDriverCreateForm,
    AgentDriverUpdateForm,
    AgentInventoryAdjustmentForm,
    AgentStockThresholdForm,
    BatchRecallForm,
    CompanyForm,
    CompanyBatchForm,
    CompanyAgentUpdateForm,
    SystemCompanyRegistrationForm,
    CompanyPremiumSettingsForm,
    CompanyProductForm,
    CustomerAddressForm,
    CustomerProfileForm,
    DriverForm,
    DriverIssueReportForm,
    DriverLocationForm,
    InternalUserCreationForm,
    LoginForm,
    RegistrationForm,
    RegistrationOTPForm,
    RestockApprovalForm,
    RestockRequestForm,
    SystemUserUpdateForm,
    AnnouncementForm,
)
from accounts.models import CustomerAddress, User, UserRole
from accounts.reporting import (
    build_audit_log_csv,
    build_company_report_excel,
    build_company_report_pdf,
    build_system_report_excel,
    build_system_report_pdf,
    format_money,
)
from accounts.services import (
    create_registration_otp,
    get_company_admin_users,
    get_registration_resend_wait_seconds,
    send_company_admin_activation_email,
    verify_registration_otp,
)
from catalog.models import (
    Agent,
    AgentBatchSale,
    AgentBatchSalePayment,
    AgentBatchSalePaymentStatus,
    AgentBatchSaleStatus,
    AgentStock,
    CompanyBatch,
    CompanyBatchStatus,
    Company,
    CompanyVerificationStatus,
    Driver,
    InventoryBatch,
    InventoryTransaction,
    InventoryTransactionType,
    PaymentSchedule,
    Product,
    RestockRequest,
    RestockRequestStatus,
)
from catalog.services import (
    apply_agent_inventory_adjustment,
    approve_agent_batch_sale,
    cancel_agent_batch_sale,
    confirm_agent_batch_sale_receipt,
    confirm_agent_batch_sale_payment,
    create_company_starter_catalog,
    get_agent_open_batch_balance,
    recall_company_batch,
    reject_agent_batch_sale,
    reject_agent_batch_sale_payment,
    submit_agent_batch_sale_payment,
    submit_agent_batch_sale_request,
)
from core.mixins import (
    AgentManagerRequiredMixin,
    CompanyAdminRequiredMixin,
    CustomerRequiredMixin,
    DriverRequiredMixin,
    SystemAdminRequiredMixin,
)
from core.models import Announcement, AnnouncementTargetRole, AuditLog, DriverLocation, Notification
from core.navigation import get_user_home_url
from core.policies import get_customer_loyalty_summary
from core.services import deliver_announcement, notify_user, record_audit_log
from orders.models import DeliveryFeedback, Order, OrderAgentRequest, OrderStatus, RefundRequest, RefundRequestStatus
from orders.services import (
    QRConfirmationError,
    accept_delivery_assignment,
    approve_refund_request,
    assign_driver,
    complete_delivery_and_deduct_stock,
    confirm_delivery_by_qr,
    expire_order_request_if_needed,
    mark_order_arrived,
    mark_order_picked_up,
    process_refund_request,
    reject_refund_request,
    report_delivery_issue,
    start_delivery,
    submit_queued_qr_scans,
)


def _float_or_none(value):
    return float(value) if value not in (None, "") else None


def email_backend_uses_inbox_delivery():
    backend = (getattr(settings, "EMAIL_BACKEND", "") or "").lower()
    return not any(
        marker in backend
        for marker in (
            "console.emailbackend",
            "locmem.emailbackend",
            "filebased.emailbackend",
            "dummy.emailbackend",
        )
    )


def build_driver_dashboard_payload(driver, assigned_orders, location):
    active_statuses = {
        OrderStatus.DRIVER_ASSIGNED,
        OrderStatus.DRIVER_ACCEPTED,
        OrderStatus.PICKED_UP,
        OrderStatus.OUT_FOR_DELIVERY,
        OrderStatus.ARRIVED,
    }
    return {
        "currentDriverLocation": {
            "latitude": _float_or_none(getattr(location, "latitude", None)),
            "longitude": _float_or_none(getattr(location, "longitude", None)),
            "online": bool(location and location.is_online),
        },
        "assignedOrders": [
            {
                "orderNumber": order.order_number,
                "statusLabel": order.get_status_display(),
                "customerName": order.customer.full_name,
                "deliveryAddress": order.delivery_address,
                "latitude": float(order.latitude),
                "longitude": float(order.longitude),
                "agentName": order.selected_agent.name if order.selected_agent else driver.agent.name,
                "agentLatitude": float(order.selected_agent.latitude) if order.selected_agent else float(driver.agent.latitude),
                "agentLongitude": float(order.selected_agent.longitude) if order.selected_agent else float(driver.agent.longitude),
            }
            for order in assigned_orders
            if order.status in active_statuses
        ],
    }


def get_driver_active_orders(driver):
    return driver.assigned_orders.select_related("customer", "selected_agent").filter(
        status__in=[
            OrderStatus.DRIVER_ASSIGNED,
            OrderStatus.DRIVER_ACCEPTED,
            OrderStatus.PICKED_UP,
            OrderStatus.OUT_FOR_DELIVERY,
            OrderStatus.ARRIVED,
        ]
    ).order_by("-updated_at")


def build_driver_performance(driver):
    today = timezone.localdate()
    history_orders = driver.assigned_orders.select_related("customer", "selected_agent").order_by("-created_at")
    completed_today = history_orders.filter(delivered_at__date=today, status=OrderStatus.DELIVERED).count()
    assigned_today = history_orders.filter(driver_assigned_at__date=today).count()
    pending_today = history_orders.filter(
        status__in=[
            OrderStatus.DRIVER_ASSIGNED,
            OrderStatus.DRIVER_ACCEPTED,
            OrderStatus.PICKED_UP,
            OrderStatus.OUT_FOR_DELIVERY,
            OrderStatus.ARRIVED,
        ]
    ).count()
    delivered_orders = history_orders.filter(status=OrderStatus.DELIVERED)
    average_rating = driver.delivery_feedback_entries.exclude(rating__isnull=True).aggregate(avg=Avg("rating")).get("avg")
    on_time_minutes = getattr(settings, "DRIVER_ON_TIME_TARGET_MINUTES", 60)
    on_time_count = 0
    delivered_count = delivered_orders.count()
    for order in delivered_orders:
        if order.out_for_delivery_at and order.delivered_at:
            duration = order.delivered_at - order.out_for_delivery_at
            if duration <= timezone.timedelta(minutes=on_time_minutes):
                on_time_count += 1
    on_time_rate = round((on_time_count / delivered_count) * 100, 1) if delivered_count else 0
    earning_amount = getattr(settings, "DRIVER_DELIVERY_EARNING_AMOUNT", Decimal("50.00"))
    earnings_today = Decimal(str(completed_today)) * Decimal(str(earning_amount))
    earnings_month = Decimal(str(history_orders.filter(delivered_at__year=today.year, delivered_at__month=today.month, status=OrderStatus.DELIVERED).count())) * Decimal(str(earning_amount))
    return {
        "assigned_today": assigned_today,
        "completed_today": completed_today,
        "pending_today": pending_today,
        "earnings_today": earnings_today,
        "earnings_month": earnings_month,
        "average_rating": average_rating,
        "on_time_rate": on_time_rate,
        "delivered_all_time": delivered_count,
    }


def build_company_map_payload(company, agents, drivers):
    company_payload = None
    if company.latitude is not None and company.longitude is not None:
        company_payload = {
            "name": company.name,
            "address": company.address,
            "latitude": float(company.latitude),
            "longitude": float(company.longitude),
        }

    return {
        "company": company_payload,
        "agents": [
            {
                "name": agent.name,
                "locationName": agent.location_name,
                "address": agent.address,
                "latitude": float(agent.latitude),
                "longitude": float(agent.longitude),
                "serviceRadiusKm": float(agent.service_radius_km),
                "isActive": agent.is_active,
                "online": agent.is_online,
            }
            for agent in agents
        ],
        "drivers": [
            {
                "name": driver.user.full_name,
                "agentName": driver.agent.name,
                "vehicleIdentifier": driver.vehicle_identifier,
                "latitude": _float_or_none(getattr(getattr(driver.user, "driver_location", None), "latitude", None)),
                "longitude": _float_or_none(getattr(getattr(driver.user, "driver_location", None), "longitude", None)),
                "online": bool(getattr(getattr(driver.user, "driver_location", None), "is_online", False)),
                "lastPingAt": getattr(getattr(driver.user, "driver_location", None), "last_ping_at", None).isoformat()
                if getattr(driver.user, "driver_location", None)
                else "",
            }
            for driver in drivers
        ],
    }


def get_managed_agent_or_404(user):
    return get_object_or_404(Agent, admin=user)


def get_managed_company_queryset(user):
    if getattr(user, "managed_company_id", None):
        return Company.objects.filter(pk=user.managed_company_id).order_by("-is_verified", "-is_active", "name", "pk")
    return Company.objects.filter(admin=user).order_by("-is_verified", "-is_active", "name", "pk")


def get_managed_company_or_404(user, request=None):
    queryset = get_managed_company_queryset(user)
    default_company = queryset.first()
    if default_company is not None:
        return default_company
    raise Http404("No company is assigned to this company admin account.")


def ensure_agent_stock_rows(agent):
    for product in agent.company.products.all():
        AgentStock.objects.get_or_create(
            agent=agent,
            product=product,
            defaults={"available_quantity": 0, "reorder_level": 0},
        )


def ensure_product_stock_rows(product):
    for agent in product.company.agents.all():
        AgentStock.objects.get_or_create(
            agent=agent,
            product=product,
            defaults={"available_quantity": 0, "reorder_level": 0},
        )


def get_agent_driver_queryset(agent):
    active_statuses = [
        OrderStatus.PAID,
        OrderStatus.DRIVER_ASSIGNED,
        OrderStatus.DRIVER_ACCEPTED,
        OrderStatus.PICKED_UP,
        OrderStatus.OUT_FOR_DELIVERY,
        OrderStatus.ARRIVED,
    ]
    return agent.drivers.select_related("user", "user__driver_location").annotate(
        completed_deliveries=Count(
            "assigned_orders",
            filter=Q(assigned_orders__status=OrderStatus.DELIVERED),
            distinct=True,
        ),
        active_deliveries=Count(
            "assigned_orders",
            filter=Q(assigned_orders__status__in=active_statuses),
            distinct=True,
        ),
        average_rating=Avg("delivery_feedback_entries__rating"),
    )


def _safe_decimal(value):
    if value in (None, ""):
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def get_company_reporting_window(request):
    date_from = parse_date(request.GET.get("date_from", ""))
    date_to = parse_date(request.GET.get("date_to", ""))
    today = timezone.localdate()
    if date_to is None:
        date_to = today
    if date_from is None:
        date_from = date_to - timezone.timedelta(days=29)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    return date_from, date_to


def build_company_agent_rows(company):
    agents = list(
        company.agents.select_related("admin").prefetch_related(
            "drivers__user",
            "drivers__delivery_feedback_entries",
            "stocks",
            "accepted_orders",
        )
    )
    rows = []
    for agent in agents:
        delivered_orders = 0
        active_orders = 0
        revenue = Decimal("0.00")
        for order in agent.accepted_orders.all():
            if order.status == OrderStatus.DELIVERED:
                delivered_orders += 1
                revenue += _safe_decimal(order.total)
            elif order.status in {
                OrderStatus.PAID,
                OrderStatus.DRIVER_ASSIGNED,
                OrderStatus.DRIVER_ACCEPTED,
                OrderStatus.PICKED_UP,
                OrderStatus.OUT_FOR_DELIVERY,
                OrderStatus.ARRIVED,
            }:
                active_orders += 1

        ratings = []
        for driver in agent.drivers.all():
            ratings.extend(
                feedback.rating
                for feedback in driver.delivery_feedback_entries.all()
                if feedback.rating is not None
            )

        average_rating = (sum(ratings) / len(ratings)) if ratings else None
        rows.append(
            {
                "object": agent,
                "id": agent.pk,
                "name": agent.name,
                "manager_name": agent.admin.full_name if agent.admin else "Unassigned",
                "location_name": agent.location_name,
                "drivers_count": agent.drivers.count(),
                "active_orders": active_orders,
                "delivered_orders": delivered_orders,
                "revenue": revenue,
                "revenue_display": format_money(revenue),
                "average_rating": average_rating,
                "average_rating_display": f"{average_rating:.1f} / 5" if average_rating is not None else "No ratings yet",
                "low_stock_items": sum(1 for stock in agent.stocks.all() if stock.low_stock),
                "is_active": agent.is_active,
                "is_accepting_orders": agent.is_accepting_orders,
            }
        )
    rows.sort(key=lambda row: (-row["revenue"], row["name"].lower()))
    return rows


def build_company_product_rows(company):
    products = list(
        company.products.prefetch_related(
            "agent_stocks__agent",
            "order_items__order",
        ).order_by("name")
    )
    rows = []
    for product in products:
        stock_entries = [stock for stock in product.agent_stocks.all() if stock.agent.company_id == company.id]
        order_items = [
            item
            for item in product.order_items.all()
            if item.order.company_id == company.id and item.order.status == OrderStatus.DELIVERED
        ]
        stock_total = sum(stock.available_quantity for stock in stock_entries)
        units_sold = sum(item.quantity for item in order_items)
        revenue = sum((_safe_decimal(item.unit_price) * item.quantity) for item in order_items) or Decimal("0.00")
        rows.append(
            {
                "object": product,
                "id": product.pk,
                "name": product.name,
                "size_label": product.size_label,
                "price": product.price,
                "price_display": format_money(product.price),
                "available_quantity": product.available_quantity,
                "stock_total": stock_total,
                "agents_stocking": sum(1 for stock in stock_entries if stock.available_quantity > 0),
                "units_sold": units_sold,
                "revenue": revenue,
                "revenue_display": format_money(revenue),
                "is_active": product.is_active,
            }
        )
    rows.sort(key=lambda row: (-row["revenue"], row["name"].lower()))
    return rows


def build_company_inventory_rows(company):
    stock_rows = list(
        AgentStock.objects.filter(agent__company=company)
        .select_related("agent", "product")
        .order_by("product__name", "agent__name")
    )
    grouped_products = {}
    for stock in stock_rows:
        key = stock.product_id
        grouped_products.setdefault(
            key,
            {
                "product": stock.product,
                "product_name": stock.product.name,
                "size_label": stock.product.size_label,
                "total_units": 0,
                "low_stock_agents": 0,
                "agents": [],
            },
        )
        grouped_products[key]["total_units"] += stock.available_quantity
        grouped_products[key]["low_stock_agents"] += 1 if stock.low_stock else 0
        grouped_products[key]["agents"].append(stock)

    product_rows = sorted(grouped_products.values(), key=lambda row: row["product_name"].lower())
    summary = {
        "tracked_products": len(product_rows),
        "tracked_rows": len(stock_rows),
        "total_units": sum(row["total_units"] for row in product_rows),
        "low_stock_rows": sum(1 for stock in stock_rows if stock.low_stock),
    }
    return product_rows, stock_rows, summary


def get_agent_batch_sale_remaining_cases(sale):
    return (
        InventoryBatch.objects.filter(
            agent=sale.agent,
            product=sale.batch.product,
            batch_number=sale.batch.batch_number,
        ).aggregate(total=Sum("quantity_remaining")).get("total")
        or 0
    )


def build_company_batch_rows(company):
    batches = list(
        company.production_batches.select_related("product", "created_by", "recalled_by")
        .prefetch_related("agent_sales__agent", "agent_sales__payments")
        .order_by("-production_date", "-created_at")
    )
    rows = []
    for batch in batches:
        exposure_sales = [
            sale
            for sale in batch.agent_sales.all()
            if sale.status in {AgentBatchSaleStatus.APPROVED, AgentBatchSaleStatus.RECEIVED}
        ]
        received_sales = [sale for sale in exposure_sales if sale.status == AgentBatchSaleStatus.RECEIVED]
        total_owed = sum(sale.total_amount for sale in exposure_sales)
        total_collected = sum(sale.amount_collected for sale in exposure_sales)
        remaining_with_agents = sum(get_agent_batch_sale_remaining_cases(sale) for sale in received_sales)
        total_received_cases = sum(sale.quantity_received for sale in received_sales)
        rows.append(
            {
                "object": batch,
                "approved_sales_count": len(exposure_sales),
                "cases_sold": batch.cases_sold,
                "total_owed": total_owed,
                "total_collected": total_collected,
                "outstanding_balance": max(total_owed - total_collected, Decimal("0.00")),
                "cash_recovery_rate": batch.cash_recovery_rate,
                "reserved_cases": sum(
                    sale.quantity_approved for sale in exposure_sales if sale.status == AgentBatchSaleStatus.APPROVED
                ),
                "remaining_with_agents": remaining_with_agents,
                "distributed_cases": max(total_received_cases - remaining_with_agents, 0),
                "overdue_count": sum(1 for sale in received_sales if sale.is_overdue),
            }
        )
    return rows


def build_company_batch_detail(batch):
    sales = list(
        batch.agent_sales.select_related("agent", "requested_by", "approved_by").prefetch_related("payments").order_by("-created_at")
    )
    exposure_sales = [
        sale for sale in sales if sale.status in {AgentBatchSaleStatus.APPROVED, AgentBatchSaleStatus.RECEIVED}
    ]
    received_sales = [sale for sale in exposure_sales if sale.status == AgentBatchSaleStatus.RECEIVED]
    sale_rows = []
    for sale in sales:
        remaining_cases = get_agent_batch_sale_remaining_cases(sale) if sale.status == AgentBatchSaleStatus.RECEIVED else 0
        received_cases = sale.quantity_received if sale.status == AgentBatchSaleStatus.RECEIVED else 0
        sale_rows.append(
            {
                "sale": sale,
                "received_cases": received_cases,
                "remaining_cases": remaining_cases,
                "distributed_cases": max(received_cases - remaining_cases, 0),
                "collection_status": sale.collection_status,
            }
        )

    top_agent_map = {}
    for sale in received_sales:
        row = top_agent_map.setdefault(
            sale.agent_id,
            {"agent": sale.agent, "cases": 0, "outstanding": Decimal("0.00")},
        )
        row["cases"] += sale.quantity_received
        row["outstanding"] += sale.outstanding_balance
    top_agents = sorted(top_agent_map.values(), key=lambda row: (-row["cases"], row["agent"].name.lower()))
    total_owed = sum(sale.total_amount for sale in exposure_sales)
    total_collected = sum(sale.amount_collected for sale in exposure_sales)
    remaining_with_agents = sum(row["remaining_cases"] for row in sale_rows if row["sale"].status == AgentBatchSaleStatus.RECEIVED)
    total_received_cases = sum(sale.quantity_received for sale in received_sales)
    metrics = {
        "cases_sold": batch.cases_sold,
        "unsold_cases_remaining": batch.unsold_cases_remaining,
        "recalled_cases": batch.recalled_cases,
        "reserved_cases": sum(
            sale.quantity_approved for sale in exposure_sales if sale.status == AgentBatchSaleStatus.APPROVED
        ),
        "remaining_with_agents": remaining_with_agents,
        "distributed_cases": max(total_received_cases - remaining_with_agents, 0),
        "sales_velocity_per_day": batch.sales_velocity_per_day,
        "cash_recovery_rate": batch.cash_recovery_rate,
        "total_owed": total_owed,
        "total_collected": total_collected,
        "outstanding_balance": max(total_owed - total_collected, Decimal("0.00")),
        "overdue_sales_count": sum(1 for sale in received_sales if sale.is_overdue),
    }
    return metrics, sale_rows, top_agents


def serialize_company_batch_for_audit(batch):
    return {
        "batch_number": batch.batch_number,
        "product_id": batch.product_id,
        "status": batch.status,
        "total_cases_produced": batch.total_cases_produced,
        "unsold_cases_remaining": batch.unsold_cases_remaining,
        "recalled_cases": batch.recalled_cases,
    }


def serialize_agent_batch_sale_for_audit(sale):
    return {
        "batch_number": sale.batch.batch_number,
        "agent_id": sale.agent_id,
        "status": sale.status,
        "payment_type": sale.payment_type,
        "quantity_requested": sale.quantity_requested,
        "quantity_approved": sale.quantity_approved,
        "quantity_received": sale.quantity_received,
        "unit_price": str(sale.unit_price),
        "credit_terms_days": sale.credit_terms_days,
        "credit_due_date": sale.credit_due_date.isoformat() if sale.credit_due_date else "",
        "received_at": sale.received_at.isoformat() if sale.received_at else "",
        "cancelled_at": sale.cancelled_at.isoformat() if sale.cancelled_at else "",
        "outstanding_balance": str(sale.outstanding_balance),
    }


def serialize_agent_batch_sale_payment_for_audit(payment):
    return {
        "sale_id": payment.sale_id,
        "amount": str(payment.amount),
        "status": payment.status,
        "confirmed_at": payment.confirmed_at.isoformat() if payment.confirmed_at else "",
    }


def build_company_report_data(company, date_from, date_to):
    orders = list(
        company.orders.filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
        .select_related("selected_agent")
        .prefetch_related("items", "refund_requests")
        .order_by("-created_at")
    )

    summary = {
        "total_orders": len(orders),
        "delivered_orders": 0,
        "revenue": Decimal("0.00"),
        "units_sold": 0,
        "approved_refunds": Decimal("0.00"),
    }
    product_summary = {}
    agent_summary = {}

    for order in orders:
        if order.selected_agent_id:
            agent_entry = agent_summary.setdefault(
                order.selected_agent_id,
                {
                    "name": order.selected_agent.name,
                    "manager_name": order.selected_agent.admin.full_name if order.selected_agent.admin else "Unassigned",
                    "drivers_count": order.selected_agent.drivers.count(),
                    "delivered_orders": 0,
                    "revenue": Decimal("0.00"),
                },
            )
        else:
            agent_entry = None

        if order.status == OrderStatus.DELIVERED:
            summary["delivered_orders"] += 1
            summary["revenue"] += _safe_decimal(order.total)
            if agent_entry is not None:
                agent_entry["delivered_orders"] += 1
                agent_entry["revenue"] += _safe_decimal(order.total)

        for item in order.items.all():
            product_entry = product_summary.setdefault(
                item.product_id,
                {
                    "name": item.product_name,
                    "size_label": getattr(item.product, "size_label", ""),
                    "stock_total": 0,
                    "units_sold": 0,
                    "revenue": Decimal("0.00"),
                },
            )
            if order.status == OrderStatus.DELIVERED:
                summary["units_sold"] += item.quantity
                product_entry["units_sold"] += item.quantity
                product_entry["revenue"] += _safe_decimal(item.unit_price) * item.quantity

        for refund_request in order.refund_requests.all():
            if refund_request.status == RefundRequestStatus.APPROVED:
                summary["approved_refunds"] += _safe_decimal(refund_request.approved_amount)

    stock_totals = {
        stock["product_id"]: stock["total_units"]
        for stock in AgentStock.objects.filter(agent__company=company)
        .values("product_id")
        .annotate(total_units=Sum("available_quantity"))
    }

    for product_id, row in product_summary.items():
        row["stock_total"] = stock_totals.get(product_id, 0) or 0
        row["revenue_display"] = format_money(row["revenue"])

    feedback_rows = Driver.objects.filter(agent__company=company).values(
        "agent_id"
    ).annotate(average_rating=Avg("delivery_feedback_entries__rating"))
    ratings_by_agent = {
        row["agent_id"]: row["average_rating"]
        for row in feedback_rows
        if row["average_rating"] is not None
    }
    for agent_id, row in agent_summary.items():
        avg_rating = ratings_by_agent.get(agent_id)
        row["average_rating"] = avg_rating
        row["revenue_display"] = format_money(row["revenue"])
        row["average_rating_display"] = f"{avg_rating:.1f} / 5" if avg_rating is not None else "No ratings yet"

    product_rows = sorted(product_summary.values(), key=lambda row: (-row["revenue"], row["name"].lower()))
    agent_rows = sorted(agent_summary.values(), key=lambda row: (-row["revenue"], row["name"].lower()))
    return summary, product_rows, agent_rows, orders


def serialize_user_for_audit(user):
    return {
        "full_name": user.full_name,
        "email": user.email,
        "phone_number": user.phone_number,
        "role": user.role,
        "is_active": user.is_active,
        "wallet_balance": str(user.wallet_balance),
    }


def serialize_company_for_audit(company):
    return {
        "name": company.name,
        "location": company.location,
        "verification_status": company.verification_status,
        "is_active": company.is_active,
        "admin_id": company.admin_id,
        "efda_reference": company.efda_reference,
    }


def serialize_refund_for_audit(refund_request):
    return {
        "order_number": refund_request.order.order_number,
        "status": refund_request.status,
        "payout_method": refund_request.payout_method,
        "requested_amount": str(refund_request.requested_amount),
        "approved_amount": str(refund_request.approved_amount),
        "resolution_note": refund_request.resolution_note,
        "failure_reason": refund_request.failure_reason,
    }


def build_user_company_labels(user):
    labels = set()
    managed_company = user._state.fields_cache.get("managed_company")
    if user.managed_company_id:
        if managed_company is not None:
            labels.add(managed_company.name)
        else:
            labels.add(user.managed_company.name)

    prefetched_managed_companies = getattr(user, "prefetched_managed_companies", None)
    if prefetched_managed_companies is not None:
        labels.update(company.name for company in prefetched_managed_companies if company.name)
    else:
        labels.update(user.managed_companies.values_list("name", flat=True))

    prefetched_managed_agent_branches = getattr(user, "prefetched_managed_agent_branches", None)
    if prefetched_managed_agent_branches is not None:
        labels.update(
            agent.company.name
            for agent in prefetched_managed_agent_branches
            if agent.company_id and agent.company and agent.company.name
        )
    else:
        labels.update(user.managed_agent_branches.values_list("company__name", flat=True))

    if "driver_profile" in user._state.fields_cache:
        driver_profile = user._state.fields_cache["driver_profile"]
    else:
        driver_profile = getattr(user, "driver_profile", None)
    if driver_profile and driver_profile.agent_id:
        labels.add(driver_profile.agent.company.name)

    prefetched_orders = getattr(user, "prefetched_orders", None)
    if prefetched_orders is not None:
        labels.update(order.company.name for order in prefetched_orders if order.company_id and order.company and order.company.name)
    else:
        labels.update(user.orders.values_list("company__name", flat=True))
    return ", ".join(sorted(label for label in labels if label)) or "Platform"


def get_system_user_queryset(request):
    queryset = User.objects.select_related(
        "managed_company",
        "driver_profile__agent__company",
    ).prefetch_related(
        Prefetch(
            "managed_companies",
            queryset=Company.objects.order_by("name"),
            to_attr="prefetched_managed_companies",
        ),
        Prefetch(
            "managed_agent_branches",
            queryset=Agent.objects.select_related("company").order_by("name"),
            to_attr="prefetched_managed_agent_branches",
        ),
        Prefetch(
            "orders",
            queryset=Order.objects.select_related("company").order_by("created_at"),
            to_attr="prefetched_orders",
        ),
    ).order_by("first_name", "last_name", "email")
    role_filter = request.GET.get("role", "").strip()
    status_filter = request.GET.get("status", "").strip()
    company_filter = request.GET.get("company", "").strip()
    search = request.GET.get("search", "").strip()

    if role_filter:
        queryset = queryset.filter(role=role_filter)
    if status_filter == "active":
        queryset = queryset.filter(is_active=True)
    elif status_filter == "inactive":
        queryset = queryset.filter(is_active=False)
    if company_filter:
        queryset = queryset.filter(
            Q(managed_company__pk=company_filter)
            | Q(managed_companies__pk=company_filter)
            | Q(managed_agent_branches__company__pk=company_filter)
            | Q(driver_profile__agent__company__pk=company_filter)
            | Q(orders__company__pk=company_filter)
        )
    if search:
        queryset = queryset.filter(
            Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(email__icontains=search)
            | Q(phone_number__icontains=search)
        )
    return queryset.distinct()


def build_system_report_data(date_from, date_to):
    companies = list(Company.objects.all().prefetch_related("agents", "orders"))
    orders = Order.objects.filter(created_at__date__gte=date_from, created_at__date__lte=date_to).select_related("company")
    users = User.objects.filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
    revenue = (
        orders.filter(status__in=[OrderStatus.DELIVERED, OrderStatus.PAID, OrderStatus.DRIVER_ASSIGNED, OrderStatus.DRIVER_ACCEPTED, OrderStatus.PICKED_UP, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.ARRIVED])
        .aggregate(total=Sum("total"))
        .get("total")
        or Decimal("0.00")
    )

    company_rows = []
    for company in companies:
        company_orders = [order for order in company.orders.all() if date_from <= timezone.localtime(order.created_at).date() <= date_to]
        company_user_ids = set(company.agents.values_list("admin_id", flat=True))
        company_user_ids.discard(None)
        company_user_ids.update(company.agents.values_list("drivers__user_id", flat=True))
        company_user_ids.discard(None)
        company_user_ids.update(company.company_admin_users.values_list("id", flat=True))
        company_user_ids.discard(None)
        if company.admin_id:
            company_user_ids.add(company.admin_id)
        company_rows.append(
            {
                "name": company.name,
                "status": company.get_verification_status_display(),
                "agents": company.agents.count(),
                "users": len(company_user_ids),
                "orders": len(company_orders),
                "revenue": sum((_safe_decimal(order.total) for order in company_orders if order.status == OrderStatus.DELIVERED), Decimal("0.00")),
            }
        )
    company_rows.sort(key=lambda row: (-row["revenue"], row["name"].lower()))
    for row in company_rows:
        row["revenue_display"] = format_money(row["revenue"])

    growth_rows = []
    for role in UserRole:
        growth_rows.append(
            {
                "role": role.value,
                "label": role.label,
                "count": users.filter(role=role.value).count(),
            }
        )

    summary = {
        "companies": Company.objects.count(),
        "verified_companies": Company.objects.filter(is_verified=True).count(),
        "users": User.objects.count(),
        "orders": Order.objects.count(),
        "revenue": revenue,
    }
    return summary, company_rows, growth_rows


def build_audit_log_rows(queryset):
    rows = []
    for log in queryset:
        rows.append(
            {
                "timestamp": timezone.localtime(log.created_at).strftime("%Y-%m-%d %H:%M:%S"),
                "actor": log.actor.full_name if log.actor else "System",
                "action": log.action,
                "entity_type": log.entity_type,
                "entity_id": log.entity_id,
                "entity_label": log.entity_label,
                "ip_address": log.ip_address or "",
                "old_values": json.dumps(log.old_values, ensure_ascii=True),
                "new_values": json.dumps(log.new_values, ensure_ascii=True),
                "object": log,
            }
        )
    return rows


def build_agent_fleet_payload(agent):
    active_statuses = [
        OrderStatus.DRIVER_ASSIGNED,
        OrderStatus.DRIVER_ACCEPTED,
        OrderStatus.PICKED_UP,
        OrderStatus.OUT_FOR_DELIVERY,
        OrderStatus.ARRIVED,
    ]
    active_orders = list(
        agent.accepted_orders.filter(status__in=active_statuses)
        .select_related("customer", "assigned_driver__user")
        .order_by("-updated_at")
    )
    current_order_by_driver_id = {}
    for order in active_orders:
        if order.assigned_driver_id and order.assigned_driver_id not in current_order_by_driver_id:
            current_order_by_driver_id[order.assigned_driver_id] = order

    drivers = list(get_agent_driver_queryset(agent))
    return {
        "agent": {
            "name": agent.name,
            "locationName": agent.location_name,
            "address": agent.address,
            "latitude": float(agent.latitude),
            "longitude": float(agent.longitude),
        },
        "summary": {
            "driverCount": len(drivers),
            "onlineDrivers": sum(1 for driver in drivers if driver.is_online),
            "activeDeliveries": len(active_orders),
        },
        "drivers": [
            {
                "id": driver.pk,
                "name": driver.user.full_name,
                "vehicleIdentifier": driver.vehicle_identifier,
                "latitude": _float_or_none(getattr(getattr(driver.user, "driver_location", None), "latitude", None)),
                "longitude": _float_or_none(getattr(getattr(driver.user, "driver_location", None), "longitude", None)),
                "online": bool(getattr(getattr(driver.user, "driver_location", None), "is_online", False)),
                "activeDeliveries": driver.active_deliveries,
                "completedDeliveries": driver.completed_deliveries,
                "averageRating": float(driver.average_rating) if driver.average_rating is not None else None,
                "currentOrder": (
                    {
                        "orderNumber": current_order_by_driver_id[driver.pk].order_number,
                        "status": current_order_by_driver_id[driver.pk].get_status_display(),
                        "customerName": current_order_by_driver_id[driver.pk].customer.full_name,
                    }
                    if driver.pk in current_order_by_driver_id
                    else None
                ),
            }
            for driver in drivers
        ],
        "orders": [
            {
                "orderNumber": order.order_number,
                "status": order.get_status_display(),
                "customerName": order.customer.full_name,
                "latitude": float(order.latitude),
                "longitude": float(order.longitude),
                "deliveryAddress": order.delivery_address,
                "driverId": order.assigned_driver_id,
            }
            for order in active_orders
        ],
    }


def build_driver_spotlight_rows(drivers, limit=2):
    ranked = sorted(
        drivers,
        key=lambda driver: (
            -(driver.active_deliveries or 0),
            -(driver.average_rating or 0),
            -(driver.completed_deliveries or 0),
            driver.user.full_name.lower(),
        ),
    )
    return ranked[:limit]


def build_weekly_revenue_bars(company):
    today = timezone.localdate()
    start_date = today - timezone.timedelta(days=6)
    delivered_orders = list(
        company.orders.filter(
            status=OrderStatus.DELIVERED,
            delivered_at__date__gte=start_date,
            delivered_at__date__lte=today,
        ).only("delivered_at", "total")
    )
    revenue_by_day = {start_date + timezone.timedelta(days=index): Decimal("0.00") for index in range(7)}
    for order in delivered_orders:
        day = timezone.localtime(order.delivered_at).date() if order.delivered_at else None
        if day in revenue_by_day:
            revenue_by_day[day] += _safe_decimal(order.total)

    max_value = max(revenue_by_day.values(), default=Decimal("0.00"))
    bars = []
    for day, value in revenue_by_day.items():
        percent = int((value / max_value) * 100) if max_value > 0 else 18
        bars.append(
            {
                "label": day.strftime("%a").upper(),
                "value": value,
                "value_display": format_money(value),
                "height": max(percent, 18),
                "is_today": day == today,
            }
        )
    return bars


class RegisterView(FormView):
    template_name = "accounts/register.html"
    form_class = RegistrationForm
    success_url = reverse_lazy("accounts:verify_registration")

    def form_valid(self, form):
        user = form.save()
        try:
            otp = create_registration_otp(user)
        except ValidationError as exc:
            user.delete()
            form.add_error(None, exc.messages[0] if exc.messages else "Unable to send the verification email.")
            return self.form_invalid(form)
        self.request.session["pending_registration_email"] = user.email
        if email_backend_uses_inbox_delivery():
            messages.success(self.request, "Your account was created. Enter the OTP we emailed to finish verification.")
        else:
            messages.warning(
                self.request,
                "Your account was created. Email delivery is not fully configured in this environment yet, so use the OTP shown here: "
                f"{otp.code}",
            )
        return super().form_valid(form)


class VerifyRegistrationOTPView(FormView):
    template_name = "accounts/verify_registration.html"
    form_class = RegistrationOTPForm
    success_url = reverse_lazy("accounts:login")

    def post(self, request, *args, **kwargs):
        ip_limited = is_ratelimited(
            request,
            group="verify_registration_otp_ip",
            key="ip",
            rate="10/m",
            method="POST",
            increment=True,
        )
        if ip_limited:
            form = self.get_form()
            form.add_error(None, "Too many OTP verification attempts. Please wait a minute and try again.")
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def get_initial(self):
        initial = super().get_initial()
        if self.request.session.get("pending_registration_email"):
            initial["email"] = self.request.session["pending_registration_email"]
        elif self.request.GET.get("email"):
            initial["email"] = self.request.GET["email"].lower()
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pending_email = (
            self.request.session.get("pending_registration_email")
            or self.get_initial().get("email")
            or ""
        )
        resend_wait_seconds = 0
        if pending_email:
            user = User.objects.filter(email__iexact=pending_email, role=UserRole.CUSTOMER).first()
            if user:
                resend_wait_seconds = get_registration_resend_wait_seconds(user)
        context["resend_wait_seconds"] = resend_wait_seconds
        return context

    def form_valid(self, form):
        user = get_object_or_404(User, email=form.cleaned_data["email"], role=UserRole.CUSTOMER)
        try:
            verify_registration_otp(user, form.cleaned_data["otp_code"])
        except ValidationError as exc:
            form.add_error("otp_code", exc.messages[0] if exc.messages else "Unable to verify that OTP.")
            return self.form_invalid(form)
        self.request.session.pop("pending_registration_email", None)
        messages.success(self.request, "Your email has been verified. You can now log in.")
        return super().form_valid(form)


class ResendRegistrationOTPView(View):
    def post(self, request):
        email = (request.POST.get("email") or request.session.get("pending_registration_email") or "").lower()
        user = get_object_or_404(User, email=email, role=UserRole.CUSTOMER)
        try:
            otp = create_registration_otp(user)
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to send a fresh OTP right now.")
            return redirect("accounts:verify_registration")
        request.session["pending_registration_email"] = user.email
        if email_backend_uses_inbox_delivery():
            messages.success(request, "A fresh OTP has been sent to your email.")
        else:
            messages.warning(request, f"A fresh OTP was generated for this environment: {otp.code}")
        return redirect("accounts:verify_registration")


class CustomerLoginView(FormView):
    template_name = "accounts/login.html"
    form_class = LoginForm
    success_url = reverse_lazy("accounts:dashboard")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form):
        login(self.request, form.get_user())
        self.request.session["last_activity_at"] = timezone.now().timestamp()
        if not form.cleaned_data.get("remember_me"):
            self.request.session.set_expiry(0)
        next_url = self.request.GET.get("next")
        messages.success(self.request, "Welcome back.")
        return redirect(next_url or get_user_home_url(self.request.user))


class CustomerLogoutView(View):
    def post(self, request):
        logout(request)
        messages.success(request, "You have been logged out.")
        return redirect("accounts:login")


class CustomerDashboardView(LoginRequiredMixin, CustomerRequiredMixin, TemplateView):
    template_name = "accounts/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        orders = self.request.user.orders.all()
        delivered_orders = list(
            orders.filter(status=OrderStatus.DELIVERED)
            .prefetch_related("items")
            .order_by("-updated_at")
        )
        volume_ytd_units = 0
        for order in delivered_orders:
            delivered_day = timezone.localtime(order.delivered_at).date() if order.delivered_at else None
            if delivered_day and delivered_day.year == today.year:
                volume_ytd_units += sum(item.quantity for item in order.items.all())

        loyalty_summary = get_customer_loyalty_summary(self.request.user)
        context["stats"] = {
            "total_orders": orders.count(),
            "active_orders": orders.filter(
                status__in=[
                    OrderStatus.REQUESTED,
                    OrderStatus.PAYMENT_PENDING,
                    OrderStatus.PAID,
                    OrderStatus.DRIVER_ASSIGNED,
                    OrderStatus.DRIVER_ACCEPTED,
                    OrderStatus.PICKED_UP,
                    OrderStatus.OUT_FOR_DELIVERY,
                    OrderStatus.ARRIVED,
                ]
            ).count(),
            "delivered_orders": orders.filter(status=OrderStatus.DELIVERED).count(),
            "total_spent": orders.filter(status=OrderStatus.DELIVERED).aggregate(total=Sum("total"))["total"] or 0,
            "wallet_balance": self.request.user.wallet_balance,
        }
        context["recent_orders"] = orders[:5]
        context["notifications"] = self.request.user.notifications.all()[:8]
        context["payment_history"] = [order.payment for order in orders.select_related("payment") if hasattr(order, "payment")]
        context["loyalty_summary"] = loyalty_summary
        context["saved_addresses"] = self.request.user.saved_addresses.all()[:3]
        context["unread_notifications_count"] = self.request.user.notifications.filter(is_read=False).count()
        context["pending_feedback_orders"] = orders.filter(status=OrderStatus.DELIVERED, feedback__isnull=True)[:5]
        context["active_order"] = orders.filter(
            status__in=[
                OrderStatus.REQUESTED,
                OrderStatus.PAYMENT_PENDING,
                OrderStatus.PAID,
                OrderStatus.DRIVER_ASSIGNED,
                OrderStatus.DRIVER_ACCEPTED,
                OrderStatus.PICKED_UP,
                OrderStatus.OUT_FOR_DELIVERY,
                OrderStatus.ARRIVED,
            ]
        ).order_by("-updated_at").first()
        context["quick_reorders"] = orders.filter(status=OrderStatus.DELIVERED).prefetch_related("items")[:3]
        context["volume_ytd_units"] = volume_ytd_units
        context["customer_tier_label"] = (
            "Premium Partner" if any(item.get("eligible") for item in loyalty_summary) else "Standard Partner"
        )
        return context


class ProfileUpdateView(LoginRequiredMixin, UpdateView):
    model = User
    form_class = CustomerProfileForm
    template_name = "accounts/profile.html"
    success_url = reverse_lazy("accounts:profile")

    def get_object(self, queryset=None):
        return self.request.user

    def form_valid(self, form):
        messages.success(self.request, "Your profile has been updated.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["saved_addresses"] = self.request.user.saved_addresses.all()[:5]
        return context


class CustomerAddressListView(LoginRequiredMixin, CustomerRequiredMixin, TemplateView):
    template_name = "accounts/address_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["address_form"] = CustomerAddressForm(user=self.request.user)
        context["addresses"] = self.request.user.saved_addresses.all()
        return context


class CustomerAddressCreateView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request):
        form = CustomerAddressForm(request.POST, user=request.user)
        if form.is_valid():
            address = form.save(commit=False)
            address.user = request.user
            if not request.user.saved_addresses.exists():
                address.is_default = True
            address.save()
            messages.success(request, f"{address.label} address saved.")
        else:
            messages.error(request, "Unable to save that address. Please review the fields and try again.")
        return redirect("accounts:addresses")


class CustomerAddressUpdateView(LoginRequiredMixin, CustomerRequiredMixin, UpdateView):
    model = CustomerAddress
    form_class = CustomerAddressForm
    template_name = "accounts/address_form.html"
    success_url = reverse_lazy("accounts:addresses")

    def get_queryset(self):
        return self.request.user.saved_addresses.all()

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        messages.success(self.request, "Address updated successfully.")
        return super().form_valid(form)


class CustomerAddressDeleteView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request, pk):
        address = get_object_or_404(request.user.saved_addresses, pk=pk)
        address.delete()
        remaining_addresses = request.user.saved_addresses.order_by("-updated_at")
        if remaining_addresses.exists() and not remaining_addresses.filter(is_default=True).exists():
            next_default = remaining_addresses.first()
            next_default.is_default = True
            next_default.save(update_fields=["is_default", "updated_at"])
        messages.success(request, "Address removed.")
        return redirect("accounts:addresses")


class CustomerAddressSetDefaultView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request, pk):
        address = get_object_or_404(request.user.saved_addresses, pk=pk)
        address.set_as_default()
        messages.success(request, f"{address.label} is now your default address.")
        return redirect("accounts:addresses")


class CustomerNotificationsView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/notifications.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["notifications"] = self.request.user.notifications.all()
        context["unread_count"] = self.request.user.notifications.filter(is_read=False).count()
        return context


class MarkNotificationsReadView(LoginRequiredMixin, View):
    def post(self, request):
        request.user.notifications.filter(is_read=False).update(is_read=True)
        messages.success(request, "Notifications marked as read.")
        return redirect("accounts:notifications")


class AgentManagerDashboardView(AgentManagerRequiredMixin, TemplateView):
    template_name = "accounts/agent_dashboard.html"

    def get_agent(self):
        return get_managed_agent_or_404(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agent = self.get_agent()
        ensure_agent_stock_rows(agent)
        pending_requests = list(agent.order_requests.select_related("order__customer").filter(status="pending"))
        for pending_request in pending_requests:
            expire_order_request_if_needed(pending_request.order)
        pending_requests = agent.order_requests.select_related("order__customer").filter(status="pending")
        context["agent"] = agent
        context["agent_online"] = agent.is_online
        context["pending_requests"] = pending_requests
        context["pending_orders_json_url"] = reverse("accounts:pending_orders_json")
        context["accepted_orders"] = agent.accepted_orders.filter(
            status__in=[
                OrderStatus.PAID,
                OrderStatus.DRIVER_ASSIGNED,
                OrderStatus.DRIVER_ACCEPTED,
                OrderStatus.PICKED_UP,
                OrderStatus.OUT_FOR_DELIVERY,
                OrderStatus.ARRIVED,
            ]
        ).select_related("customer", "assigned_driver__user")[:10]
        context["drivers"] = get_agent_driver_queryset(agent)
        context["assignable_drivers"] = context["drivers"].filter(
            is_active=True,
            user__is_active=True,
            availability_status=Driver.AvailabilityStatus.AVAILABLE,
        )
        context["low_stock_items"] = agent.stocks.select_related("product").filter(
            available_quantity__lte=models.F("reorder_level")
        )
        context["restock_form"] = RestockRequestForm(products=Product.objects.filter(company=agent.company))
        context["batch_request_form"] = AgentBatchSaleRequestForm(agent=agent)
        context["notifications"] = self.request.user.notifications.all()[:8]
        context["recent_inventory_transactions"] = agent.inventory_transactions.select_related("product", "performed_by")[:6]
        context["online_driver_count"] = sum(1 for driver in context["drivers"] if driver.is_online)
        context["pending_refund_count"] = RefundRequest.objects.filter(
            order__selected_agent=agent,
            status=RefundRequestStatus.PENDING,
        ).count()
        context["open_batch_balance"] = get_agent_open_batch_balance(agent)
        today = timezone.localdate()
        context["dashboard_metrics"] = {
            "deliveries_today": agent.accepted_orders.filter(status=OrderStatus.DELIVERED, delivered_at__date=today).count(),
            "active_drivers": context["drivers"].filter(is_active=True, user__is_active=True).count(),
            "stock_alerts": context["low_stock_items"].count(),
        }
        context["warehouse_preview"] = list(agent.stocks.select_related("product").order_by("available_quantity", "product__name")[:3])
        context["fleet_payload"] = build_agent_fleet_payload(agent)
        context["fleet_summary"] = context["fleet_payload"]["summary"]
        context["fleet_json_url"] = reverse("accounts:agent_fleet_json")
        context["top_drivers"] = build_driver_spotlight_rows(list(context["drivers"]))
        return context


class PendingOrdersJsonView(AgentManagerRequiredMixin, View):
    def get(self, request):
        agent = get_object_or_404(Agent, admin=request.user)
        pending_requests = list(agent.order_requests.select_related("order__customer").filter(status="pending"))
        for request_item in pending_requests:
            expire_order_request_if_needed(request_item.order)
        pending_requests = agent.order_requests.select_related("order__customer").filter(status="pending")
        payload = [
            {
                "order_number": request_item.order.order_number,
                "customer": request_item.order.customer.full_name,
                "distance_km": float(request_item.distance_km),
                "total": float(request_item.order.total),
                "deadline": request_item.order.agent_response_deadline.isoformat() if request_item.order.agent_response_deadline else "",
                "address": request_item.order.delivery_address,
                "accept_url": reverse("accounts:accept_request", kwargs={"pk": request_item.pk}),
                "reject_url": reverse("accounts:reject_request", kwargs={"pk": request_item.pk}),
            }
            for request_item in pending_requests
        ]
        return JsonResponse({"orders": payload})


class AgentRequestDecisionView(AgentManagerRequiredMixin, View):
    action = None

    def post(self, request, pk):
        from orders.services import accept_agent_request, reject_agent_request

        agent = get_object_or_404(Agent, admin=request.user)
        agent_request = get_object_or_404(OrderAgentRequest, pk=pk, agent=agent)
        note = request.POST.get("note", "")
        try:
            if self.action == "accept":
                accept_agent_request(agent_request, note=note, accepted_by=request.user)
                messages.success(request, f"{agent_request.order.order_number} accepted successfully.")
            else:
                reject_agent_request(agent_request, note=note)
                messages.info(request, f"{agent_request.order.order_number} rejected.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to update that order request.")
        return redirect("accounts:agent_dashboard")


class AssignDriverView(AgentManagerRequiredMixin, View):
    def post(self, request, order_number):
        agent = get_managed_agent_or_404(request.user)
        order = get_object_or_404(
            Order,
            order_number=order_number,
            selected_agent=agent,
            status__in=[
                OrderStatus.PAID,
                OrderStatus.DRIVER_ASSIGNED,
                OrderStatus.DRIVER_ACCEPTED,
                OrderStatus.PICKED_UP,
                OrderStatus.OUT_FOR_DELIVERY,
                OrderStatus.ARRIVED,
            ],
        )
        driver = get_object_or_404(Driver, pk=request.POST.get("driver_id"), agent=agent)
        was_reassignment = bool(order.assigned_driver_id and order.assigned_driver_id != driver.pk)
        try:
            assign_driver(order, driver)
            if was_reassignment:
                messages.success(request, f"{driver.user.full_name} reassigned to {order.order_number}.")
            else:
                messages.success(request, f"{driver.user.full_name} assigned to {order.order_number}.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to assign that driver.")
        return redirect("accounts:agent_dashboard")


class CreateRestockRequestView(AgentManagerRequiredMixin, View):
    def post(self, request):
        agent = get_managed_agent_or_404(request.user)
        form = RestockRequestForm(request.POST, products=Product.objects.filter(company=agent.company))
        if form.is_valid():
            restock_request = form.save(commit=False)
            restock_request.agent = agent
            restock_request.requested_by = request.user
            restock_request.save()
            for admin_user in get_company_admin_users(agent.company):
                notify_user(
                    admin_user,
                    "Restock request submitted",
                    f"{agent.name} requested more stock for {restock_request.product.name}.",
                    link=reverse("accounts:company_dashboard"),
                )
            messages.success(request, "Restock request submitted.")
        else:
            messages.error(request, "Unable to submit restock request.")
        return redirect("accounts:agent_dashboard")


class AgentBatchSaleRequestCreateView(AgentManagerRequiredMixin, View):
    def post(self, request):
        agent = get_managed_agent_or_404(request.user)
        form = AgentBatchSaleRequestForm(request.POST, agent=agent)
        if form.is_valid():
            cleaned = form.cleaned_data
            try:
                sale = submit_agent_batch_sale_request(
                    agent=agent,
                    batch=cleaned["batch"],
                    requested_by=request.user,
                    quantity_requested=cleaned["quantity_requested"],
                    payment_type=cleaned["payment_type"],
                    requested_upfront_amount=cleaned.get("requested_upfront_amount") or 0,
                    requested_note=cleaned.get("requested_note", ""),
                )
                record_audit_log(
                    request=request,
                    actor=request.user,
                    action="batch_sale.requested",
                    entity_type="agent_batch_sale",
                    entity_id=sale.pk,
                    entity_label=sale.batch.batch_number,
                    new_values=serialize_agent_batch_sale_for_audit(sale),
                )
                messages.success(request, f"Stock request submitted for batch {sale.batch.batch_number}.")
            except ValidationError as exc:
                messages.error(request, exc.messages[0] if exc.messages else "Unable to submit that batch request.")
        else:
            messages.error(request, "Unable to submit that batch request.")
        return redirect(request.POST.get("next") or "accounts:agent_inventory")


class AgentBatchSalePaymentCreateView(AgentManagerRequiredMixin, View):
    def post(self, request, pk):
        agent = get_managed_agent_or_404(request.user)
        sale = get_object_or_404(
            AgentBatchSale,
            pk=pk,
            agent=agent,
            status__in=[AgentBatchSaleStatus.APPROVED, AgentBatchSaleStatus.RECEIVED],
        )
        form = AgentBatchSalePaymentForm(request.POST)
        if form.is_valid():
            try:
                payment = submit_agent_batch_sale_payment(
                    sale=sale,
                    submitted_by=request.user,
                    amount=form.cleaned_data["amount"],
                    note=form.cleaned_data.get("submitted_note", ""),
                )
                record_audit_log(
                    request=request,
                    actor=request.user,
                    action="batch_sale_payment.submitted",
                    entity_type="agent_batch_sale_payment",
                    entity_id=payment.pk,
                    entity_label=sale.batch.batch_number,
                    new_values=serialize_agent_batch_sale_payment_for_audit(payment),
                )
                messages.success(request, "Payment submission sent to the company admin for confirmation.")
            except ValidationError as exc:
                messages.error(request, exc.messages[0] if exc.messages else "Unable to submit that payment.")
        else:
            messages.error(request, "Unable to submit that payment.")
        return redirect("accounts:agent_inventory")


class AgentBatchSaleReceiptConfirmView(AgentManagerRequiredMixin, View):
    def post(self, request, pk):
        agent = get_managed_agent_or_404(request.user)
        sale = get_object_or_404(AgentBatchSale, pk=pk, agent=agent)
        old_values = serialize_agent_batch_sale_for_audit(sale)
        form = AgentBatchSaleReceiptForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Review the receipt note before continuing.")
            return redirect("accounts:agent_inventory")
        try:
            confirm_agent_batch_sale_receipt(
                sale=sale,
                received_by=request.user,
                receipt_note=form.cleaned_data.get("receipt_note", ""),
            )
            sale.refresh_from_db()
            record_audit_log(
                request=request,
                actor=request.user,
                action="agent_batch_sale.received",
                entity_type="agent_batch_sale",
                entity_id=sale.pk,
                entity_label=sale.batch.batch_number,
                old_values=old_values,
                new_values=serialize_agent_batch_sale_for_audit(sale),
            )
            messages.success(request, f"Receipt confirmed for batch {sale.batch.batch_number}.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to confirm that stock receipt.")
        return redirect("accounts:agent_inventory")


class AgentDriverListView(AgentManagerRequiredMixin, TemplateView):
    template_name = "accounts/agent_driver_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agent = get_managed_agent_or_404(self.request.user)
        context["agent"] = agent
        context["drivers"] = get_agent_driver_queryset(agent)
        context["driver_form"] = AgentDriverCreateForm()
        return context


class AgentDriverCreateView(AgentManagerRequiredMixin, View):
    def post(self, request):
        agent = get_managed_agent_or_404(request.user)
        form = AgentDriverCreateForm(request.POST)
        if form.is_valid():
            driver = form.save(agent=agent)
            messages.success(request, f"{driver.user.full_name} was added to {agent.name}.")
        else:
            messages.error(request, "Unable to create the driver account. Please review the fields and try again.")
        return redirect("accounts:agent_drivers")


class AgentDriverUpdateView(AgentManagerRequiredMixin, UpdateView):
    model = Driver
    form_class = AgentDriverUpdateForm
    template_name = "accounts/agent_driver_form.html"

    def get_queryset(self):
        agent = get_managed_agent_or_404(self.request.user)
        return agent.drivers.select_related("user")

    def get_success_url(self):
        return reverse("accounts:agent_drivers")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["agent"] = get_managed_agent_or_404(self.request.user)
        context["driver"] = self.object
        return context

    def form_valid(self, form):
        messages.success(self.request, "Driver details updated successfully.")
        return super().form_valid(form)


class AgentDriverToggleView(AgentManagerRequiredMixin, View):
    def post(self, request, pk):
        agent = get_managed_agent_or_404(request.user)
        driver = get_object_or_404(agent.drivers.select_related("user"), pk=pk)
        target_state = not driver.is_active
        driver.is_active = target_state
        driver.user.is_active = target_state
        driver.user.save(update_fields=["is_active", "updated_at"])
        driver.save(update_fields=["is_active", "updated_at"])
        if target_state:
            messages.success(request, f"{driver.user.full_name} is now active.")
        else:
            messages.warning(request, f"{driver.user.full_name} was deactivated.")
        return redirect("accounts:agent_drivers")


class AgentDriverDeleteView(AgentManagerRequiredMixin, View):
    def post(self, request, pk):
        agent = get_managed_agent_or_404(request.user)
        driver = get_object_or_404(agent.drivers.select_related("user"), pk=pk)
        active_delivery_statuses = [
            OrderStatus.PAID,
            OrderStatus.DRIVER_ASSIGNED,
            OrderStatus.DRIVER_ACCEPTED,
            OrderStatus.PICKED_UP,
            OrderStatus.OUT_FOR_DELIVERY,
            OrderStatus.ARRIVED,
        ]
        if driver.assigned_orders.filter(status__in=active_delivery_statuses).exists():
            messages.error(request, "Reassign this driver's active deliveries before removing the account.")
            return redirect("accounts:agent_drivers")

        has_history = driver.assigned_orders.exists() or driver.delivery_feedback_entries.exists()
        driver_user = driver.user
        if has_history:
            driver.is_active = False
            driver_user.is_active = False
            driver_user.save(update_fields=["is_active", "updated_at"])
            driver.save(update_fields=["is_active", "updated_at"])
            messages.info(
                request,
                f"{driver_user.full_name} has delivery history, so the account was deactivated instead of deleted.",
            )
        else:
            driver.delete()
            driver_user.delete()
            messages.success(request, "Driver account removed completely.")
        return redirect("accounts:agent_drivers")


class AgentDriverDetailView(AgentManagerRequiredMixin, DetailView):
    model = Driver
    template_name = "accounts/agent_driver_detail.html"
    context_object_name = "driver"

    def get_queryset(self):
        agent = get_managed_agent_or_404(self.request.user)
        return get_agent_driver_queryset(agent)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_statuses = [
            OrderStatus.DRIVER_ASSIGNED,
            OrderStatus.DRIVER_ACCEPTED,
            OrderStatus.PICKED_UP,
            OrderStatus.OUT_FOR_DELIVERY,
            OrderStatus.ARRIVED,
        ]
        month_start = timezone.localdate().replace(day=1)
        history_orders = self.object.assigned_orders.select_related("customer", "selected_agent", "feedback").order_by("-created_at")
        delivered_orders = history_orders.filter(status=OrderStatus.DELIVERED)
        failed_orders = history_orders.filter(status=OrderStatus.FAILED)
        context["agent"] = get_managed_agent_or_404(self.request.user)
        context["current_location"] = getattr(self.object.user, "driver_location", None)
        context["active_orders"] = history_orders.filter(status__in=active_statuses)[:5]
        context["recent_orders"] = history_orders[:12]
        context["recent_feedback"] = self.object.delivery_feedback_entries.select_related("order", "customer").exclude(
            rating__isnull=True
        )[:8]
        context["performance"] = {
            "completed_deliveries": delivered_orders.count(),
            "failed_deliveries": failed_orders.count(),
            "active_deliveries": history_orders.filter(status__in=active_statuses).count(),
            "month_completed": delivered_orders.filter(delivered_at__date__gte=month_start).count(),
            "average_rating": self.object.average_rating,
        }
        return context


class AgentFleetView(AgentManagerRequiredMixin, TemplateView):
    template_name = "accounts/agent_fleet.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agent = get_managed_agent_or_404(self.request.user)
        fleet_payload = build_agent_fleet_payload(agent)
        context["agent"] = agent
        context["fleet_payload"] = fleet_payload
        context["fleet_json_url"] = reverse("accounts:agent_fleet_json")
        context["fleet_summary"] = fleet_payload["summary"]
        context["fleet_drivers"] = get_agent_driver_queryset(agent)
        return context


class AgentFleetJsonView(AgentManagerRequiredMixin, View):
    def get(self, request):
        agent = get_managed_agent_or_404(request.user)
        return JsonResponse(build_agent_fleet_payload(agent))


class AgentInventoryView(AgentManagerRequiredMixin, TemplateView):
    template_name = "accounts/agent_inventory.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agent = get_managed_agent_or_404(self.request.user)
        ensure_agent_stock_rows(agent)
        stocks = agent.stocks.select_related("product").order_by("product__name")
        batch_request_form = AgentBatchSaleRequestForm(agent=agent)
        context["agent"] = agent
        context["stocks"] = stocks
        context["adjustment_form"] = AgentInventoryAdjustmentForm(
            products=Product.objects.filter(company=agent.company, is_active=True)
        )
        context["batch_request_form"] = batch_request_form
        context["batch_payment_form"] = AgentBatchSalePaymentForm()
        context["batch_receipt_form"] = AgentBatchSaleReceiptForm()
        context["restock_form"] = RestockRequestForm(products=Product.objects.filter(company=agent.company))
        context["company_products"] = Product.objects.filter(company=agent.company, is_active=True).order_by("name")
        context["available_company_batches"] = list(batch_request_form.fields["batch"].queryset)
        context["inventory_batches"] = agent.inventory_batches.select_related("product")[:12]
        context["restock_requests"] = agent.restock_requests.select_related("product", "requested_by", "approved_by")[:12]
        context["batch_sales"] = agent.batch_sales.select_related(
            "batch__product",
            "approved_by",
            "received_by",
            "cancelled_by",
        ).prefetch_related("payments")[:25]
        context["batch_payments"] = AgentBatchSalePayment.objects.filter(sale__agent=agent).select_related(
            "sale__batch",
            "confirmed_by",
        )[:20]
        context["inventory_transactions"] = agent.inventory_transactions.select_related("product", "performed_by", "batch")[:25]
        context["inventory_metrics"] = {
            "product_count": stocks.count(),
            "total_units": stocks.aggregate(total=Sum("available_quantity"))["total"] or 0,
            "low_stock_count": stocks.filter(available_quantity__lte=models.F("reorder_level")).count(),
            "transaction_count": agent.inventory_transactions.count(),
            "open_batch_balance": get_agent_open_batch_balance(agent),
            "pending_batch_requests": agent.batch_sales.filter(status=AgentBatchSaleStatus.PENDING).count(),
        }
        context["credit_summary"] = {
            "credit_limit": agent.credit_limit,
            "credit_period_days": agent.credit_period_days,
            "outstanding_balance": get_agent_open_batch_balance(agent),
        }
        return context


class AgentOrdersView(AgentManagerRequiredMixin, TemplateView):
    template_name = "accounts/agent_orders.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agent = get_managed_agent_or_404(self.request.user)
        orders = agent.accepted_orders.select_related("customer", "assigned_driver__user").order_by("-created_at")
        selected_status = self.request.GET.get("status", "").strip()
        selected_driver = self.request.GET.get("driver", "").strip()
        date_from = self.request.GET.get("date_from", "").strip()
        date_to = self.request.GET.get("date_to", "").strip()
        if selected_status:
            orders = orders.filter(status=selected_status)
        if selected_driver:
            orders = orders.filter(assigned_driver_id=selected_driver)
        if date_from:
            orders = orders.filter(created_at__date__gte=date_from)
        if date_to:
            orders = orders.filter(created_at__date__lte=date_to)
        context["agent"] = agent
        context["orders"] = orders[:100]
        context["drivers"] = get_agent_driver_queryset(agent)
        context["status_choices"] = OrderStatus.choices
        context["selected_status"] = selected_status
        context["selected_driver"] = selected_driver
        context["date_from"] = date_from
        context["date_to"] = date_to
        return context


class AgentFeedbackView(AgentManagerRequiredMixin, TemplateView):
    template_name = "accounts/agent_feedback.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agent = get_managed_agent_or_404(self.request.user)
        feedback_entries = DeliveryFeedback.objects.filter(driver__agent=agent).select_related(
            "order",
            "customer",
            "driver__user",
        ).order_by("-created_at")
        context["agent"] = agent
        context["feedback_entries"] = feedback_entries[:100]
        context["feedback_summary"] = {
            "count": feedback_entries.exclude(rating__isnull=True).count(),
            "average_rating": feedback_entries.exclude(rating__isnull=True).aggregate(avg=Avg("rating")).get("avg"),
            "skipped": feedback_entries.filter(skipped_at__isnull=False).count(),
        }
        return context


class AgentRefundListView(AgentManagerRequiredMixin, TemplateView):
    template_name = "accounts/agent_refunds.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agent = get_managed_agent_or_404(self.request.user)
        selected_status = self.request.GET.get("status", "").strip()
        refund_requests = RefundRequest.objects.filter(order__selected_agent=agent).select_related(
            "order__customer",
            "requested_by",
            "reviewed_by",
        )
        if selected_status:
            refund_requests = refund_requests.filter(status=selected_status)
        context["agent"] = agent
        context["refund_requests"] = refund_requests
        context["selected_status"] = selected_status
        context["status_choices"] = RefundRequestStatus.choices
        context["refund_summary"] = {
            "pending": RefundRequest.objects.filter(order__selected_agent=agent, status=RefundRequestStatus.PENDING).count(),
            "approved": RefundRequest.objects.filter(order__selected_agent=agent, status=RefundRequestStatus.APPROVED).count(),
            "rejected": RefundRequest.objects.filter(order__selected_agent=agent, status=RefundRequestStatus.REJECTED).count(),
            "processed": RefundRequest.objects.filter(order__selected_agent=agent, status=RefundRequestStatus.PROCESSED).count(),
            "failed": RefundRequest.objects.filter(order__selected_agent=agent, status=RefundRequestStatus.FAILED).count(),
        }
        return context


class AgentRefundDecisionView(AgentManagerRequiredMixin, View):
    action = None

    def post(self, request, pk):
        agent = get_managed_agent_or_404(request.user)
        refund_request = get_object_or_404(RefundRequest, pk=pk, order__selected_agent=agent)
        old_values = serialize_refund_for_audit(refund_request)
        resolution_note = (request.POST.get("resolution_note") or "").strip()
        approved_amount_raw = request.POST.get("approved_amount")
        failure_reason = (request.POST.get("failure_reason") or "").strip()

        if self.action in {"approve", "reject"} and not resolution_note:
            messages.error(request, "A written reason is required for every refund decision.")
            return redirect("accounts:agent_refunds")

        try:
            if self.action == "approve":
                approve_refund_request(
                    refund_request,
                    reviewed_by=request.user,
                    approved_amount=approved_amount_raw or None,
                    resolution_note=resolution_note,
                )
                messages.success(request, f"Refund approved for {refund_request.order.order_number}.")
                audit_action = "refund.approved"
            elif self.action == "process":
                process_refund_request(
                    refund_request,
                    processed_by=request.user,
                )
                messages.success(request, f"Refund processed for {refund_request.order.order_number}.")
                audit_action = "refund.processed"
            elif self.action == "fail":
                if not failure_reason:
                    messages.error(request, "A reason is required when marking refund processing as failed.")
                    return redirect("accounts:agent_refunds")
                process_refund_request(
                    refund_request,
                    processed_by=request.user,
                    failure_reason=failure_reason,
                )
                messages.warning(request, f"Refund processing marked failed for {refund_request.order.order_number}.")
                audit_action = "refund.processing_failed"
            else:
                reject_refund_request(
                    refund_request,
                    reviewed_by=request.user,
                    resolution_note=resolution_note,
                )
                messages.info(request, f"Refund rejected for {refund_request.order.order_number}.")
                audit_action = "refund.rejected"
            record_audit_log(
                request=request,
                actor=request.user,
                action=audit_action,
                entity_type="refund_request",
                entity_id=refund_request.pk,
                entity_label=refund_request.order.order_number,
                old_values=old_values,
                new_values=serialize_refund_for_audit(refund_request),
            )
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to update this refund request.")
        return redirect("accounts:agent_refunds")


class AgentInventoryThresholdUpdateView(AgentManagerRequiredMixin, View):
    def post(self, request, pk):
        agent = get_managed_agent_or_404(request.user)
        stock = get_object_or_404(agent.stocks.select_related("product"), pk=pk)
        form = AgentStockThresholdForm(request.POST, instance=stock)
        if form.is_valid():
            form.save()
            messages.success(request, f"Reorder level updated for {stock.product.name}.")
        else:
            messages.error(request, "Unable to update that reorder level.")
        return redirect("accounts:agent_inventory")


class AgentInventoryAdjustmentCreateView(AgentManagerRequiredMixin, View):
    def post(self, request):
        agent = get_managed_agent_or_404(request.user)
        form = AgentInventoryAdjustmentForm(
            request.POST,
            products=Product.objects.filter(company=agent.company, is_active=True),
        )
        if form.is_valid():
            cleaned = form.cleaned_data
            try:
                apply_agent_inventory_adjustment(
                    agent=agent,
                    product=cleaned["product"],
                    quantity_change=cleaned["quantity_change"],
                    transaction_type=cleaned["transaction_type"],
                    performed_by=request.user,
                    note=cleaned.get("note", ""),
                    batch_number=cleaned.get("batch_number", ""),
                    base_unit_cost=cleaned.get("base_unit_cost") or 0,
                    expires_at=cleaned.get("expires_at"),
                    received_at=cleaned.get("received_at"),
                    reference=f"MANUAL-{agent.pk}",
                )
                messages.success(request, "Inventory adjustment recorded successfully.")
            except ValidationError as exc:
                messages.error(request, exc.messages[0] if exc.messages else "Unable to apply that inventory change.")
        else:
            messages.error(request, "Unable to apply that inventory change. Please review the fields and try again.")
        return redirect("accounts:agent_inventory")


class DriverDashboardView(DriverRequiredMixin, TemplateView):
    template_name = "accounts/driver_dashboard.html"

    def get_driver(self):
        return get_object_or_404(Driver, user=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        driver = self.get_driver()
        assigned_orders = list(driver.assigned_orders.select_related("customer", "selected_agent").order_by("-updated_at")[:10])
        location = getattr(self.request.user, "driver_location", None)
        performance = build_driver_performance(driver)
        active_delivery = get_driver_active_orders(driver).first()
        context["driver"] = driver
        context["assigned_orders"] = assigned_orders
        context["active_delivery"] = active_delivery
        context["location"] = location
        context["driver_online"] = driver.is_online
        context["driver_status_choices"] = driver.AvailabilityStatus.choices
        context["performance"] = performance
        context["driver_map_payload"] = build_driver_dashboard_payload(driver, assigned_orders, location)
        context["reverse_geocode_url"] = reverse("core:reverse_geocode")
        context["notifications"] = self.request.user.notifications.all()[:8]
        context["issue_form"] = DriverIssueReportForm()
        return context


class UpdateDriverAvailabilityView(DriverRequiredMixin, View):
    def post(self, request):
        driver = get_object_or_404(Driver, user=request.user)
        requested_status = (request.POST.get("availability_status") or "").strip()
        valid_statuses = {choice[0] for choice in Driver.AvailabilityStatus.choices}
        if requested_status not in valid_statuses:
            messages.error(request, "Choose a valid driver availability status.")
            return redirect("accounts:driver_dashboard")

        has_active_delivery = get_driver_active_orders(driver).exists()
        if has_active_delivery and requested_status in {
            Driver.AvailabilityStatus.AVAILABLE,
            Driver.AvailabilityStatus.OFF_DUTY,
        }:
            messages.error(request, "You cannot leave delivery mode while an assigned delivery is still active.")
            return redirect("accounts:driver_dashboard")
        if not has_active_delivery and requested_status == Driver.AvailabilityStatus.ON_DELIVERY:
            messages.error(request, "You can only switch to On Delivery when you have an active assigned order.")
            return redirect("accounts:driver_dashboard")

        driver.availability_status = requested_status
        driver.save(update_fields=["availability_status", "updated_at"])
        messages.success(request, f"Availability updated to {driver.get_availability_status_display()}.")
        return redirect("accounts:driver_dashboard")


class DriverHistoryView(DriverRequiredMixin, TemplateView):
    template_name = "accounts/driver_history.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        driver = get_object_or_404(Driver, user=self.request.user)
        history_orders = driver.assigned_orders.select_related("customer", "selected_agent", "feedback").order_by("-created_at")
        status = self.request.GET.get("status", "").strip()
        date_from = self.request.GET.get("date_from", "").strip()
        date_to = self.request.GET.get("date_to", "").strip()
        if status:
            history_orders = history_orders.filter(status=status)
        if date_from:
            history_orders = history_orders.filter(created_at__date__gte=date_from)
        if date_to:
            history_orders = history_orders.filter(created_at__date__lte=date_to)
        context["driver"] = driver
        context["orders"] = history_orders[:100]
        context["performance"] = build_driver_performance(driver)
        context["status_choices"] = OrderStatus.choices
        context["selected_status"] = status
        context["date_from"] = date_from
        context["date_to"] = date_to
        return context


class UpdateDriverLocationView(DriverRequiredMixin, View):
    def post(self, request):
        form = DriverLocationForm(request.POST)
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        if form.is_valid():
            DriverLocation.objects.update_or_create(
                driver_user=request.user,
                defaults=form.cleaned_data,
            )
            if is_ajax:
                return JsonResponse({"ok": True})
            messages.success(request, "Driver location updated successfully.")
            return redirect("accounts:driver_dashboard")
        if is_ajax:
            return JsonResponse({"ok": False, "errors": form.errors}, status=400)
        messages.error(request, "Unable to update driver location.")
        return redirect("accounts:driver_dashboard")


class DriverStartDeliveryView(DriverRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(Order, order_number=order_number)
        try:
            start_delivery(order, request.user)
            messages.success(request, f"Started delivery for {order.order_number}.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to start that delivery.")
        return redirect("accounts:driver_dashboard")


class DriverAcceptDeliveryView(DriverRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(Order, order_number=order_number)
        try:
            accept_delivery_assignment(order, request.user)
            messages.success(request, f"Accepted delivery for {order.order_number}.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to accept that delivery.")
        return redirect("accounts:driver_dashboard")


class DriverPickedUpView(DriverRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(Order, order_number=order_number)
        try:
            mark_order_picked_up(order, request.user)
            messages.success(request, f"Marked {order.order_number} as picked up.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to update pickup status.")
        return redirect("accounts:driver_dashboard")


class DriverArrivedView(DriverRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(Order, order_number=order_number)
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        try:
            mark_order_arrived(order, request.user)
            if is_ajax:
                return JsonResponse({"ok": True, "status": order.status, "label": order.get_status_display()})
            messages.success(request, f"Marked {order.order_number} as arrived.")
        except ValidationError as exc:
            if is_ajax:
                return JsonResponse(
                    {"ok": False, "message": exc.messages[0] if exc.messages else "Unable to mark the order as arrived."},
                    status=400,
                )
            messages.error(request, exc.messages[0] if exc.messages else "Unable to mark the order as arrived.")
        return redirect("accounts:driver_dashboard")


class DriverReportIssueView(DriverRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(Order, order_number=order_number)
        form = DriverIssueReportForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Please choose an issue type before submitting the report.")
            return redirect("accounts:driver_dashboard")

        try:
            report_delivery_issue(
                order,
                request.user,
                form.cleaned_data["issue_type"],
                form.cleaned_data["description"],
            )
            messages.warning(request, f"Reported a delivery issue for {order.order_number}.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to report that issue.")
        return redirect("accounts:driver_dashboard")


class DriverCompleteDeliveryView(DriverRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(Order, order_number=order_number)
        try:
            complete_delivery_and_deduct_stock(
                order,
                otp_code=request.POST.get("otp_code", ""),
                qr_token=request.POST.get("qr_token", ""),
                verified_by_user=request.user,
            )
            messages.success(request, f"Completed delivery for {order.order_number}.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to complete that delivery.")
        return redirect("accounts:driver_dashboard")


class DriverConfirmQRView(DriverRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(Order, order_number=order_number)
        raw_payload = request.POST.get("qr_payload", "")
        if request.content_type and "application/json" in request.content_type:
            try:
                raw_payload = json.loads(request.body.decode("utf-8")).get("qr_payload", raw_payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raw_payload = ""

        try:
            confirm_delivery_by_qr(order, raw_payload, request.user)
        except QRConfirmationError as exc:
            return JsonResponse({"error": exc.code, "message": exc.message}, status=422)
        except ValidationError as exc:
            return JsonResponse(
                {"error": "INVALID_STATE", "message": exc.messages[0] if exc.messages else "Unable to confirm that QR code."},
                status=400,
            )
        return JsonResponse({"status": "confirmed"})


class DriverConfirmQRBatchView(DriverRequiredMixin, View):
    def post(self, request):
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"error": "INVALID_PAYLOAD", "message": "Submit a valid JSON batch payload."}, status=400)

        scan_items = payload.get("scans", [])
        if not isinstance(scan_items, list):
            return JsonResponse({"error": "INVALID_PAYLOAD", "message": "The scans payload must be a list."}, status=400)
        return JsonResponse({"results": submit_queued_qr_scans(scan_items, request.user)})


class CompanyAdminDashboardView(CompanyAdminRequiredMixin, TemplateView):
    template_name = "accounts/company_dashboard.html"

    def get_company(self):
        return get_managed_company_or_404(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = self.get_company()
        context["managed_companies"] = list(get_managed_company_queryset(self.request.user))
        agents = list(company.agents.select_related("admin"))
        drivers = list(
            Driver.objects.filter(agent__company=company).select_related("user", "agent", "user__driver_location")
        )
        context["company"] = company
        context["agents"] = agents
        context["drivers"] = drivers
        context["orders"] = company.orders.select_related("customer", "selected_agent", "assigned_driver__user")[:15]
        context["restock_requests"] = RestockRequest.objects.filter(agent__company=company).select_related("agent", "product", "requested_by")[:15]
        context["payment_schedules"] = PaymentSchedule.objects.filter(restock_request__agent__company=company).select_related("restock_request__agent", "restock_request__product")[:15]
        context["inventory_batches"] = InventoryBatch.objects.filter(agent__company=company).select_related("agent", "product")[:15]
        context["batch_form"] = CompanyBatchForm(company=company)
        context["company_batches"] = build_company_batch_rows(company)[:12]
        context["pending_batch_sales"] = AgentBatchSale.objects.filter(
            agent__company=company,
            status=AgentBatchSaleStatus.PENDING,
        ).select_related("agent", "batch__product", "requested_by")[:12]
        context["pending_batch_payments"] = AgentBatchSalePayment.objects.filter(
            sale__agent__company=company,
            status=AgentBatchSalePaymentStatus.PENDING,
        ).select_related("sale__agent", "sale__batch", "submitted_by")[:12]
        context["refund_requests"] = RefundRequest.objects.filter(order__company=company).select_related("order", "requested_by", "reviewed_by")[:15]
        context["agent_form"] = AgentForm(company=company)
        context["agent_manager_form"] = InternalUserCreationForm(allowed_roles=(UserRole.AGENT_MANAGER,))
        context["driver_user_form"] = InternalUserCreationForm(allowed_roles=(UserRole.DRIVER,))
        context["driver_form"] = DriverForm(company=company)
        context["premium_form"] = CompanyPremiumSettingsForm(instance=company)
        context["company_map_payload"] = build_company_map_payload(company, agents, drivers)
        context["company_locations_url"] = reverse("accounts:company_locations_json")
        context["notifications"] = self.request.user.notifications.all()[:8]
        top_product = build_company_product_rows(company)[:1]
        agent_rows = build_company_agent_rows(company)
        delivered_orders = company.orders.filter(status=OrderStatus.DELIVERED)
        revenue_total = delivered_orders.aggregate(total=Sum("total")).get("total") or Decimal("0.00")
        context["company_metrics"] = {
            "orders": company.orders.count(),
            "revenue": revenue_total,
            "revenue_display": format_money(revenue_total),
            "top_product_name": top_product[0]["name"] if top_product else "No product data",
            "top_agent_name": agent_rows[0]["name"] if agent_rows else "No agent data",
            "agent_count": len(agents),
        }
        context["weekly_revenue_bars"] = build_weekly_revenue_bars(company)
        context["top_agent_rows"] = agent_rows[:5]
        context["live_fleet_summary"] = {
            "drivers_total": len(drivers),
            "drivers_online": sum(1 for driver in drivers if driver.is_online),
            "deliveries_active": company.orders.filter(
                status__in=[
                    OrderStatus.DRIVER_ASSIGNED,
                    OrderStatus.DRIVER_ACCEPTED,
                    OrderStatus.PICKED_UP,
                    OrderStatus.OUT_FOR_DELIVERY,
                    OrderStatus.ARRIVED,
                ]
            ).count(),
        }
        return context


class CompanyLocationsJsonView(CompanyAdminRequiredMixin, View):
    def get(self, request):
        company = get_managed_company_or_404(request.user)
        agents = list(company.agents.select_related("admin"))
        drivers = list(
            Driver.objects.filter(agent__company=company).select_related("user", "agent", "user__driver_location")
        )
        return JsonResponse(build_company_map_payload(company, agents, drivers))


class CompanyProductListView(CompanyAdminRequiredMixin, TemplateView):
    template_name = "accounts/company_products.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = get_managed_company_or_404(self.request.user)
        context["company"] = company
        context["products"] = build_company_product_rows(company)
        context["product_form"] = kwargs.get("product_form") or CompanyProductForm(company=company)
        context["available_batch_count"] = company.production_batches.filter(
            status=CompanyBatchStatus.AVAILABLE,
            unsold_cases_remaining__gt=0,
        ).count()
        return context


class CompanyProductCreateView(CompanyAdminRequiredMixin, View):
    def post(self, request):
        company = get_managed_company_or_404(request.user)
        form = CompanyProductForm(request.POST, request.FILES, company=company)
        if form.is_valid():
            product = form.save(commit=False)
            product.company = company
            product.save()
            ensure_product_stock_rows(product)
            messages.success(request, f"{product.name} was added to your catalog.")
        else:
            messages.error(request, "Unable to add that product. Please review the entered details.")
        return redirect("accounts:company_products")


class CompanyProductStarterSeedView(CompanyAdminRequiredMixin, View):
    def post(self, request):
        company = get_managed_company_or_404(request.user)
        created_products, created_batches = create_company_starter_catalog(company=company, created_by=request.user)
        if created_products or created_batches:
            messages.success(
                request,
                f"Starter catalog ready: {len(created_products)} products and {len(created_batches)} sellable batches were added.",
            )
        else:
            messages.info(
                request,
                "Starter products already exist for this company. You can edit them or create more production batches from inventory.",
            )
        return redirect(request.POST.get("next") or "accounts:company_products")


class CompanyProductUpdateView(CompanyAdminRequiredMixin, UpdateView):
    template_name = "accounts/company_product_form.html"
    form_class = CompanyProductForm
    context_object_name = "product"

    def get_queryset(self):
        return Product.objects.filter(company=get_managed_company_or_404(self.request.user))

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = get_managed_company_or_404(self.request.user)
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"{self.object.name} was updated.")
        return response

    def form_invalid(self, form):
        messages.error(self.request, "Unable to update that product.")
        return super().form_invalid(form)

    def get_success_url(self):
        return reverse("accounts:company_products")


class CompanyProductToggleView(CompanyAdminRequiredMixin, View):
    def post(self, request, pk):
        company = get_managed_company_or_404(request.user)
        product = get_object_or_404(Product, pk=pk, company=company)
        product.is_active = not product.is_active
        product.save(update_fields=["is_active", "updated_at"])
        messages.success(
            request,
            f"{product.name} is now {'active' if product.is_active else 'inactive'} in your catalog.",
        )
        return redirect("accounts:company_products")


class CompanyAgentListView(CompanyAdminRequiredMixin, TemplateView):
    template_name = "accounts/company_agents.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = get_managed_company_or_404(self.request.user)
        context["company"] = company
        context["agents"] = build_company_agent_rows(company)
        context["agent_form"] = kwargs.get("agent_form") or AgentForm(company=company)
        return context


class CompanyAgentUpdateView(CompanyAdminRequiredMixin, UpdateView):
    template_name = "accounts/company_agent_form.html"
    form_class = CompanyAgentUpdateForm
    context_object_name = "agent"

    def get_queryset(self):
        return Agent.objects.filter(company=get_managed_company_or_404(self.request.user)).select_related("admin")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = get_managed_company_or_404(self.request.user)
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        ensure_agent_stock_rows(self.object)
        messages.success(self.request, f"{self.object.name} was updated.")
        return response

    def form_invalid(self, form):
        messages.error(self.request, "Unable to update that agent branch.")
        return super().form_invalid(form)

    def get_success_url(self):
        return reverse("accounts:company_agents")


class CompanyAgentToggleView(CompanyAdminRequiredMixin, View):
    def post(self, request, pk):
        company = get_managed_company_or_404(request.user)
        agent = get_object_or_404(Agent, pk=pk, company=company)
        agent.is_active = not agent.is_active
        agent.is_accepting_orders = agent.is_active
        agent.save(update_fields=["is_active", "is_accepting_orders", "updated_at"])
        messages.success(
            request,
            f"{agent.name} is now {'active' if agent.is_active else 'inactive'}.",
        )
        return redirect("accounts:company_agents")


class CompanyAgentDeleteView(CompanyAdminRequiredMixin, View):
    def post(self, request, pk):
        company = get_managed_company_or_404(request.user)
        agent = get_object_or_404(Agent, pk=pk, company=company)
        has_dependencies = any(
            (
                agent.drivers.exists(),
                agent.accepted_orders.exists(),
                agent.stocks.exists(),
                agent.restock_requests.exists(),
                agent.inventory_batches.exists(),
                agent.inventory_transactions.exists(),
            )
        )
        if has_dependencies:
            messages.warning(request, "This agent already has operational records. Deactivate it instead of deleting it.")
            return redirect("accounts:company_agents")
        agent.delete()
        messages.success(request, "Agent branch removed.")
        return redirect("accounts:company_agents")


class CompanyInventoryView(CompanyAdminRequiredMixin, TemplateView):
    template_name = "accounts/company_inventory.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = get_managed_company_or_404(self.request.user)
        product_rows, stock_rows, summary = build_company_inventory_rows(company)
        context["company"] = company
        context["product_rows"] = product_rows
        context["stock_rows"] = stock_rows
        context["inventory_summary"] = summary
        context["batch_form"] = kwargs.get("batch_form") or CompanyBatchForm(company=company)
        context["batch_sale_approval_form"] = AgentBatchSaleApprovalForm()
        context["batch_sale_cancellation_form"] = AgentBatchSaleCancellationForm()
        context["products"] = company.products.filter(is_active=True).order_by("name")
        context["company_batches"] = build_company_batch_rows(company)
        context["approval_today"] = timezone.localdate()
        context["pending_batch_sales"] = AgentBatchSale.objects.filter(
            agent__company=company,
            status=AgentBatchSaleStatus.PENDING,
        ).select_related("agent", "batch__product", "requested_by")
        context["active_batch_sales"] = AgentBatchSale.objects.filter(
            agent__company=company,
            status__in=[AgentBatchSaleStatus.APPROVED, AgentBatchSaleStatus.RECEIVED],
        ).select_related("agent", "batch__product", "requested_by", "received_by", "cancelled_by").prefetch_related("payments")
        context["pending_batch_payments"] = AgentBatchSalePayment.objects.filter(
            sale__agent__company=company,
            status=AgentBatchSalePaymentStatus.PENDING,
        ).select_related("sale__agent", "sale__batch", "submitted_by")
        context["has_active_products"] = company.products.filter(is_active=True).exists()
        context["has_available_batches"] = company.production_batches.filter(
            status=CompanyBatchStatus.AVAILABLE,
            unsold_cases_remaining__gt=0,
        ).exists()
        return context


class CompanyBatchCreateView(CompanyAdminRequiredMixin, View):
    def post(self, request):
        company = get_managed_company_or_404(request.user)
        form = CompanyBatchForm(request.POST, company=company)
        if form.is_valid():
            batch = form.save(commit=False)
            batch.company = company
            batch.created_by = request.user
            batch.save()
            record_audit_log(
                request=request,
                actor=request.user,
                action="company_batch.created",
                entity_type="company_batch",
                entity_id=batch.pk,
                entity_label=batch.batch_number,
                new_values=serialize_company_batch_for_audit(batch),
            )
            messages.success(request, f"Batch {batch.batch_number} is now available for agent requests.")
        else:
            messages.error(request, "Unable to create that production batch.")
        return redirect(request.POST.get("next") or "accounts:company_inventory")


class CompanyBatchDetailView(CompanyAdminRequiredMixin, DetailView):
    template_name = "accounts/company_batch_detail.html"
    context_object_name = "batch"

    def get_queryset(self):
        return CompanyBatch.objects.filter(company=get_managed_company_or_404(self.request.user)).select_related(
            "product",
            "created_by",
            "recalled_by",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        metrics, sale_rows, top_agents = build_company_batch_detail(self.object)
        context["company"] = get_managed_company_or_404(self.request.user)
        context["metrics"] = metrics
        context["sale_rows"] = sale_rows
        context["top_agents"] = top_agents
        context["recall_form"] = BatchRecallForm()
        return context


class CompanyBatchRecallView(CompanyAdminRequiredMixin, View):
    def post(self, request, pk):
        company = get_managed_company_or_404(request.user)
        batch = get_object_or_404(CompanyBatch, pk=pk, company=company)
        old_values = serialize_company_batch_for_audit(batch)
        form = BatchRecallForm(request.POST)
        if form.is_valid():
            try:
                recall_company_batch(batch=batch, recalled_by=request.user, reason=form.cleaned_data["reason"])
                batch.refresh_from_db()
                record_audit_log(
                    request=request,
                    actor=request.user,
                    action="company_batch.recalled",
                    entity_type="company_batch",
                    entity_id=batch.pk,
                    entity_label=batch.batch_number,
                    old_values=old_values,
                    new_values=serialize_company_batch_for_audit(batch),
                )
                messages.warning(request, f"Recall triggered for batch {batch.batch_number}.")
            except ValidationError as exc:
                messages.error(request, exc.messages[0] if exc.messages else "Unable to recall that batch.")
        else:
            messages.error(request, "Provide a recall reason before continuing.")
        return redirect("accounts:company_batch_detail", pk=pk)


class CompanyBatchSaleDecisionView(CompanyAdminRequiredMixin, View):
    action = None

    def post(self, request, pk):
        company = get_managed_company_or_404(request.user)
        sale = get_object_or_404(AgentBatchSale, pk=pk, agent__company=company)
        old_values = serialize_agent_batch_sale_for_audit(sale)
        if self.action == "approve":
            form = AgentBatchSaleApprovalForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Review the approval fields before continuing.")
                return redirect(request.POST.get("next") or "accounts:company_inventory")
            try:
                approve_agent_batch_sale(
                    sale=sale,
                    approved_by=request.user,
                    quantity_approved=form.cleaned_data["quantity_approved"],
                    unit_price=form.cleaned_data["unit_price"],
                    initial_payment_amount=form.cleaned_data.get("initial_payment_amount") or 0,
                    credit_terms_days=form.cleaned_data.get("credit_terms_days"),
                    decision_note=form.cleaned_data.get("decision_note", ""),
                )
                sale.refresh_from_db()
                record_audit_log(
                    request=request,
                    actor=request.user,
                    action="agent_batch_sale.approved",
                    entity_type="agent_batch_sale",
                    entity_id=sale.pk,
                    entity_label=sale.batch.batch_number,
                    old_values=old_values,
                    new_values=serialize_agent_batch_sale_for_audit(sale),
                )
                messages.success(request, f"Approved stock request from {sale.agent.name} and awaiting receipt confirmation.")
            except ValidationError as exc:
                messages.error(request, exc.messages[0] if exc.messages else "Unable to approve that stock request.")
        else:
            decision_note = (request.POST.get("decision_note") or "").strip()
            try:
                reject_agent_batch_sale(sale=sale, reviewed_by=request.user, decision_note=decision_note)
                sale.refresh_from_db()
                record_audit_log(
                    request=request,
                    actor=request.user,
                    action="agent_batch_sale.rejected",
                    entity_type="agent_batch_sale",
                    entity_id=sale.pk,
                    entity_label=sale.batch.batch_number,
                    old_values=old_values,
                    new_values=serialize_agent_batch_sale_for_audit(sale),
                )
                messages.info(request, f"Rejected stock request from {sale.agent.name}.")
            except ValidationError as exc:
                messages.error(request, exc.messages[0] if exc.messages else "Unable to reject that stock request.")
        return redirect(request.POST.get("next") or "accounts:company_inventory")


class CompanyBatchSaleCancelView(CompanyAdminRequiredMixin, View):
    def post(self, request, pk):
        company = get_managed_company_or_404(request.user)
        sale = get_object_or_404(AgentBatchSale, pk=pk, agent__company=company)
        old_values = serialize_agent_batch_sale_for_audit(sale)
        form = AgentBatchSaleCancellationForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Provide a cancellation reason before continuing.")
            return redirect(request.POST.get("next") or "accounts:company_inventory")
        try:
            cancel_agent_batch_sale(
                sale=sale,
                cancelled_by=request.user,
                reason=form.cleaned_data["reason"],
            )
            sale.refresh_from_db()
            record_audit_log(
                request=request,
                actor=request.user,
                action="agent_batch_sale.cancelled",
                entity_type="agent_batch_sale",
                entity_id=sale.pk,
                entity_label=sale.batch.batch_number,
                old_values=old_values,
                new_values=serialize_agent_batch_sale_for_audit(sale),
            )
            messages.warning(request, f"Cancelled batch sale for {sale.agent.name}.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to cancel that batch sale.")
        return redirect(request.POST.get("next") or "accounts:company_inventory")


class CompanyBatchSalePaymentDecisionView(CompanyAdminRequiredMixin, View):
    action = None

    def post(self, request, pk):
        company = get_managed_company_or_404(request.user)
        payment = get_object_or_404(AgentBatchSalePayment, pk=pk, sale__agent__company=company)
        old_values = serialize_agent_batch_sale_payment_for_audit(payment)
        try:
            if self.action == "confirm":
                confirm_agent_batch_sale_payment(payment=payment, confirmed_by=request.user)
                payment.refresh_from_db()
                audit_action = "agent_batch_sale_payment.confirmed"
                success_message = f"Confirmed payment for batch {payment.sale.batch.batch_number}."
            else:
                rejection_reason = (request.POST.get("rejection_reason") or "").strip()
                reject_agent_batch_sale_payment(
                    payment=payment,
                    confirmed_by=request.user,
                    rejection_reason=rejection_reason,
                )
                payment.refresh_from_db()
                audit_action = "agent_batch_sale_payment.rejected"
                success_message = f"Rejected payment for batch {payment.sale.batch.batch_number}."
            record_audit_log(
                request=request,
                actor=request.user,
                action=audit_action,
                entity_type="agent_batch_sale_payment",
                entity_id=payment.pk,
                entity_label=payment.sale.batch.batch_number,
                old_values=old_values,
                new_values=serialize_agent_batch_sale_payment_for_audit(payment),
            )
            messages.success(request, success_message)
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to review that payment submission.")
        return redirect(request.POST.get("next") or "accounts:company_inventory")


class CompanyReportsView(CompanyAdminRequiredMixin, TemplateView):
    template_name = "accounts/company_reports.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = get_managed_company_or_404(self.request.user)
        date_from, date_to = get_company_reporting_window(self.request)
        summary, product_rows, agent_rows, orders = build_company_report_data(company, date_from, date_to)
        context["company"] = company
        context["summary"] = summary
        context["product_rows"] = product_rows
        context["agent_rows"] = agent_rows
        context["orders"] = orders[:20]
        context["date_from"] = date_from
        context["date_to"] = date_to
        context["date_label"] = f"{date_from.isoformat()} to {date_to.isoformat()}"
        context["revenue_display"] = format_money(summary["revenue"])
        context["refunds_display"] = format_money(summary["approved_refunds"])
        query_string = f"date_from={date_from.isoformat()}&date_to={date_to.isoformat()}"
        context["excel_export_url"] = f"{reverse('accounts:company_reports_export', kwargs={'export_format': 'excel'})}?{query_string}"
        context["pdf_export_url"] = f"{reverse('accounts:company_reports_export', kwargs={'export_format': 'pdf'})}?{query_string}"
        return context


class CompanyReportExportView(CompanyAdminRequiredMixin, View):
    def get(self, request, export_format):
        company = get_managed_company_or_404(request.user)
        date_from, date_to = get_company_reporting_window(request)
        summary, product_rows, agent_rows, _ = build_company_report_data(company, date_from, date_to)
        date_label = f"{date_from.isoformat()} to {date_to.isoformat()}"
        filename_stem = f"{company.slug or company.name.lower().replace(' ', '-')}-report-{date_to.isoformat()}"

        if export_format == "excel":
            payload = build_company_report_excel(
                company_name=company.name,
                date_label=date_label,
                summary=summary,
                product_rows=product_rows,
                agent_rows=agent_rows,
            )
            response = HttpResponse(payload, content_type="application/vnd.ms-excel")
            response["Content-Disposition"] = f'attachment; filename="{filename_stem}.xls"'
            return response

        if export_format == "pdf":
            payload = build_company_report_pdf(
                company_name=company.name,
                date_label=date_label,
                summary=summary,
                product_rows=product_rows,
                agent_rows=agent_rows,
            )
            response = HttpResponse(payload, content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="{filename_stem}.pdf"'
            return response

        return HttpResponse("Unsupported export format.", status=400)


class CompanyAIInsightsView(CompanyAdminRequiredMixin, TemplateView):
    template_name = "accounts/company_ai_insights.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["company"] = get_managed_company_or_404(self.request.user)
        return context


class CreateAgentManagerView(CompanyAdminRequiredMixin, View):
    role = UserRole.AGENT_MANAGER

    def post(self, request):
        form = InternalUserCreationForm(request.POST, allowed_roles=(self.role,))
        if form.is_valid():
            form.save()
            messages.success(request, f"{UserRole(self.role).label} account created.")
        else:
            messages.error(request, "Unable to create the account.")
        return redirect("accounts:company_dashboard")


class CreateAgentView(CompanyAdminRequiredMixin, View):
    def post(self, request):
        company = get_managed_company_or_404(request.user)
        form = AgentForm(request.POST, company=company)
        if form.is_valid():
            agent = form.save(commit=False)
            agent.company = company
            agent.save()
            ensure_agent_stock_rows(agent)
            messages.success(request, f"{agent.name} created successfully.")
        else:
            messages.error(request, "Unable to create the agent branch.")
        return redirect(request.POST.get("next") or "accounts:company_dashboard")


class CreateDriverUserView(CompanyAdminRequiredMixin, View):
    def post(self, request):
        form = InternalUserCreationForm(request.POST, allowed_roles=(UserRole.DRIVER,))
        if form.is_valid():
            form.save()
            messages.success(request, "Driver user account created successfully.")
        else:
            messages.error(request, "Unable to create the driver user account.")
        return redirect("accounts:company_dashboard")


class CreateDriverView(CompanyAdminRequiredMixin, View):
    def post(self, request):
        company = get_managed_company_or_404(request.user)
        form = DriverForm(request.POST, company=company)
        if form.is_valid():
            driver = form.save(commit=False)
            if driver.agent.company_id != company.id:
                messages.error(request, "Driver must belong to one of your company agents.")
                return redirect("accounts:company_dashboard")
            driver.save()
            messages.success(request, "Driver record created successfully.")
        else:
            messages.error(request, "Unable to create the driver record.")
        return redirect("accounts:company_dashboard")


class UpdateCompanyPremiumSettingsView(CompanyAdminRequiredMixin, View):
    def post(self, request):
        company = get_managed_company_or_404(request.user)
        form = CompanyPremiumSettingsForm(request.POST, instance=company)
        if form.is_valid():
            form.save()
            messages.success(request, "Premium loyalty settings updated.")
        else:
            messages.error(request, "Unable to update premium loyalty settings.")
        return redirect("accounts:company_dashboard")


class CompanyRefundDecisionView(CompanyAdminRequiredMixin, View):
    action = None

    def post(self, request, pk):
        company = get_managed_company_or_404(request.user)
        refund_request = get_object_or_404(RefundRequest, pk=pk, order__company=company)
        old_values = serialize_refund_for_audit(refund_request)
        resolution_note = request.POST.get("resolution_note", "")
        approved_amount_raw = request.POST.get("approved_amount")
        approved_amount = approved_amount_raw or None
        failure_reason = (request.POST.get("failure_reason") or "").strip()
        try:
            if self.action == "approve":
                approve_refund_request(
                    refund_request,
                    reviewed_by=request.user,
                    approved_amount=approved_amount,
                    resolution_note=resolution_note,
                )
                messages.success(request, f"Refund approved for {refund_request.order.order_number}.")
                audit_action = "refund.approved"
            elif self.action == "process":
                process_refund_request(
                    refund_request,
                    processed_by=request.user,
                )
                messages.success(request, f"Refund processed for {refund_request.order.order_number}.")
                audit_action = "refund.processed"
            elif self.action == "fail":
                if not failure_reason:
                    messages.error(request, "A reason is required when marking refund processing as failed.")
                    return redirect("accounts:company_dashboard")
                process_refund_request(
                    refund_request,
                    processed_by=request.user,
                    failure_reason=failure_reason,
                )
                messages.warning(request, f"Refund processing marked failed for {refund_request.order.order_number}.")
                audit_action = "refund.processing_failed"
            else:
                reject_refund_request(
                    refund_request,
                    reviewed_by=request.user,
                    resolution_note=resolution_note,
                )
                messages.info(request, f"Refund request rejected for {refund_request.order.order_number}.")
                audit_action = "refund.rejected"
            record_audit_log(
                request=request,
                actor=request.user,
                action=audit_action,
                entity_type="refund_request",
                entity_id=refund_request.pk,
                entity_label=refund_request.order.order_number,
                old_values=old_values,
                new_values=serialize_refund_for_audit(refund_request),
            )
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to update this refund request.")
        return redirect("accounts:company_dashboard")


class ApproveRestockRequestView(CompanyAdminRequiredMixin, View):
    def post(self, request, pk):
        company = get_managed_company_or_404(request.user)
        restock_request = get_object_or_404(RestockRequest, pk=pk, agent__company=company)
        form = RestockApprovalForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Unable to approve the restock request. Please review the entered values.")
            return redirect("accounts:company_dashboard")

        cleaned = form.cleaned_data
        restock_request.status = RestockRequestStatus.FULFILLED
        restock_request.quantity_approved = cleaned["quantity_approved"]
        restock_request.approved_by = request.user
        restock_request.save()

        PaymentSchedule.objects.create(
            restock_request=restock_request,
            due_date=cleaned["due_date"],
            base_price=cleaned["base_price"],
            excise_tax=cleaned["excise_tax"],
            vat=cleaned["vat"],
            transport_cost=cleaned["transport_cost"],
            amount_paid=cleaned["amount_paid"],
            status=cleaned["status"],
        )

        apply_agent_inventory_adjustment(
            agent=restock_request.agent,
            product=restock_request.product,
            quantity_change=cleaned["quantity_approved"],
            transaction_type=InventoryTransactionType.RESTOCK,
            performed_by=request.user,
            note=restock_request.note or "Restock received from company admin approval.",
            batch_number=cleaned["batch_number"],
            base_unit_cost=cleaned["base_price"],
            expires_at=cleaned["expires_at"],
            received_at=cleaned["received_at"],
            reference=f"RESTOCK-{restock_request.pk}",
        )

        if restock_request.agent.admin:
            notify_user(
                restock_request.agent.admin,
                "Restock request fulfilled",
                f"{cleaned['quantity_approved']} units of {restock_request.product.name} were added to inventory.",
                link=reverse("accounts:agent_dashboard"),
            )
        messages.success(request, "Restock approved, payment schedule created, and inventory batch received.")
        return redirect("accounts:company_dashboard")


class SystemDashboardView(SystemAdminRequiredMixin, TemplateView):
    template_name = "accounts/system_dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stats"] = {
            "companies": Company.objects.count(),
            "verified_companies": Company.objects.filter(is_verified=True).count(),
            "pending_companies": Company.objects.filter(verification_status=CompanyVerificationStatus.PENDING_EFDA).count(),
            "suspended_companies": Company.objects.filter(is_active=False).count(),
            "agents": Agent.objects.count(),
            "drivers": Driver.objects.count(),
            "users": User.objects.count(),
            "orders": Order.objects.count(),
            "locked_users": User.objects.filter(locked_until__gt=timezone.now()).count(),
        }
        context["recent_orders"] = Order.objects.select_related("company", "customer", "selected_agent")[:15]
        context["companies"] = Company.objects.select_related("admin")
        context["company_form"] = SystemCompanyRegistrationForm()
        context["company_admin_form"] = InternalUserCreationForm(allowed_roles=(UserRole.COMPANY_ADMIN,))
        context["system_admin_form"] = InternalUserCreationForm(allowed_roles=(UserRole.SYSTEM_ADMIN,))
        context["recent_audit_logs"] = AuditLog.objects.select_related("actor")[:10]
        context["recent_announcements"] = Announcement.objects.select_related("created_by")[:5]
        context["notifications"] = self.request.user.notifications.all()[:8]
        context["featured_companies"] = context["companies"][:6]
        return context


class SystemCompanyListView(SystemAdminRequiredMixin, TemplateView):
    template_name = "accounts/system_companies.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        status_filter = self.request.GET.get("status", "").strip()
        search = self.request.GET.get("search", "").strip()
        companies = Company.objects.select_related("admin").order_by("name")
        if status_filter:
            companies = companies.filter(verification_status=status_filter)
        if search:
            companies = companies.filter(Q(name__icontains=search) | Q(location__icontains=search))
        context["companies"] = companies
        context["company_form"] = kwargs.get("company_form") or SystemCompanyRegistrationForm()
        context["status_filter"] = status_filter
        context["search"] = search
        return context


class SystemUserListView(SystemAdminRequiredMixin, TemplateView):
    template_name = "accounts/system_users.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        users = list(get_system_user_queryset(self.request))
        for user in users:
            user.company_label = build_user_company_labels(user)
        context["users"] = users
        context["companies"] = Company.objects.order_by("name")
        context["role_filter"] = self.request.GET.get("role", "").strip()
        context["status_filter"] = self.request.GET.get("status", "").strip()
        context["company_filter"] = self.request.GET.get("company", "").strip()
        context["search"] = self.request.GET.get("search", "").strip()
        return context


class SystemUserUpdateView(SystemAdminRequiredMixin, UpdateView):
    template_name = "accounts/system_user_form.html"
    form_class = SystemUserUpdateForm
    context_object_name = "managed_user"

    def get_queryset(self):
        return User.objects.all()

    def form_valid(self, form):
        user = self.get_object()
        old_values = serialize_user_for_audit(user)
        if user.pk == self.request.user.pk:
            new_role = form.cleaned_data.get("role")
            if new_role != UserRole.SYSTEM_ADMIN or not form.cleaned_data.get("is_active", True):
                form.add_error(None, "You cannot deactivate or demote your own system admin account.")
                return self.form_invalid(form)
        response = super().form_valid(form)
        record_audit_log(
            request=self.request,
            actor=self.request.user,
            action="user.updated",
            entity_type="user",
            entity_id=user.pk,
            entity_label=user.email,
            old_values=old_values,
            new_values=serialize_user_for_audit(self.object),
        )
        messages.success(self.request, "User account updated.")
        return response

    def form_invalid(self, form):
        messages.error(self.request, "Unable to update that user account.")
        return super().form_invalid(form)

    def get_success_url(self):
        return reverse("accounts:system_users")


class SystemUserBulkActionView(SystemAdminRequiredMixin, View):
    def post(self, request):
        user_ids = request.POST.getlist("user_ids")
        action = request.POST.get("action", "").strip()
        queryset = User.objects.filter(pk__in=user_ids)
        if action not in {"activate", "deactivate"} or not queryset.exists():
            messages.error(request, "Choose at least one user and a valid bulk action.")
            return redirect("accounts:system_users")

        updated = 0
        for user in queryset:
            if user.pk == request.user.pk and action == "deactivate":
                continue
            old_values = serialize_user_for_audit(user)
            user.is_active = action == "activate"
            if not user.is_active and user.role == UserRole.SYSTEM_ADMIN:
                continue
            user.save(update_fields=["is_active", "updated_at"])
            record_audit_log(
                request=request,
                actor=request.user,
                action=f"user.bulk_{action}",
                entity_type="user",
                entity_id=user.pk,
                entity_label=user.email,
                old_values=old_values,
                new_values=serialize_user_for_audit(user),
            )
            updated += 1

        messages.success(request, f"{updated} user account(s) updated.")
        return redirect("accounts:system_users")


class SystemUserPasswordResetView(SystemAdminRequiredMixin, View):
    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        form = PasswordResetForm({"email": user.email})
        if form.is_valid():
            form.save(
                request=request,
                use_https=request.is_secure(),
                email_template_name="accounts/password_reset_email.txt",
                subject_template_name="accounts/password_reset_subject.txt",
            )
            record_audit_log(
                request=request,
                actor=request.user,
                action="user.password_reset_requested",
                entity_type="user",
                entity_id=user.pk,
                entity_label=user.email,
                new_values={"email": user.email},
            )
            messages.success(request, f"Password reset email sent to {user.email}.")
        else:
            messages.error(request, "Unable to send the password reset email.")
        return redirect("accounts:system_users")


class SystemAuditLogListView(SystemAdminRequiredMixin, TemplateView):
    template_name = "accounts/system_audit_logs.html"

    def get_queryset(self):
        queryset = AuditLog.objects.select_related("actor").all()
        action_filter = self.request.GET.get("action", "").strip()
        entity_filter = self.request.GET.get("entity_type", "").strip()
        search = self.request.GET.get("search", "").strip()
        if action_filter:
            queryset = queryset.filter(action__icontains=action_filter)
        if entity_filter:
            queryset = queryset.filter(entity_type__icontains=entity_filter)
        if search:
            queryset = queryset.filter(
                Q(entity_label__icontains=search)
                | Q(entity_id__icontains=search)
                | Q(actor__email__icontains=search)
            )
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queryset = self.get_queryset()
        context["audit_logs"] = queryset[:200]
        context["action_filter"] = self.request.GET.get("action", "").strip()
        context["entity_filter"] = self.request.GET.get("entity_type", "").strip()
        context["search"] = self.request.GET.get("search", "").strip()
        export_query = self.request.GET.urlencode()
        context["export_url"] = reverse("accounts:system_audit_export") + (f"?{export_query}" if export_query else "")
        return context


class SystemAuditLogExportView(SystemAdminRequiredMixin, View):
    def get(self, request):
        view = SystemAuditLogListView()
        view.request = request
        rows = build_audit_log_rows(view.get_queryset()[:1000])
        payload = build_audit_log_csv(rows)
        response = HttpResponse(payload, content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="audit-log-export.csv"'
        return response


class SystemReportsView(SystemAdminRequiredMixin, TemplateView):
    template_name = "accounts/system_reports.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        date_from, date_to = get_company_reporting_window(self.request)
        summary, company_rows, growth_rows = build_system_report_data(date_from, date_to)
        context["summary"] = summary
        context["company_rows"] = company_rows
        context["growth_rows"] = growth_rows
        context["date_from"] = date_from
        context["date_to"] = date_to
        context["date_label"] = f"{date_from.isoformat()} to {date_to.isoformat()}"
        context["revenue_display"] = format_money(summary["revenue"])
        query_string = f"date_from={date_from.isoformat()}&date_to={date_to.isoformat()}"
        context["excel_export_url"] = f"{reverse('accounts:system_reports_export', kwargs={'export_format': 'excel'})}?{query_string}"
        context["pdf_export_url"] = f"{reverse('accounts:system_reports_export', kwargs={'export_format': 'pdf'})}?{query_string}"
        return context


class SystemReportExportView(SystemAdminRequiredMixin, View):
    def get(self, request, export_format):
        date_from, date_to = get_company_reporting_window(request)
        summary, company_rows, growth_rows = build_system_report_data(date_from, date_to)
        date_label = f"{date_from.isoformat()} to {date_to.isoformat()}"
        if export_format == "excel":
            payload = build_system_report_excel(
                date_label=date_label,
                summary=summary,
                company_rows=company_rows,
                growth_rows=growth_rows,
            )
            response = HttpResponse(payload, content_type="application/vnd.ms-excel")
            response["Content-Disposition"] = 'attachment; filename="platform-report.xls"'
            return response
        if export_format == "pdf":
            payload = build_system_report_pdf(
                date_label=date_label,
                summary=summary,
                company_rows=company_rows,
                growth_rows=growth_rows,
            )
            response = HttpResponse(payload, content_type="application/pdf")
            response["Content-Disposition"] = 'attachment; filename="platform-report.pdf"'
            return response
        return HttpResponse("Unsupported export format.", status=400)


class SystemAnnouncementListView(SystemAdminRequiredMixin, TemplateView):
    template_name = "accounts/system_announcements.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["announcement_form"] = kwargs.get("announcement_form") or AnnouncementForm()
        context["announcements"] = Announcement.objects.select_related("created_by").prefetch_related("deliveries")[:30]
        return context


class SystemAnnouncementCreateView(SystemAdminRequiredMixin, View):
    def post(self, request):
        form = AnnouncementForm(request.POST)
        if form.is_valid():
            announcement = form.save(commit=False)
            announcement.created_by = request.user
            announcement.save()
            deliver_announcement(announcement)
            record_audit_log(
                request=request,
                actor=request.user,
                action="announcement.created",
                entity_type="announcement",
                entity_id=announcement.pk,
                entity_label=announcement.title,
                new_values={
                    "target_role": announcement.target_role,
                    "recipient_count": announcement.recipient_count,
                    "sent_count": announcement.sent_count,
                    "failed_count": announcement.failed_count,
                },
            )
            messages.success(request, f"Announcement delivered to {announcement.sent_count} recipients.")
        else:
            messages.error(request, "Unable to send that announcement.")
        return redirect("accounts:system_announcements")


class CreateCompanyAdminView(SystemAdminRequiredMixin, View):
    role = UserRole.COMPANY_ADMIN

    def post(self, request):
        form = InternalUserCreationForm(request.POST, allowed_roles=(self.role,))
        if form.is_valid():
            user = form.save()
            company_id = request.POST.get("company_id")
            if company_id:
                company = Company.objects.filter(pk=company_id).first()
                if company is not None:
                    user.managed_company = company
                    user.save(update_fields=["managed_company", "updated_at"])
                    if company.admin_id is None:
                        company.admin = user
                        company.save(update_fields=["admin", "updated_at"])
            record_audit_log(
                request=request,
                actor=request.user,
                action="user.created",
                entity_type="user",
                entity_id=user.pk,
                entity_label=user.email,
                new_values=serialize_user_for_audit(user),
            )
            messages.success(request, "Company admin account created.")
        else:
            messages.error(request, "Unable to create company admin account.")
        return redirect(request.POST.get("next") or "accounts:system_dashboard")


class CreateSystemAdminView(SystemAdminRequiredMixin, View):
    def post(self, request):
        form = InternalUserCreationForm(request.POST, allowed_roles=(UserRole.SYSTEM_ADMIN,))
        if form.is_valid():
            user = form.save()
            record_audit_log(
                request=request,
                actor=request.user,
                action="user.created",
                entity_type="user",
                entity_id=user.pk,
                entity_label=user.email,
                new_values=serialize_user_for_audit(user),
            )
            messages.success(request, "System admin account created.")
        else:
            messages.error(request, "Unable to create system admin account.")
        return redirect(request.POST.get("next") or "accounts:system_dashboard")


class CreateCompanyView(SystemAdminRequiredMixin, View):
    def post(self, request):
        form = SystemCompanyRegistrationForm(request.POST, request.FILES)
        if form.is_valid():
            company = form.save(commit=False)
            company.verification_status = CompanyVerificationStatus.PENDING_EFDA
            company.submitted_to_efda_at = timezone.now()
            company.is_verified = False
            company.save()
            if company.admin and company.admin.managed_company_id != company.pk:
                company.admin.managed_company = company
                company.admin.save(update_fields=["managed_company", "updated_at"])
            record_audit_log(
                request=request,
                actor=request.user,
                action="company.created",
                entity_type="company",
                entity_id=company.pk,
                entity_label=company.name,
                new_values=serialize_company_for_audit(company),
            )
            for admin_user in get_company_admin_users(company):
                notify_user(
                    admin_user,
                    "Company registration submitted",
                    f"{company.name} has been submitted for EFDA review. Your company admin account will activate after approval.",
                )
            messages.success(
                request,
                f"{company.name} was created with {company.admin.email} as the pending company admin and sent to EFDA review.",
            )
        else:
            messages.error(request, "Unable to create the company.")
        return redirect(request.POST.get("next") or "accounts:system_dashboard")


class CompanyVerificationDecisionView(SystemAdminRequiredMixin, View):
    action = None

    def post(self, request, pk):
        company = get_object_or_404(Company, pk=pk)
        old_values = serialize_company_for_audit(company)
        note = request.POST.get("verification_note", "").strip()
        reference = request.POST.get("efda_reference", "").strip()

        if self.action == "suspend":
            company.is_active = False
            company.verification_note = note or "Suspended by system admin."
            company.save()
            for admin_user in get_company_admin_users(company):
                admin_user.is_active = False
                admin_user.save(update_fields=["is_active", "updated_at"])
                notify_user(
                    admin_user,
                    "Company suspended",
                    f"{company.name} has been suspended on the platform.",
                    link=reverse("accounts:company_dashboard"),
                )
            record_audit_log(
                request=request,
                actor=request.user,
                action="company.suspended",
                entity_type="company",
                entity_id=company.pk,
                entity_label=company.name,
                old_values=old_values,
                new_values=serialize_company_for_audit(company),
            )
            messages.warning(request, f"{company.name} has been suspended.")
            return redirect(request.POST.get("next") or "accounts:system_dashboard")

        if self.action == "reactivate":
            company.is_active = True
            company.verification_note = note or company.verification_note
            company.save()
            for admin_user in get_company_admin_users(company):
                admin_user.is_active = True
                admin_user.save(update_fields=["is_active", "updated_at"])
                notify_user(
                    admin_user,
                    "Company reactivated",
                    f"{company.name} has been reactivated on the platform.",
                    link=reverse("accounts:company_dashboard"),
                )
            record_audit_log(
                request=request,
                actor=request.user,
                action="company.reactivated",
                entity_type="company",
                entity_id=company.pk,
                entity_label=company.name,
                old_values=old_values,
                new_values=serialize_company_for_audit(company),
            )
            messages.success(request, f"{company.name} has been reactivated.")
            return redirect(request.POST.get("next") or "accounts:system_dashboard")

        if self.action == "resubmit":
            company.verification_status = CompanyVerificationStatus.PENDING_EFDA
            company.submitted_to_efda_at = timezone.now()
            company.efda_verified_at = None
            company.efda_reference = ""
            company.verification_note = note
            company.save()
            for admin_user in get_company_admin_users(company):
                notify_user(
                    admin_user,
                    "Company resubmitted to EFDA",
                    f"{company.name} has been resubmitted for EFDA verification.",
                    link=reverse("accounts:company_dashboard"),
                )
            record_audit_log(
                request=request,
                actor=request.user,
                action="company.resubmitted",
                entity_type="company",
                entity_id=company.pk,
                entity_label=company.name,
                old_values=old_values,
                new_values=serialize_company_for_audit(company),
            )
            messages.success(request, f"{company.name} was resubmitted to EFDA.")
            return redirect(request.POST.get("next") or "accounts:system_dashboard")

        if self.action == "verify":
            company.verification_status = CompanyVerificationStatus.VERIFIED
            company.efda_verified_at = timezone.now()
            company.efda_reference = reference
            company.verification_note = note
            company.is_active = True
            company.is_verified = True
            company.save()
            for admin_user in get_company_admin_users(company):
                admin_user.is_active = True
                if admin_user.email_verified_at is None:
                    admin_user.email_verified_at = timezone.now()
                admin_user.save(update_fields=["is_active", "email_verified_at", "updated_at"])
                notify_user(
                    admin_user,
                    "Company verified",
                    f"{company.name} passed EFDA verification and is now live on the platform.",
                    link=reverse("accounts:company_dashboard"),
                )
            send_company_admin_activation_email(company)
            record_audit_log(
                request=request,
                actor=request.user,
                action="company.verified",
                entity_type="company",
                entity_id=company.pk,
                entity_label=company.name,
                old_values=old_values,
                new_values=serialize_company_for_audit(company),
            )
            messages.success(request, f"{company.name} is now verified.")
        else:
            company.verification_status = CompanyVerificationStatus.REJECTED
            company.efda_verified_at = None
            company.verification_note = note or "Rejected during EFDA review."
            company.is_verified = False
            company.save()
            for admin_user in get_company_admin_users(company):
                admin_user.is_active = False
                admin_user.save(update_fields=["is_active", "updated_at"])
                notify_user(
                    admin_user,
                    "Company verification rejected",
                    f"{company.name} needs updates before EFDA approval.",
                    link=reverse("accounts:company_dashboard"),
                )
            record_audit_log(
                request=request,
                actor=request.user,
                action="company.rejected",
                entity_type="company",
                entity_id=company.pk,
                entity_label=company.name,
                old_values=old_values,
                new_values=serialize_company_for_audit(company),
            )
            messages.info(request, f"{company.name} was marked as rejected.")
        return redirect(request.POST.get("next") or "accounts:system_dashboard")


class NotificationsJsonView(LoginRequiredMixin, View):
    def get(self, request):
        notifications = request.user.notifications.all()[:10]
        payload = [
            {
                "title": notification.title,
                "message": notification.message,
                "link": notification.link,
                "created_at": notification.created_at.isoformat(),
                "is_read": notification.is_read,
            }
            for notification in notifications
        ]
        return JsonResponse({"notifications": payload})
