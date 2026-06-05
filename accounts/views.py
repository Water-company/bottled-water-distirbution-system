from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import FormView, TemplateView, UpdateView

from accounts.forms import (
    AgentForm,
    CompanyForm,
    CompanyPremiumSettingsForm,
    CustomerProfileForm,
    DriverForm,
    DriverLocationForm,
    InternalUserCreationForm,
    LoginForm,
    RegistrationForm,
    RegistrationOTPForm,
    RestockApprovalForm,
    RestockRequestForm,
)
from accounts.models import User, UserRole
from accounts.services import create_registration_otp, verify_registration_otp
from catalog.models import (
    Agent,
    AgentStock,
    Company,
    CompanyVerificationStatus,
    Driver,
    InventoryBatch,
    PaymentSchedule,
    Product,
    RestockRequest,
    RestockRequestStatus,
)
from core.mixins import (
    AgentManagerRequiredMixin,
    CompanyAdminRequiredMixin,
    CustomerRequiredMixin,
    DriverRequiredMixin,
    SystemAdminRequiredMixin,
)
from core.models import DriverLocation
from core.navigation import get_user_home_url
from core.policies import get_customer_loyalty_summary
from core.services import notify_user
from orders.models import Order, OrderAgentRequest, OrderStatus, RefundRequest, RefundRequestStatus
from orders.services import (
    approve_refund_request,
    assign_driver,
    complete_delivery_and_deduct_stock,
    reject_refund_request,
    start_delivery,
)


def _float_or_none(value):
    return float(value) if value not in (None, "") else None


def build_driver_dashboard_payload(driver, assigned_orders, location):
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
        ],
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


class RegisterView(FormView):
    template_name = "accounts/register.html"
    form_class = RegistrationForm
    success_url = reverse_lazy("accounts:verify_registration")

    def form_valid(self, form):
        user = form.save()
        create_registration_otp(user)
        self.request.session["pending_registration_email"] = user.email
        messages.success(self.request, "Your account was created. Enter the OTP we emailed to finish verification.")
        return super().form_valid(form)


class VerifyRegistrationOTPView(FormView):
    template_name = "accounts/verify_registration.html"
    form_class = RegistrationOTPForm
    success_url = reverse_lazy("accounts:login")

    def get_initial(self):
        initial = super().get_initial()
        if self.request.session.get("pending_registration_email"):
            initial["email"] = self.request.session["pending_registration_email"]
        elif self.request.GET.get("email"):
            initial["email"] = self.request.GET["email"].lower()
        return initial

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
        create_registration_otp(user)
        request.session["pending_registration_email"] = user.email
        messages.success(request, "A fresh OTP has been sent to your email.")
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
        orders = self.request.user.orders.all()
        context["stats"] = {
            "total_orders": orders.count(),
            "active_orders": orders.filter(
                status__in=[
                    OrderStatus.REQUESTED,
                    OrderStatus.PAYMENT_PENDING,
                    OrderStatus.PAID,
                    OrderStatus.DRIVER_ASSIGNED,
                    OrderStatus.OUT_FOR_DELIVERY,
                ]
            ).count(),
            "delivered_orders": orders.filter(status=OrderStatus.DELIVERED).count(),
            "total_spent": orders.filter(status=OrderStatus.DELIVERED).aggregate(total=Sum("total"))["total"] or 0,
        }
        context["recent_orders"] = orders[:5]
        context["notifications"] = self.request.user.notifications.all()[:8]
        context["payment_history"] = [order.payment for order in orders.select_related("payment") if hasattr(order, "payment")]
        context["loyalty_summary"] = get_customer_loyalty_summary(self.request.user)
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


class AgentManagerDashboardView(AgentManagerRequiredMixin, TemplateView):
    template_name = "accounts/agent_dashboard.html"

    def get_agent(self):
        return get_object_or_404(Agent, admin=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agent = self.get_agent()
        pending_requests = agent.order_requests.select_related("order__customer").filter(status="pending")
        context["agent"] = agent
        context["agent_online"] = agent.is_online
        context["pending_requests"] = pending_requests
        context["accepted_orders"] = agent.accepted_orders.filter(
            status__in=[
                OrderStatus.PAID,
                OrderStatus.DRIVER_ASSIGNED,
                OrderStatus.OUT_FOR_DELIVERY,
            ]
        ).select_related("customer", "assigned_driver__user")[:10]
        context["drivers"] = agent.drivers.select_related("user")
        context["low_stock_items"] = agent.stocks.select_related("product").filter(
            available_quantity__lte=models.F("reorder_level")
        )
        context["restock_form"] = RestockRequestForm(products=Product.objects.filter(company=agent.company))
        context["notifications"] = self.request.user.notifications.all()[:8]
        return context


class PendingOrdersJsonView(AgentManagerRequiredMixin, View):
    def get(self, request):
        agent = get_object_or_404(Agent, admin=request.user)
        pending_requests = agent.order_requests.select_related("order__customer").filter(status="pending")
        payload = [
            {
                "order_number": request_item.order.order_number,
                "customer": request_item.order.customer.full_name,
                "distance_km": float(request_item.distance_km),
                "total": float(request_item.order.total),
                "deadline": request_item.order.agent_response_deadline.isoformat() if request_item.order.agent_response_deadline else "",
                "address": request_item.order.delivery_address,
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
        agent = get_object_or_404(Agent, admin=request.user)
        order = get_object_or_404(Order, order_number=order_number, selected_agent=agent, status=OrderStatus.PAID)
        driver = get_object_or_404(Driver, pk=request.POST.get("driver_id"), agent=agent)
        try:
            assign_driver(order, driver)
            messages.success(request, f"{driver.user.full_name} assigned to {order.order_number}.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to assign that driver.")
        return redirect("accounts:agent_dashboard")


class CreateRestockRequestView(AgentManagerRequiredMixin, View):
    def post(self, request):
        agent = get_object_or_404(Agent, admin=request.user)
        form = RestockRequestForm(request.POST, products=Product.objects.filter(company=agent.company))
        if form.is_valid():
            restock_request = form.save(commit=False)
            restock_request.agent = agent
            restock_request.requested_by = request.user
            restock_request.save()
            if agent.company.admin:
                notify_user(
                    agent.company.admin,
                    "Restock request submitted",
                    f"{agent.name} requested more stock for {restock_request.product.name}.",
                    link=reverse("accounts:company_dashboard"),
                )
            messages.success(request, "Restock request submitted.")
        else:
            messages.error(request, "Unable to submit restock request.")
        return redirect("accounts:agent_dashboard")


class DriverDashboardView(DriverRequiredMixin, TemplateView):
    template_name = "accounts/driver_dashboard.html"

    def get_driver(self):
        return get_object_or_404(Driver, user=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        driver = self.get_driver()
        assigned_orders = list(driver.assigned_orders.select_related("customer", "selected_agent")[:10])
        location = getattr(self.request.user, "driver_location", None)
        context["driver"] = driver
        context["assigned_orders"] = assigned_orders
        context["location"] = location
        context["driver_online"] = driver.is_online
        context["driver_map_payload"] = build_driver_dashboard_payload(driver, assigned_orders, location)
        context["reverse_geocode_url"] = reverse("core:reverse_geocode")
        context["notifications"] = self.request.user.notifications.all()[:8]
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


class CompanyAdminDashboardView(CompanyAdminRequiredMixin, TemplateView):
    template_name = "accounts/company_dashboard.html"

    def get_company(self):
        return get_object_or_404(Company, admin=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = self.get_company()
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
        context["refund_requests"] = RefundRequest.objects.filter(order__company=company).select_related("order", "requested_by", "reviewed_by")[:15]
        context["agent_form"] = AgentForm(company=company)
        context["agent_manager_form"] = InternalUserCreationForm(allowed_roles=(UserRole.AGENT_MANAGER,))
        context["driver_user_form"] = InternalUserCreationForm(allowed_roles=(UserRole.DRIVER,))
        context["driver_form"] = DriverForm(company=company)
        context["premium_form"] = CompanyPremiumSettingsForm(instance=company)
        context["company_map_payload"] = build_company_map_payload(company, agents, drivers)
        context["company_locations_url"] = reverse("accounts:company_locations_json")
        context["notifications"] = self.request.user.notifications.all()[:8]
        return context


class CompanyLocationsJsonView(CompanyAdminRequiredMixin, View):
    def get(self, request):
        company = get_object_or_404(Company, admin=request.user)
        agents = list(company.agents.select_related("admin"))
        drivers = list(
            Driver.objects.filter(agent__company=company).select_related("user", "agent", "user__driver_location")
        )
        return JsonResponse(build_company_map_payload(company, agents, drivers))


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
        company = get_object_or_404(Company, admin=request.user)
        form = AgentForm(request.POST, company=company)
        if form.is_valid():
            agent = form.save(commit=False)
            agent.company = company
            agent.save()
            messages.success(request, f"{agent.name} created successfully.")
        else:
            messages.error(request, "Unable to create the agent branch.")
        return redirect("accounts:company_dashboard")


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
        company = get_object_or_404(Company, admin=request.user)
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
        company = get_object_or_404(Company, admin=request.user)
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
        company = get_object_or_404(Company, admin=request.user)
        refund_request = get_object_or_404(RefundRequest, pk=pk, order__company=company)
        resolution_note = request.POST.get("resolution_note", "")
        approved_amount_raw = request.POST.get("approved_amount")
        approved_amount = approved_amount_raw or None
        try:
            if self.action == "approve":
                approve_refund_request(
                    refund_request,
                    reviewed_by=request.user,
                    approved_amount=approved_amount,
                    resolution_note=resolution_note,
                )
                messages.success(request, f"Refund approved for {refund_request.order.order_number}.")
            else:
                reject_refund_request(
                    refund_request,
                    reviewed_by=request.user,
                    resolution_note=resolution_note,
                )
                messages.info(request, f"Refund request rejected for {refund_request.order.order_number}.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to update this refund request.")
        return redirect("accounts:company_dashboard")


class ApproveRestockRequestView(CompanyAdminRequiredMixin, View):
    def post(self, request, pk):
        company = get_object_or_404(Company, admin=request.user)
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

        InventoryBatch.objects.create(
            agent=restock_request.agent,
            product=restock_request.product,
            batch_number=cleaned["batch_number"],
            quantity_received=cleaned["quantity_approved"],
            quantity_remaining=cleaned["quantity_approved"],
            base_unit_cost=cleaned["base_price"],
            expires_at=cleaned["expires_at"],
            received_at=cleaned["received_at"],
        )
        stock, _ = AgentStock.objects.get_or_create(
            agent=restock_request.agent,
            product=restock_request.product,
            defaults={"available_quantity": 0, "reorder_level": 0},
        )
        stock.available_quantity += cleaned["quantity_approved"]
        stock.save(update_fields=["available_quantity", "updated_at"])

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
            "agents": Agent.objects.count(),
            "drivers": Driver.objects.count(),
            "users": User.objects.count(),
            "orders": Order.objects.count(),
        }
        context["recent_orders"] = Order.objects.select_related("company", "customer", "selected_agent")[:15]
        context["companies"] = Company.objects.select_related("admin")
        context["company_form"] = CompanyForm()
        context["company_admin_form"] = InternalUserCreationForm(allowed_roles=(UserRole.COMPANY_ADMIN,))
        context["system_admin_form"] = InternalUserCreationForm(allowed_roles=(UserRole.SYSTEM_ADMIN,))
        context["notifications"] = self.request.user.notifications.all()[:8]
        return context


class CreateCompanyAdminView(SystemAdminRequiredMixin, View):
    role = UserRole.COMPANY_ADMIN

    def post(self, request):
        form = InternalUserCreationForm(request.POST, allowed_roles=(self.role,))
        if form.is_valid():
            form.save()
            messages.success(request, "Company admin account created.")
        else:
            messages.error(request, "Unable to create company admin account.")
        return redirect("accounts:system_dashboard")


class CreateSystemAdminView(SystemAdminRequiredMixin, View):
    def post(self, request):
        form = InternalUserCreationForm(request.POST, allowed_roles=(UserRole.SYSTEM_ADMIN,))
        if form.is_valid():
            form.save()
            messages.success(request, "System admin account created.")
        else:
            messages.error(request, "Unable to create system admin account.")
        return redirect("accounts:system_dashboard")


class CreateCompanyView(SystemAdminRequiredMixin, View):
    def post(self, request):
        form = CompanyForm(request.POST, request.FILES)
        if form.is_valid():
            company = form.save(commit=False)
            company.verification_status = CompanyVerificationStatus.PENDING_EFDA
            company.submitted_to_efda_at = timezone.now()
            company.is_verified = False
            company.save()
            if company.admin:
                notify_user(
                    company.admin,
                    "Company registration submitted",
                    f"{company.name} has been submitted for EFDA review.",
                    link=reverse("accounts:company_dashboard"),
                )
            messages.success(request, f"{company.name} was created and sent to EFDA review.")
        else:
            messages.error(request, "Unable to create the company.")
        return redirect("accounts:system_dashboard")


class CompanyVerificationDecisionView(SystemAdminRequiredMixin, View):
    action = None

    def post(self, request, pk):
        company = get_object_or_404(Company, pk=pk)
        note = request.POST.get("verification_note", "").strip()
        reference = request.POST.get("efda_reference", "").strip()

        if self.action == "resubmit":
            company.verification_status = CompanyVerificationStatus.PENDING_EFDA
            company.submitted_to_efda_at = timezone.now()
            company.efda_verified_at = None
            company.efda_reference = ""
            company.verification_note = note
            company.save()
            if company.admin:
                notify_user(
                    company.admin,
                    "Company resubmitted to EFDA",
                    f"{company.name} has been resubmitted for EFDA verification.",
                    link=reverse("accounts:company_dashboard"),
                )
            messages.success(request, f"{company.name} was resubmitted to EFDA.")
            return redirect("accounts:system_dashboard")

        if self.action == "verify":
            company.verification_status = CompanyVerificationStatus.VERIFIED
            company.efda_verified_at = timezone.now()
            company.efda_reference = reference
            company.verification_note = note
            company.is_verified = True
            company.save()
            if company.admin:
                notify_user(
                    company.admin,
                    "Company verified",
                    f"{company.name} passed EFDA verification and is now live on the platform.",
                    link=reverse("accounts:company_dashboard"),
                )
            messages.success(request, f"{company.name} is now verified.")
        else:
            company.verification_status = CompanyVerificationStatus.REJECTED
            company.efda_verified_at = None
            company.verification_note = note or "Rejected during EFDA review."
            company.is_verified = False
            company.save()
            if company.admin:
                notify_user(
                    company.admin,
                    "Company verification rejected",
                    f"{company.name} needs updates before EFDA approval.",
                    link=reverse("accounts:company_dashboard"),
                )
            messages.info(request, f"{company.name} was marked as rejected.")
        return redirect("accounts:system_dashboard")


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
