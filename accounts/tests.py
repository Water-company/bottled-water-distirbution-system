from decimal import Decimal

from django.conf import settings
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.contrib.messages import get_messages
from django.utils import timezone

from accounts.forms import SystemCompanyRegistrationForm
from accounts.models import CustomerAddress, RegistrationOTP, User, UserRole
from catalog.models import (
    Agent,
    AgentBatchSale,
    AgentBatchSalePayment,
    AgentBatchSalePaymentStatus,
    AgentBatchSalePaymentType,
    AgentBatchSaleStatus,
    AgentStock,
    Company,
    CompanyBatch,
    CompanyBatchStatus,
    CompanyVerificationStatus,
    Driver,
    InventoryBatch,
    InventoryTransaction,
    InventoryTransactionType,
    Product,
)
from catalog.services import (
    auto_confirm_stale_agent_batch_sales,
    cancel_agent_batch_sale,
    confirm_agent_batch_sale_receipt,
    get_agent_open_batch_balance,
    notify_overdue_agent_batch_sales,
)
from core.models import Announcement, AuditLog, DriverLocation, Notification
from orders.models import Order, OrderStatus, Payment, PaymentProvider, PaymentStatus, RefundRequestStatus
from orders.services import request_order_refund


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class AccountFlowTests(TestCase):
    def test_registration_creates_inactive_user_and_sends_otp_email(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "first_name": "Sara",
                "last_name": "Bekele",
                "email": "sara@example.com",
                "phone_number": "+251911000001",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )

        self.assertRedirects(response, reverse("accounts:verify_registration"))
        user = User.objects.get(email="sara@example.com")
        self.assertFalse(user.is_active)
        self.assertEqual(RegistrationOTP.objects.filter(user=user).count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("OTP", mail.outbox[0].body)

    def test_otp_verification_activates_user_within_expiry_window_and_sends_success_email(self):
        self.client.post(
            reverse("accounts:register"),
            {
                "first_name": "Miki",
                "last_name": "Stone",
                "email": "active@example.com",
                "phone_number": "+251911000002",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )
        user = User.objects.get(email="active@example.com")
        otp = RegistrationOTP.objects.filter(user=user).latest("created_at")

        response = self.client.post(
            reverse("accounts:verify_registration"),
            {"email": user.email, "otp_code": otp.code},
        )
        self.assertRedirects(response, reverse("accounts:login"))
        user.refresh_from_db()
        otp.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertIsNotNone(user.email_verified_at)
        self.assertIsNotNone(otp.consumed_at)
        self.assertEqual(len(mail.outbox), 2)

    def test_expired_registration_otp_is_rejected_and_invalidated(self):
        self.client.post(
            reverse("accounts:register"),
            {
                "first_name": "Expired",
                "last_name": "User",
                "email": "expired@example.com",
                "phone_number": "+251911000003",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )
        user = User.objects.get(email="expired@example.com")
        otp = RegistrationOTP.objects.filter(user=user).latest("created_at")
        expired_at = timezone.now() - timezone.timedelta(seconds=5)
        RegistrationOTP.objects.filter(pk=otp.pk).update(expires_at=expired_at)

        response = self.client.post(
            reverse("accounts:verify_registration"),
            {"email": user.email, "otp_code": otp.code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "That OTP is invalid or has expired. Please request a new code.")
        user.refresh_from_db()
        otp.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(otp.consumed_at, otp.expires_at)

    def test_resend_registration_otp_sends_fresh_email(self):
        self.client.post(
            reverse("accounts:register"),
            {
                "first_name": "Resend",
                "last_name": "User",
                "email": "resend@example.com",
                "phone_number": "+251911000011",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )
        user = User.objects.get(email="resend@example.com")
        first_otp = RegistrationOTP.objects.filter(user=user).latest("created_at")
        RegistrationOTP.objects.filter(pk=first_otp.pk).update(
            created_at=timezone.now() - timezone.timedelta(seconds=61)
        )

        response = self.client.post(
            reverse("accounts:resend_registration_otp"),
            {"email": user.email},
        )

        self.assertRedirects(response, reverse("accounts:verify_registration"))
        self.assertEqual(RegistrationOTP.objects.filter(user=user).count(), 2)
        latest_otp = RegistrationOTP.objects.filter(user=user).order_by("-pk").first()
        first_otp.refresh_from_db()
        self.assertNotEqual(first_otp.pk, latest_otp.pk)
        self.assertIsNotNone(first_otp.consumed_at)
        self.assertGreater(latest_otp.expires_at, first_otp.expires_at)
        self.assertEqual(len(mail.outbox), 2)

    def test_verify_registration_page_exposes_server_timestamps_for_countdowns(self):
        self.client.post(
            reverse("accounts:register"),
            {
                "first_name": "Timer",
                "last_name": "User",
                "email": "timer@example.com",
                "phone_number": "+251911000014",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )
        user = User.objects.get(email="timer@example.com")
        otp = RegistrationOTP.objects.filter(user=user).latest("created_at")

        response = self.client.get(reverse("accounts:verify_registration"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["otp_expires_at"], otp.expires_at)
        self.assertEqual(response.context["otp_issued_at"], otp.created_at)
        self.assertEqual(
            response.context["resend_available_at"],
            otp.created_at + timezone.timedelta(seconds=settings.REGISTRATION_OTP_RESEND_SECONDS),
        )
        self.assertContains(response, 'data-expires-at="')
        self.assertContains(response, 'data-resend-available-at="')

    def test_login_is_blocked_until_registration_is_verified(self):
        self.client.post(
            reverse("accounts:register"),
            {
                "first_name": "Blocked",
                "last_name": "Login",
                "email": "blocked@example.com",
                "phone_number": "+251911000012",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )

        login_response = self.client.post(
            reverse("accounts:login"),
            {"email": "blocked@example.com", "password": "StrongPass123!", "remember_me": ""},
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertContains(login_response, "Please verify your email before logging in.")

    def test_account_locks_after_five_failed_login_attempts(self):
        user = User.objects.create_user(
            email="lock-me@example.com",
            password="StrongPass123!",
            first_name="Lock",
            last_name="Me",
            phone_number="+251911000013",
            is_active=True,
        )

        for _ in range(4):
            response = self.client.post(
                reverse("accounts:login"),
                {"email": user.email, "password": "WrongPass123!", "remember_me": ""},
            )
            self.assertContains(response, "Invalid email or password.")

        response = self.client.post(
            reverse("accounts:login"),
            {"email": user.email, "password": "WrongPass123!", "remember_me": ""},
        )

        user.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Too many failed login attempts.")
        self.assertTrue(user.is_locked)

    def test_password_reset_request_sends_email(self):
        user = User.objects.create_user(
            email="reset@example.com",
            password="StrongPass123!",
            first_name="Liya",
            last_name="Abebe",
            phone_number="+251911000003",
            is_active=True,
        )

        response = self.client.post(reverse("accounts:password_reset"), {"email": user.email})
        self.assertRedirects(response, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)

    def test_customer_user_home_is_public_company_landing_page(self):
        customer_user = User.objects.create_user(
            email="customer@example.com",
            password="StrongPass123!",
            first_name="Customer",
            last_name="User",
            phone_number="+251911000005",
            is_customer=True,
        )

        self.client.force_login(customer_user)
        home_response = self.client.get(reverse("home"))
        self.assertEqual(home_response.status_code, 200)
        self.assertContains(home_response, "Browse different water companies")

    def test_home_is_public_company_landing_page_for_staff_too(self):
        staff_user = User.objects.create_superuser(
            email="admin@example.com",
            password="StrongPass123!",
            first_name="Admin",
            last_name="User",
            phone_number="+251911000004",
        )

        self.client.force_login(staff_user)
        home_response = self.client.get(reverse("home"))
        self.assertEqual(home_response.status_code, 200)
        self.assertContains(home_response, "Browse different water companies")

    def test_login_page_is_accessible_while_already_authenticated(self):
        staff_user = User.objects.create_superuser(
            email="admin2@example.com",
            password="StrongPass123!",
            first_name="Admin",
            last_name="Two",
            phone_number="+251911000006",
        )

        self.client.force_login(staff_user)
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)

    def test_system_admin_user_is_redirected_to_system_dashboard_from_login(self):
        staff_user = User.objects.create_user(
            email="admin3@example.com",
            password="StrongPass123!",
            first_name="Admin",
            last_name="User",
            phone_number="+251911000007",
            role=UserRole.SYSTEM_ADMIN,
        )

        login_response = self.client.post(
            reverse("accounts:login"),
            {"email": staff_user.email, "password": "StrongPass123!", "remember_me": ""},
        )
        self.assertRedirects(login_response, reverse("accounts:system_dashboard"))

    def test_non_customer_is_redirected_to_their_dashboard_from_customer_dashboard(self):
        system_admin = User.objects.create_user(
            email="system@example.com",
            password="StrongPass123!",
            first_name="System",
            last_name="Admin",
            phone_number="+251911000008",
            role=UserRole.SYSTEM_ADMIN,
        )

        self.client.force_login(system_admin)
        dashboard_response = self.client.get(reverse("accounts:dashboard"))
        self.assertRedirects(dashboard_response, reverse("accounts:system_dashboard"))
        messages = [message.message for message in get_messages(dashboard_response.wsgi_request)]
        self.assertIn("That page is only available to customer accounts.", messages)

    def test_profile_page_is_available_to_internal_roles_too(self):
        driver_user = User.objects.create_user(
            email="driver@example.com",
            password="StrongPass123!",
            first_name="Driver",
            last_name="User",
            phone_number="+251911000009",
            role=UserRole.DRIVER,
        )

        self.client.force_login(driver_user)
        response = self.client.get(reverse("accounts:profile"))
        self.assertEqual(response.status_code, 200)


class CustomerAddressAndNotificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="customer-flows@example.com",
            password="StrongPass123!",
            first_name="Customer",
            last_name="Flow",
            phone_number="+251911000030",
            is_active=True,
        )
        self.client.force_login(self.user)

    def test_first_saved_address_becomes_default(self):
        response = self.client.post(
            reverse("accounts:create_address"),
            {
                "label": "Hotel",
                "address_line": "Bole Road, Addis Ababa",
                "latitude": "9.011111",
                "longitude": "38.761111",
                "notes": "Front desk drop-off",
                "is_default": "",
            },
        )

        self.assertRedirects(response, reverse("accounts:addresses"))
        address = CustomerAddress.objects.get(user=self.user, label="Hotel")
        self.assertTrue(address.is_default)

    def test_setting_new_default_address_unsets_previous_default(self):
        first_address = CustomerAddress.objects.create(
            user=self.user,
            label="Office",
            address_line="Kazanchis, Addis Ababa",
            latitude="9.022222",
            longitude="38.752222",
            is_default=True,
        )
        second_address = CustomerAddress.objects.create(
            user=self.user,
            label="Venue",
            address_line="CMC, Addis Ababa",
            latitude="9.033333",
            longitude="38.773333",
            is_default=False,
        )

        response = self.client.post(reverse("accounts:set_default_address", kwargs={"pk": second_address.pk}))

        self.assertRedirects(response, reverse("accounts:addresses"))
        first_address.refresh_from_db()
        second_address.refresh_from_db()
        self.assertFalse(first_address.is_default)
        self.assertTrue(second_address.is_default)

    def test_mark_notifications_read_marks_all_customer_notifications_as_read(self):
        Notification.objects.create(recipient=self.user, title="Order accepted", message="Your order is ready.")
        Notification.objects.create(recipient=self.user, title="Driver assigned", message="A driver is coming.")

        response = self.client.post(reverse("accounts:mark_notifications_read"))

        self.assertRedirects(response, reverse("accounts:notifications"))
        self.assertEqual(self.user.notifications.filter(is_read=False).count(), 0)


class UploadValidationFormTests(TestCase):
    def build_company_registration_form(self, *, registration_document):
        return SystemCompanyRegistrationForm(
            data={
                "name": "Aqua Capital",
                "description": "Regional bottler",
                "location": "Adama",
                "address": "Adama Industrial Zone",
                "latitude": "8.540000",
                "longitude": "39.270000",
                "contact_email": "ops@aquacapital.example.com",
                "contact_phone": "+251911000080",
                "efda_license_number": "EFDA-2026-001",
                "admin_first_name": "Marta",
                "admin_last_name": "Tesfaye",
                "admin_email": "marta@aquacapital.example.com",
                "admin_phone_number": "+251911000081",
                "admin_password": "StrongPass123!",
            },
            files={"registration_document": registration_document},
        )

    def test_registration_document_rejects_files_over_ten_mb(self):
        oversized_document = SimpleUploadedFile(
            "license.pdf",
            b"x" * ((10 * 1024 * 1024) + 1),
            content_type="application/pdf",
        )

        form = self.build_company_registration_form(registration_document=oversized_document)

        self.assertFalse(form.is_valid())
        self.assertIn("File size must not exceed 10 MB.", form.errors["registration_document"])

    def test_registration_document_rejects_wrong_content_type(self):
        invalid_document = SimpleUploadedFile(
            "license.txt",
            b"plain-text-document",
            content_type="text/plain",
        )

        form = self.build_company_registration_form(registration_document=invalid_document)

        self.assertFalse(form.is_valid())
        self.assertIn(
            "Unsupported file type. Allowed types: application/pdf, image/jpeg, image/png.",
            form.errors["registration_document"],
        )

    def test_registration_document_accepts_valid_pdf_upload(self):
        valid_document = SimpleUploadedFile(
            "license.pdf",
            b"%PDF-1.4 fake pdf",
            content_type="application/pdf",
        )

        form = self.build_company_registration_form(registration_document=valid_document)

        self.assertTrue(form.is_valid(), form.errors)


class AgentManagerPortalTests(TestCase):
    def setUp(self):
        self.agent_manager = User.objects.create_user(
            email="agent-portal@example.com",
            password="StrongPass123!",
            first_name="Alem",
            last_name="Manager",
            phone_number="+251911000040",
            role=UserRole.AGENT_MANAGER,
            is_active=True,
        )
        self.company_admin = User.objects.create_user(
            email="company-owner@example.com",
            password="StrongPass123!",
            first_name="Company",
            last_name="Owner",
            phone_number="+251911000041",
            role=UserRole.COMPANY_ADMIN,
            is_active=True,
        )
        self.company = Company.objects.create(
            name="Blue Source",
            description="Regional supplier",
            location="Addis Ababa",
            is_verified=True,
            admin=self.company_admin,
        )
        self.agent = Agent.objects.create(
            company=self.company,
            name="Blue Source Bole Agent",
            location_name="Bole",
            latitude="9.010000",
            longitude="38.760000",
            service_radius_km="20.00",
            is_active=True,
            is_accepting_orders=True,
            admin=self.agent_manager,
        )
        self.product = Product.objects.create(
            company=self.company,
            name="18L Office Bottle",
            description="Large office bottle",
            price="100.00",
            available_quantity=200,
        )
        self.company_batch = CompanyBatch.objects.create(
            company=self.company,
            product=self.product,
            batch_number="BATCH-2026-AGT1",
            production_date=timezone.localdate(),
            total_cases_produced=400,
            unsold_cases_remaining=400,
            unit_price="85.00",
            created_by=self.company_admin,
        )
        self.stock = AgentStock.objects.create(
            agent=self.agent,
            product=self.product,
            available_quantity=10,
            reorder_level=3,
        )
        self.customer = User.objects.create_user(
            email="agent-customer@example.com",
            password="StrongPass123!",
            first_name="Grand",
            last_name="Hotel",
            phone_number="+251911000046",
            is_active=True,
        )
        self.driver_user = User.objects.create_user(
            email="agent-driver@example.com",
            password="StrongPass123!",
            first_name="Live",
            last_name="Driver",
            phone_number="+251911000047",
            role=UserRole.DRIVER,
            is_active=True,
        )
        self.driver = Driver.objects.create(
            agent=self.agent,
            user=self.driver_user,
            vehicle_identifier="LIVE-7",
            phone_number=self.driver_user.phone_number,
            is_active=True,
        )
        self.client.force_login(self.agent_manager)

    def test_agent_manager_can_create_driver_account_from_driver_page(self):
        response = self.client.post(
            reverse("accounts:agent_driver_create"),
            {
                "first_name": "Marta",
                "last_name": "Driver",
                "email": "marta.driver@example.com",
                "phone_number": "+251911000042",
                "password": "StrongPass123!",
                "vehicle_identifier": "AA-88888",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("accounts:agent_drivers"))
        driver = Driver.objects.get(user__email="marta.driver@example.com")
        self.assertEqual(driver.agent, self.agent)
        self.assertEqual(driver.vehicle_identifier, "AA-88888")
        self.assertTrue(driver.is_active)
        self.assertTrue(driver.user.is_active)

    def test_agent_manager_can_update_driver_details(self):
        driver_user = User.objects.create_user(
            email="driver-edit@example.com",
            password="StrongPass123!",
            first_name="Old",
            last_name="Name",
            phone_number="+251911000043",
            role=UserRole.DRIVER,
            is_active=True,
        )
        driver = Driver.objects.create(
            agent=self.agent,
            user=driver_user,
            vehicle_identifier="OLD-1",
            phone_number=driver_user.phone_number,
            is_active=True,
        )

        response = self.client.post(
            reverse("accounts:agent_driver_edit", kwargs={"pk": driver.pk}),
            {
                "first_name": "New",
                "last_name": "Driver",
                "email": "driver-edit@example.com",
                "phone_number": "+251911000044",
                "vehicle_identifier": "NEW-9",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("accounts:agent_drivers"))
        driver.refresh_from_db()
        driver_user.refresh_from_db()
        self.assertEqual(driver.user.full_name, "New Driver")
        self.assertEqual(driver.phone_number, "+251911000044")
        self.assertEqual(driver.vehicle_identifier, "NEW-9")

    def test_agent_manager_can_toggle_driver_active_state(self):
        driver_user = User.objects.create_user(
            email="driver-toggle@example.com",
            password="StrongPass123!",
            first_name="Toggle",
            last_name="Driver",
            phone_number="+251911000045",
            role=UserRole.DRIVER,
            is_active=True,
        )
        driver = Driver.objects.create(
            agent=self.agent,
            user=driver_user,
            vehicle_identifier="TOGGLE-1",
            phone_number=driver_user.phone_number,
            is_active=True,
        )

        response = self.client.post(reverse("accounts:agent_driver_toggle", kwargs={"pk": driver.pk}))

        self.assertRedirects(response, reverse("accounts:agent_drivers"))
        driver.refresh_from_db()
        driver_user.refresh_from_db()
        self.assertFalse(driver.is_active)
        self.assertFalse(driver.user.is_active)

    def test_agent_manager_can_record_inventory_adjustment_with_transaction_log(self):
        response = self.client.post(
            reverse("accounts:agent_inventory_adjust"),
            {
                "product": self.product.pk,
                "transaction_type": InventoryTransactionType.RESTOCK,
                "change_direction": "increase",
                "quantity": 5,
                "batch_number": "RESTOCK-500",
                "base_unit_cost": "72.00",
                "received_at": "2026-06-06",
                "expires_at": "2026-12-31",
                "note": "Emergency top-up",
            },
        )

        self.assertRedirects(response, reverse("accounts:agent_inventory"))
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.available_quantity, 15)
        transaction = InventoryTransaction.objects.get(agent=self.agent, product=self.product, reference=f"MANUAL-{self.agent.pk}")
        self.assertEqual(transaction.transaction_type, InventoryTransactionType.RESTOCK)
        self.assertEqual(transaction.quantity_change, 5)
        self.assertEqual(transaction.stock_after, 15)

    def test_agent_manager_can_view_driver_detail_page(self):
        order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            selected_agent=self.agent,
            assigned_driver=self.driver,
            order_number="ORD-DRVSTAT1",
            status=OrderStatus.DELIVERED,
            delivery_address="Bole Atlas, Addis Ababa",
            latitude="9.015000",
            longitude="38.770000",
            phone_number=self.customer.phone_number,
            subtotal="100.00",
            delivery_fee="10.00",
            total="110.00",
            paid_at=timezone.now(),
            delivered_at=timezone.now(),
        )
        order.items.create(
            product=self.product,
            product_name=self.product.name,
            unit_price="100.00",
            quantity=1,
        )

        response = self.client.get(reverse("accounts:agent_driver_detail", kwargs={"pk": self.driver.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.driver.user.full_name)
        self.assertContains(response, order.order_number)

    def test_agent_fleet_json_returns_driver_and_active_delivery(self):
        Order.objects.create(
            customer=self.customer,
            company=self.company,
            selected_agent=self.agent,
            assigned_driver=self.driver,
            order_number="ORD-FLEET01",
            status=OrderStatus.OUT_FOR_DELIVERY,
            delivery_address="Kazanchis, Addis Ababa",
            latitude="9.020000",
            longitude="38.750000",
            phone_number=self.customer.phone_number,
            subtotal="100.00",
            delivery_fee="10.00",
            total="110.00",
            paid_at=timezone.now(),
            out_for_delivery_at=timezone.now(),
        )
        DriverLocation.objects.create(
            driver_user=self.driver_user,
            latitude="9.019000",
            longitude="38.749000",
            is_online=True,
        )

        response = self.client.get(reverse("accounts:agent_fleet_json"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary"]["onlineDrivers"], 1)
        self.assertEqual(payload["summary"]["activeDeliveries"], 1)
        self.assertEqual(payload["drivers"][0]["name"], self.driver.user.full_name)
        self.assertEqual(payload["orders"][0]["customerName"], self.customer.full_name)

    def test_agent_manager_can_approve_refund_from_agent_refund_queue(self):
        order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            selected_agent=self.agent,
            assigned_driver=self.driver,
            order_number="ORD-RFDAGT1",
            status=OrderStatus.DELIVERED,
            delivery_address="Piassa, Addis Ababa",
            latitude="9.030000",
            longitude="38.740000",
            phone_number=self.customer.phone_number,
            subtotal="100.00",
            delivery_fee="10.00",
            total="110.00",
            paid_at=timezone.now(),
            delivered_at=timezone.now(),
        )
        Payment.objects.create(
            order=order,
            provider=PaymentProvider.CHAPA,
            status=PaymentStatus.PAID,
            amount=order.total,
            reference="CHAPA-AGENT-RFD-1",
            paid_at=timezone.now(),
        )
        refund_request = request_order_refund(order, self.customer, "The water arrived leaking.")

        response = self.client.post(
            reverse("accounts:agent_approve_refund", kwargs={"pk": refund_request.pk}),
            {
                "approved_amount": str(order.total),
                "resolution_note": "Approved after branch delivery review.",
            },
        )

        self.assertRedirects(response, reverse("accounts:agent_refunds"))
        refund_request.refresh_from_db()
        order.payment.refresh_from_db()
        self.assertEqual(refund_request.status, RefundRequestStatus.APPROVED)
        self.assertEqual(order.payment.status, PaymentStatus.PAID)

        response = self.client.post(reverse("accounts:agent_process_refund", kwargs={"pk": refund_request.pk}))
        self.assertRedirects(response, reverse("accounts:agent_refunds"))
        refund_request.refresh_from_db()
        order.payment.refresh_from_db()
        self.assertEqual(refund_request.status, RefundRequestStatus.PROCESSED)
        self.assertEqual(order.payment.status, PaymentStatus.REFUNDED)

    def test_agent_manager_can_submit_batch_stock_request(self):
        response = self.client.post(
            reverse("accounts:agent_batch_request_create"),
            {
                "batch": self.company_batch.pk,
                "quantity_requested": 50,
                "payment_type": AgentBatchSalePaymentType.PARTIAL,
                "requested_upfront_amount": "1000.00",
                "requested_note": "Need hotel distribution stock for the weekend.",
            },
        )

        self.assertRedirects(response, reverse("accounts:agent_inventory"))
        sale = AgentBatchSale.objects.get(agent=self.agent, batch=self.company_batch)
        self.assertEqual(sale.status, AgentBatchSaleStatus.PENDING)
        self.assertEqual(sale.quantity_requested, 50)
        self.assertEqual(sale.payment_type, AgentBatchSalePaymentType.PARTIAL)

    def test_agent_manager_can_submit_payment_against_approved_batch_sale(self):
        sale = AgentBatchSale.objects.create(
            agent=self.agent,
            batch=self.company_batch,
            requested_by=self.agent_manager,
            approved_by=self.company_admin,
            quantity_requested=30,
            quantity_approved=30,
            payment_type=AgentBatchSalePaymentType.CREDIT,
            unit_price="85.00",
            credit_due_date=timezone.localdate() + timezone.timedelta(days=7),
            status=AgentBatchSaleStatus.APPROVED,
            approved_at=timezone.now(),
        )

        response = self.client.post(
            reverse("accounts:agent_batch_payment_create", kwargs={"pk": sale.pk}),
            {
                "amount": "750.00",
                "submitted_note": "Collected from event clients and remitting part of the balance.",
            },
        )

        self.assertRedirects(response, reverse("accounts:agent_inventory"))
        payment = AgentBatchSalePayment.objects.get(sale=sale)
        self.assertEqual(payment.status, AgentBatchSalePaymentStatus.PENDING)
        self.assertEqual(payment.submitted_by, self.agent_manager)
        self.assertEqual(payment.amount, Decimal("750.00"))

    def test_agent_manager_can_confirm_batch_sale_receipt_and_credit_due_date_is_receipt_anchored(self):
        sale = AgentBatchSale.objects.create(
            agent=self.agent,
            batch=self.company_batch,
            requested_by=self.agent_manager,
            approved_by=self.company_admin,
            quantity_requested=30,
            quantity_approved=30,
            payment_type=AgentBatchSalePaymentType.CREDIT,
            unit_price="85.00",
            credit_terms_days=7,
            status=AgentBatchSaleStatus.APPROVED,
            approved_at=timezone.now(),
        )

        response = self.client.post(
            reverse("accounts:agent_batch_sale_confirm_receipt", kwargs={"pk": sale.pk}),
            {"receipt_note": "Warehouse team counted and accepted the full load."},
        )

        self.assertRedirects(response, reverse("accounts:agent_inventory"))
        sale.refresh_from_db()
        self.company_batch.refresh_from_db()
        stock = AgentStock.objects.get(agent=self.agent, product=self.product)
        inventory_batch = InventoryBatch.objects.get(agent=self.agent, batch_number=self.company_batch.batch_number)
        transaction = InventoryTransaction.objects.get(
            reference=f"BATCH-SALE-{sale.pk}",
            transaction_type=InventoryTransactionType.RESTOCK,
        )

        self.assertEqual(sale.status, AgentBatchSaleStatus.RECEIVED)
        self.assertEqual(sale.quantity_received, 30)
        self.assertEqual(sale.received_by, self.agent_manager)
        self.assertEqual(
            sale.credit_due_date,
            timezone.localtime(sale.received_at).date() + timezone.timedelta(days=7),
        )
        self.assertEqual(self.company_batch.unsold_cases_remaining, 370)
        self.assertEqual(stock.available_quantity, 40)
        self.assertEqual(inventory_batch.quantity_remaining, 30)
        self.assertEqual(transaction.quantity_change, 30)

    def test_agent_manager_is_blocked_when_credit_limit_is_already_exceeded(self):
        self.agent.credit_limit = Decimal("2000.00")
        self.agent.save(update_fields=["credit_limit", "updated_at"])
        approved_sale = AgentBatchSale.objects.create(
            agent=self.agent,
            batch=self.company_batch,
            requested_by=self.agent_manager,
            approved_by=self.company_admin,
            quantity_requested=40,
            quantity_approved=40,
            payment_type=AgentBatchSalePaymentType.CREDIT,
            unit_price="60.00",
            credit_due_date=timezone.localdate() + timezone.timedelta(days=7),
            status=AgentBatchSaleStatus.APPROVED,
            approved_at=timezone.now(),
        )
        AgentBatchSalePayment.objects.create(
            sale=approved_sale,
            amount="100.00",
            submitted_by=self.company_admin,
            confirmed_by=self.company_admin,
            status=AgentBatchSalePaymentStatus.CONFIRMED,
            confirmed_at=timezone.now(),
        )

        response = self.client.post(
            reverse("accounts:agent_batch_request_create"),
            {
                "batch": self.company_batch.pk,
                "quantity_requested": 10,
                "payment_type": AgentBatchSalePaymentType.CREDIT,
                "requested_upfront_amount": "0.00",
                "requested_note": "Try again",
            },
            follow=True,
        )

        self.assertContains(response, "above the credit limit")


class CompanyAdminPortalTests(TestCase):
    def setUp(self):
        self.company_admin = User.objects.create_user(
            email="company-admin@example.com",
            password="StrongPass123!",
            first_name="Company",
            last_name="Admin",
            phone_number="+251911000060",
            role=UserRole.COMPANY_ADMIN,
            is_active=True,
        )
        self.agent_manager = User.objects.create_user(
            email="branch-manager@example.com",
            password="StrongPass123!",
            first_name="Branch",
            last_name="Manager",
            phone_number="+251911000061",
            role=UserRole.AGENT_MANAGER,
            is_active=True,
        )
        self.company = Company.objects.create(
            name="Crystal Drop",
            description="Bulk bottled water supplier",
            location="Addis Ababa",
            is_verified=True,
            admin=self.company_admin,
        )
        self.company_admin.managed_company = self.company
        self.company_admin.save(update_fields=["managed_company", "updated_at"])
        self.agent = Agent.objects.create(
            company=self.company,
            name="Crystal Drop Bole",
            location_name="Bole",
            address="Bole Atlas, Addis Ababa",
            latitude="9.015000",
            longitude="38.770000",
            service_radius_km="18.00",
            phone_number="+251911000062",
            is_active=True,
            is_accepting_orders=True,
            admin=self.agent_manager,
        )
        self.product = Product.objects.create(
            company=self.company,
            name="20L Premium Jar",
            size_label="20L",
            description="Office delivery water jar",
            price="140.00",
            available_quantity=500,
            is_active=True,
        )
        self.company_batch = CompanyBatch.objects.create(
            company=self.company,
            product=self.product,
            batch_number="BATCH-2026-COMPANY1",
            production_date=timezone.localdate(),
            total_cases_produced=300,
            unsold_cases_remaining=300,
            unit_price="118.00",
            created_by=self.company_admin,
        )
        AgentStock.objects.create(
            agent=self.agent,
            product=self.product,
            available_quantity=25,
            reorder_level=5,
        )
        self.customer = User.objects.create_user(
            email="company-customer@example.com",
            password="StrongPass123!",
            first_name="Grand",
            last_name="Hotel",
            phone_number="+251911000063",
            is_active=True,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            selected_agent=self.agent,
            order_number="ORD-COMPANY1",
            status=OrderStatus.DELIVERED,
            delivery_address="Kazanchis, Addis Ababa",
            latitude="9.020000",
            longitude="38.750000",
            phone_number=self.customer.phone_number,
            subtotal="280.00",
            delivery_fee="20.00",
            total="300.00",
            paid_at=timezone.now(),
            delivered_at=timezone.now(),
        )
        self.order.items.create(
            product=self.product,
            product_name=self.product.name,
            unit_price="140.00",
            quantity=2,
        )
        self.client.force_login(self.company_admin)

    def test_company_admin_can_create_product_and_seed_agent_stock_rows(self):
        response = self.client.post(
            reverse("accounts:company_product_create"),
            {
                "name": "0.5L Daily Sport",
                "size_label": "0.5L",
                "description": "Small single-use bottle",
                "price": "35.00",
                "available_quantity": 1200,
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("accounts:company_products"))
        product = Product.objects.get(company=self.company, name="0.5L Daily Sport")
        self.assertEqual(product.size_label, "0.5L")
        self.assertTrue(AgentStock.objects.filter(agent=self.agent, product=product, available_quantity=0).exists())

    def test_company_admin_can_toggle_product_active_state(self):
        response = self.client.post(reverse("accounts:company_product_toggle", kwargs={"pk": self.product.pk}))

        self.assertRedirects(response, reverse("accounts:company_products"))
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)

    def test_company_admin_can_update_agent_branch(self):
        response = self.client.post(
            reverse("accounts:company_agent_edit", kwargs={"pk": self.agent.pk}),
            {
                "name": "Crystal Drop CMC",
                "description": "Expanded east-side branch",
                "location_name": "CMC",
                "address": "CMC Roundabout, Addis Ababa",
                "latitude": "9.050000",
                "longitude": "38.800000",
                "service_radius_km": "22.00",
                "phone_number": "+251911000064",
                "is_active": "on",
                "is_accepting_orders": "on",
                "admin": self.agent_manager.pk,
            },
        )

        self.assertRedirects(response, reverse("accounts:company_agents"))
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.name, "Crystal Drop CMC")
        self.assertEqual(self.agent.location_name, "CMC")
        self.assertEqual(self.agent.phone_number, "+251911000064")

    def test_company_inventory_page_shows_aggregated_stock(self):
        response = self.client.get(reverse("accounts:company_inventory"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.product.name)
        self.assertContains(response, self.agent.name)
        self.assertContains(response, "25")

    def test_company_dashboard_uses_company_admin_managed_company_assignment(self):
        primary_contact = User.objects.create_user(
            email="ops-lead@example.com",
            password="StrongPass123!",
            first_name="Ops",
            last_name="Lead",
            phone_number="+251911000099",
            role=UserRole.COMPANY_ADMIN,
            is_active=True,
            managed_company=self.company,
        )
        self.company.admin = primary_contact
        self.company.save(update_fields=["admin", "updated_at"])

        response = self.client.get(reverse("accounts:company_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Operational Overview")
        self.assertContains(response, "Crystal Drop")

    def test_company_admin_can_seed_starter_catalog_and_batches(self):
        empty_company = Company.objects.create(
            name="Fresh Spring Co",
            description="New bottler with no starter data yet",
            location="Bishoftu",
            is_verified=True,
            admin=self.company_admin,
        )
        self.company_admin.managed_company = empty_company
        self.company_admin.save(update_fields=["managed_company", "updated_at"])

        response = self.client.post(reverse("accounts:company_product_seed_starter"))

        self.assertRedirects(response, reverse("accounts:company_products"))
        self.assertTrue(Product.objects.filter(company=empty_company, name="0.5L Daily Sport").exists())
        self.assertTrue(CompanyBatch.objects.filter(company=empty_company, product__name="0.5L Daily Sport").exists())

    def test_company_admin_can_create_production_batch(self):
        response = self.client.post(
            reverse("accounts:company_batch_create"),
            {
                "product": self.product.pk,
                "batch_number": "BATCH-2026-COMPANY2",
                "production_date": timezone.localdate().isoformat(),
                "total_cases_produced": 420,
                "unit_price": "125.00",
                "note": "Fresh factory run for the west-side hotels.",
            },
        )

        self.assertRedirects(response, reverse("accounts:company_inventory"))
        batch = CompanyBatch.objects.get(company=self.company, batch_number="BATCH-2026-COMPANY2")
        self.assertEqual(batch.unsold_cases_remaining, 420)
        self.assertEqual(batch.status, CompanyBatchStatus.AVAILABLE)

    def test_company_admin_can_approve_agent_batch_request_without_allocating_stock_until_receipt(self):
        sale = AgentBatchSale.objects.create(
            agent=self.agent,
            batch=self.company_batch,
            requested_by=self.agent_manager,
            quantity_requested=40,
            payment_type=AgentBatchSalePaymentType.PARTIAL,
            requested_upfront_amount="1000.00",
            unit_price=self.company_batch.unit_price,
        )

        response = self.client.post(
            reverse("accounts:company_batch_sale_approve", kwargs={"pk": sale.pk}),
            {
                "quantity_approved": 35,
                "unit_price": "120.00",
                "initial_payment_amount": "1400.00",
                "credit_terms_days": 10,
                "decision_note": "Approved slightly below the requested quantity.",
            },
        )

        self.assertRedirects(response, reverse("accounts:company_inventory"))
        sale.refresh_from_db()
        self.company_batch.refresh_from_db()
        stock = AgentStock.objects.get(agent=self.agent, product=self.product)
        payment = AgentBatchSalePayment.objects.get(sale=sale)

        self.assertEqual(sale.status, AgentBatchSaleStatus.APPROVED)
        self.assertEqual(sale.quantity_approved, 35)
        self.assertEqual(sale.credit_terms_days, 10)
        self.assertIsNone(sale.credit_due_date)
        self.assertEqual(self.company_batch.unsold_cases_remaining, 300)
        self.assertEqual(stock.available_quantity, 25)
        self.assertFalse(InventoryBatch.objects.filter(agent=self.agent, batch_number=self.company_batch.batch_number).exists())
        self.assertEqual(payment.status, AgentBatchSalePaymentStatus.CONFIRMED)

    def test_company_admin_can_cancel_approved_batch_sale_without_stock_reversal(self):
        sale = AgentBatchSale.objects.create(
            agent=self.agent,
            batch=self.company_batch,
            requested_by=self.agent_manager,
            approved_by=self.company_admin,
            quantity_requested=20,
            quantity_approved=20,
            payment_type=AgentBatchSalePaymentType.CREDIT,
            unit_price="100.00",
            credit_terms_days=7,
            status=AgentBatchSaleStatus.APPROVED,
            approved_at=timezone.now(),
        )

        self.assertEqual(get_agent_open_batch_balance(self.agent), Decimal("2000.00"))

        response = self.client.post(
            reverse("accounts:company_batch_sale_cancel", kwargs={"pk": sale.pk}),
            {"reason": "Truck never dispatched."},
        )

        self.assertRedirects(response, reverse("accounts:company_inventory"))
        sale.refresh_from_db()
        self.company_batch.refresh_from_db()
        stock = AgentStock.objects.get(agent=self.agent, product=self.product)

        self.assertEqual(sale.status, AgentBatchSaleStatus.CANCELLED)
        self.assertEqual(self.company_batch.unsold_cases_remaining, 300)
        self.assertEqual(stock.available_quantity, 25)
        self.assertEqual(get_agent_open_batch_balance(self.agent), Decimal("0.00"))

    def test_company_admin_can_confirm_agent_batch_payment_submission(self):
        sale = AgentBatchSale.objects.create(
            agent=self.agent,
            batch=self.company_batch,
            requested_by=self.agent_manager,
            approved_by=self.company_admin,
            quantity_requested=30,
            quantity_approved=30,
            payment_type=AgentBatchSalePaymentType.CREDIT,
            unit_price="119.00",
            credit_due_date=timezone.localdate() + timezone.timedelta(days=14),
            status=AgentBatchSaleStatus.APPROVED,
            approved_at=timezone.now(),
        )
        payment = AgentBatchSalePayment.objects.create(
            sale=sale,
            amount="1500.00",
            submitted_by=self.agent_manager,
            submitted_note="Weekend hotel collections remitted.",
            status=AgentBatchSalePaymentStatus.PENDING,
        )

        response = self.client.post(
            reverse("accounts:company_batch_payment_confirm", kwargs={"pk": payment.pk}),
        )

        self.assertRedirects(response, reverse("accounts:company_inventory"))
        payment.refresh_from_db()
        self.assertEqual(payment.status, AgentBatchSalePaymentStatus.CONFIRMED)
        self.assertEqual(payment.confirmed_by, self.company_admin)
        self.assertIsNotNone(payment.confirmed_at)

    def test_company_admin_can_recall_batch_and_block_future_approvals(self):
        sale = AgentBatchSale.objects.create(
            agent=self.agent,
            batch=self.company_batch,
            requested_by=self.agent_manager,
            quantity_requested=20,
            payment_type=AgentBatchSalePaymentType.CREDIT,
            unit_price=self.company_batch.unit_price,
        )

        recall_response = self.client.post(
            reverse("accounts:company_batch_recall", kwargs={"pk": self.company_batch.pk}),
            {"reason": "Labelling issue detected during QA review."},
        )
        approve_response = self.client.post(
            reverse("accounts:company_batch_sale_approve", kwargs={"pk": sale.pk}),
            {
                "quantity_approved": 20,
                "unit_price": "118.00",
                "initial_payment_amount": "0.00",
                "credit_terms_days": 7,
                "decision_note": "Attempt after recall",
            },
            follow=True,
        )

        self.assertRedirects(recall_response, reverse("accounts:company_batch_detail", kwargs={"pk": self.company_batch.pk}))
        self.company_batch.refresh_from_db()
        sale.refresh_from_db()
        self.assertEqual(self.company_batch.status, CompanyBatchStatus.RECALLED)
        self.assertEqual(sale.status, AgentBatchSaleStatus.PENDING)
        self.assertContains(approve_response, "recalled batch")

    @override_settings(AGENT_BATCH_RECEIPT_AUTO_CONFIRM_DAYS=5)
    def test_auto_confirm_stale_agent_batch_sales_marks_sale_received_with_system_actor(self):
        sale = AgentBatchSale.objects.create(
            agent=self.agent,
            batch=self.company_batch,
            requested_by=self.agent_manager,
            approved_by=self.company_admin,
            quantity_requested=12,
            quantity_approved=12,
            payment_type=AgentBatchSalePaymentType.CREDIT,
            unit_price="118.00",
            credit_terms_days=5,
            status=AgentBatchSaleStatus.APPROVED,
            approved_at=timezone.now() - timezone.timedelta(days=6),
        )

        summary = auto_confirm_stale_agent_batch_sales()

        sale.refresh_from_db()
        self.company_batch.refresh_from_db()
        stock = AgentStock.objects.get(agent=self.agent, product=self.product)

        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(sale.status, AgentBatchSaleStatus.RECEIVED)
        self.assertEqual(sale.quantity_received, 12)
        self.assertIsNone(sale.received_by)
        self.assertEqual(
            sale.receipt_note,
            "Auto-confirmed after 5 days with no manual confirmation.",
        )
        self.assertEqual(self.company_batch.unsold_cases_remaining, 288)
        self.assertEqual(stock.available_quantity, 37)

    def test_cancel_received_batch_sale_reverses_stock_and_excludes_open_balance(self):
        sale = AgentBatchSale.objects.create(
            agent=self.agent,
            batch=self.company_batch,
            requested_by=self.agent_manager,
            approved_by=self.company_admin,
            quantity_requested=10,
            quantity_approved=10,
            payment_type=AgentBatchSalePaymentType.CREDIT,
            unit_price="118.00",
            credit_terms_days=7,
            status=AgentBatchSaleStatus.APPROVED,
            approved_at=timezone.now(),
        )
        confirm_agent_batch_sale_receipt(sale=sale, received_by=self.agent_manager, receipt_note="Warehouse handoff complete.")

        self.assertEqual(get_agent_open_batch_balance(self.agent), Decimal("1180.00"))

        cancel_agent_batch_sale(
            sale=sale,
            cancelled_by=self.company_admin,
            reason="Customer branch rejected the load after inspection.",
        )

        sale.refresh_from_db()
        self.company_batch.refresh_from_db()
        stock = AgentStock.objects.get(agent=self.agent, product=self.product)
        inventory_batch = InventoryBatch.objects.get(agent=self.agent, batch_number=self.company_batch.batch_number)
        reversal_transaction = InventoryTransaction.objects.filter(
            reference=f"BATCH-SALE-{sale.pk}",
            transaction_type=InventoryTransactionType.RETURN,
        ).latest("created_at")

        self.assertEqual(sale.status, AgentBatchSaleStatus.CANCELLED)
        self.assertEqual(self.company_batch.unsold_cases_remaining, 300)
        self.assertEqual(stock.available_quantity, 25)
        self.assertEqual(inventory_batch.quantity_remaining, 0)
        self.assertEqual(reversal_transaction.quantity_change, -10)
        self.assertEqual(get_agent_open_batch_balance(self.agent), Decimal("0.00"))

    def test_notify_overdue_agent_batch_sales_only_notifies_once_per_sale(self):
        sale = AgentBatchSale.objects.create(
            agent=self.agent,
            batch=self.company_batch,
            requested_by=self.agent_manager,
            approved_by=self.company_admin,
            quantity_requested=18,
            quantity_approved=18,
            quantity_received=18,
            payment_type=AgentBatchSalePaymentType.CREDIT,
            unit_price="118.00",
            credit_terms_days=7,
            credit_due_date=timezone.localdate() - timezone.timedelta(days=1),
            status=AgentBatchSaleStatus.RECEIVED,
            approved_at=timezone.now() - timezone.timedelta(days=10),
            received_at=timezone.now() - timezone.timedelta(days=8),
            received_by=self.agent_manager,
        )

        first_run = notify_overdue_agent_batch_sales()
        second_run = notify_overdue_agent_batch_sales()

        sale.refresh_from_db()
        self.assertEqual(first_run["notified"], 1)
        self.assertEqual(second_run["notified"], 0)
        self.assertIsNotNone(sale.overdue_notified_at)
        self.assertEqual(Notification.objects.filter(recipient=self.company_admin, title="Agent batch sale overdue").count(), 1)

    def test_company_reports_support_excel_and_pdf_exports(self):
        date_value = timezone.localdate().isoformat()

        excel_response = self.client.get(
            reverse("accounts:company_reports_export", kwargs={"export_format": "excel"}),
            {"date_from": date_value, "date_to": date_value},
        )
        pdf_response = self.client.get(
            reverse("accounts:company_reports_export", kwargs={"export_format": "pdf"}),
            {"date_from": date_value, "date_to": date_value},
        )

        self.assertEqual(excel_response.status_code, 200)
        self.assertIn("application/vnd.ms-excel", excel_response["Content-Type"])
        self.assertIn(self.product.name.encode("utf-8"), excel_response.content)
        self.assertEqual(pdf_response.status_code, 200)
        self.assertIn("application/pdf", pdf_response["Content-Type"])
        self.assertTrue(pdf_response.content.startswith(b"%PDF"))

    def test_company_ai_page_is_marked_coming_soon(self):
        response = self.client.get(reverse("accounts:company_ai_insights"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Coming soon")


class DriverPortalTests(TestCase):
    def setUp(self):
        self.agent_manager = User.objects.create_user(
            email="driver-agent-manager@example.com",
            password="StrongPass123!",
            first_name="Branch",
            last_name="Lead",
            phone_number="+251911000065",
            role=UserRole.AGENT_MANAGER,
            is_active=True,
        )
        self.driver_user = User.objects.create_user(
            email="mobile-driver@example.com",
            password="StrongPass123!",
            first_name="Mobile",
            last_name="Driver",
            phone_number="+251911000066",
            role=UserRole.DRIVER,
            is_active=True,
        )
        self.company_admin = User.objects.create_user(
            email="driver-company-admin@example.com",
            password="StrongPass123!",
            first_name="Company",
            last_name="Supervisor",
            phone_number="+251911000067",
            role=UserRole.COMPANY_ADMIN,
            is_active=True,
        )
        self.company = Company.objects.create(
            name="Driver Flow Co",
            description="Delivery operator",
            location="Addis Ababa",
            is_verified=True,
            admin=self.company_admin,
        )
        self.agent = Agent.objects.create(
            company=self.company,
            name="Driver Flow Bole",
            location_name="Bole",
            address="Bole, Addis Ababa",
            latitude="9.010000",
            longitude="38.760000",
            service_radius_km="18.00",
            phone_number="+251911000068",
            is_active=True,
            is_accepting_orders=True,
            admin=self.agent_manager,
        )
        self.driver = Driver.objects.create(
            agent=self.agent,
            user=self.driver_user,
            vehicle_identifier="DRV-101",
            phone_number=self.driver_user.phone_number,
            is_active=True,
        )
        self.customer = User.objects.create_user(
            email="driver-portal-customer@example.com",
            password="StrongPass123!",
            first_name="Hotel",
            last_name="Manager",
            phone_number="+251911000069",
            is_active=True,
        )
        self.product = Product.objects.create(
            company=self.company,
            name="5L Pack",
            description="Family pack",
            price="60.00",
            available_quantity=100,
        )
        self.client.force_login(self.driver_user)

    def test_driver_can_update_availability_when_no_active_delivery(self):
        response = self.client.post(
            reverse("accounts:driver_availability"),
            {"availability_status": Driver.AvailabilityStatus.OFF_DUTY},
        )

        self.assertRedirects(response, reverse("accounts:driver_dashboard"))
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.availability_status, Driver.AvailabilityStatus.OFF_DUTY)

    def test_driver_history_page_loads(self):
        order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            selected_agent=self.agent,
            assigned_driver=self.driver,
            order_number="ORD-DRVHIST",
            status=OrderStatus.DELIVERED,
            delivery_address="CMC, Addis Ababa",
            latitude="9.030000",
            longitude="38.780000",
            phone_number=self.customer.phone_number,
            subtotal="60.00",
            delivery_fee="10.00",
            total="70.00",
            paid_at=timezone.now(),
            delivered_at=timezone.now(),
        )
        order.items.create(
            product=self.product,
            product_name=self.product.name,
            unit_price="60.00",
            quantity=1,
        )

        response = self.client.get(reverse("accounts:driver_history"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, order.order_number)


class SystemUserListQueryTests(TestCase):
    def setUp(self):
        self.system_admin = User.objects.create_user(
            email="query-admin@example.com",
            password="StrongPass123!",
            first_name="Query",
            last_name="Admin",
            phone_number="+251911009000",
            role=UserRole.SYSTEM_ADMIN,
            is_active=True,
        )
        self.base_company = Company.objects.create(
            name="Query Water",
            description="Query test supplier",
            location="Addis Ababa",
            is_verified=True,
        )
        self.base_agent_manager = User.objects.create_user(
            email="query-agent-manager@example.com",
            password="StrongPass123!",
            first_name="Query",
            last_name="Manager",
            phone_number="+251911009001",
            role=UserRole.AGENT_MANAGER,
            is_active=True,
        )
        self.base_agent = Agent.objects.create(
            company=self.base_company,
            name="Query Bole Agent",
            location_name="Bole",
            address="Bole, Addis Ababa",
            latitude="9.010000",
            longitude="38.760000",
            service_radius_km="20.00",
            phone_number="+251911009002",
            is_active=True,
            is_accepting_orders=True,
            admin=self.base_agent_manager,
        )
        self.client.force_login(self.system_admin)

    def create_system_user_fixture_batch(self, start_index, count):
        for idx in range(start_index, start_index + count):
            pattern = idx % 5
            phone_number = f"+2519{idx:08d}"
            email = f"system-user-{idx}@example.com"

            if pattern == 0:
                user = User.objects.create_user(
                    email=email,
                    password="StrongPass123!",
                    first_name=f"Managed{idx}",
                    last_name="Admin",
                    phone_number=phone_number,
                    role=UserRole.COMPANY_ADMIN,
                    is_active=True,
                )
                managed_company = Company.objects.create(
                    name=f"Managed Company {idx}",
                    description="Managed company for query testing",
                    location="Adama",
                    is_verified=True,
                    admin=user,
                )
                user.managed_company = managed_company
                user.save(update_fields=["managed_company", "updated_at"])
            elif pattern == 1:
                user = User.objects.create_user(
                    email=email,
                    password="StrongPass123!",
                    first_name=f"Branch{idx}",
                    last_name="Manager",
                    phone_number=phone_number,
                    role=UserRole.AGENT_MANAGER,
                    is_active=True,
                )
                Agent.objects.create(
                    company=self.base_company,
                    name=f"Branch Agent {idx}",
                    location_name=f"Area {idx}",
                    address="Addis Ababa",
                    latitude="9.020000",
                    longitude="38.770000",
                    service_radius_km="18.00",
                    phone_number=f"+2518{idx:08d}",
                    is_active=True,
                    is_accepting_orders=True,
                    admin=user,
                )
            elif pattern == 2:
                user = User.objects.create_user(
                    email=email,
                    password="StrongPass123!",
                    first_name=f"Fleet{idx}",
                    last_name="Driver",
                    phone_number=phone_number,
                    role=UserRole.DRIVER,
                    is_active=True,
                )
                Driver.objects.create(
                    agent=self.base_agent,
                    user=user,
                    vehicle_identifier=f"DRV-{idx}",
                    phone_number=phone_number,
                    is_active=True,
                )
            elif pattern == 3:
                user = User.objects.create_user(
                    email=email,
                    password="StrongPass123!",
                    first_name=f"Customer{idx}",
                    last_name="User",
                    phone_number=phone_number,
                    role=UserRole.CUSTOMER,
                    is_active=True,
                )
                Order.objects.create(
                    customer=user,
                    company=self.base_company,
                    selected_agent=self.base_agent,
                    order_number=f"ORD-QUERY-{idx}",
                    status=OrderStatus.DELIVERED,
                    delivery_address="Kazanchis, Addis Ababa",
                    latitude="9.020000",
                    longitude="38.750000",
                    phone_number=user.phone_number,
                    subtotal="90.00",
                    delivery_fee="20.00",
                    total="110.00",
                    paid_at=timezone.now(),
                    delivered_at=timezone.now(),
                )
            else:
                User.objects.create_user(
                    email=email,
                    password="StrongPass123!",
                    first_name=f"Platform{idx}",
                    last_name="Only",
                    phone_number=phone_number,
                    role=UserRole.CUSTOMER,
                    is_active=True,
                )

    def capture_system_user_list_query_count(self, *, warm_up=False):
        if warm_up:
            warm_up_response = self.client.get(reverse("accounts:system_users"))
            self.assertEqual(warm_up_response.status_code, 200)
        with CaptureQueriesContext(connection) as captured_queries:
            response = self.client.get(reverse("accounts:system_users"))
        self.assertEqual(response.status_code, 200)
        return len(captured_queries)

    def test_system_user_list_query_count_stays_constant_as_user_count_grows(self):
        self.create_system_user_fixture_batch(start_index=1, count=5)
        queries_with_five_fixture_users = self.capture_system_user_list_query_count(warm_up=True)

        self.create_system_user_fixture_batch(start_index=6, count=45)
        queries_with_fifty_fixture_users = self.capture_system_user_list_query_count(warm_up=True)

        self.assertEqual(queries_with_five_fixture_users, queries_with_fifty_fixture_users)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SystemAdminPortalTests(TestCase):
    def setUp(self):
        self.system_admin = User.objects.create_user(
            email="sys-admin@example.com",
            password="StrongPass123!",
            first_name="System",
            last_name="Admin",
            phone_number="+251911000070",
            role=UserRole.SYSTEM_ADMIN,
            is_active=True,
        )
        self.company_admin = User.objects.create_user(
            email="company-admin-2@example.com",
            password="StrongPass123!",
            first_name="Company",
            last_name="Lead",
            phone_number="+251911000071",
            role=UserRole.COMPANY_ADMIN,
            is_active=True,
        )
        self.company = Company.objects.create(
            name="Blue Nile Water",
            description="National supplier",
            location="Addis Ababa",
            admin=self.company_admin,
            verification_status="pending_efda",
            is_verified=False,
            submitted_to_efda_at=timezone.now(),
        )
        self.agent_manager = User.objects.create_user(
            email="ops-manager@example.com",
            password="StrongPass123!",
            first_name="Ops",
            last_name="Manager",
            phone_number="+251911000072",
            role=UserRole.AGENT_MANAGER,
            is_active=True,
        )
        self.agent = Agent.objects.create(
            company=self.company,
            name="Blue Nile Bole",
            location_name="Bole",
            address="Bole, Addis Ababa",
            latitude="9.010000",
            longitude="38.760000",
            service_radius_km="20.00",
            phone_number="+251911000073",
            is_active=True,
            is_accepting_orders=True,
            admin=self.agent_manager,
        )
        self.driver_user = User.objects.create_user(
            email="fleet-driver@example.com",
            password="StrongPass123!",
            first_name="Fleet",
            last_name="Driver",
            phone_number="+251911000074",
            role=UserRole.DRIVER,
            is_active=True,
        )
        self.driver = Driver.objects.create(
            agent=self.agent,
            user=self.driver_user,
            vehicle_identifier="AA-2020",
            phone_number=self.driver_user.phone_number,
            is_active=True,
        )
        self.customer = User.objects.create_user(
            email="hotel@example.com",
            password="StrongPass123!",
            first_name="Grand",
            last_name="Hotel",
            phone_number="+251911000075",
            is_active=True,
        )
        self.product = Product.objects.create(
            company=self.company,
            name="18L Refill",
            size_label="18L",
            description="Refill water",
            price="90.00",
            available_quantity=200,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            selected_agent=self.agent,
            assigned_driver=self.driver,
            order_number="ORD-SYS001",
            status=OrderStatus.DELIVERED,
            delivery_address="Kazanchis, Addis Ababa",
            latitude="9.020000",
            longitude="38.750000",
            phone_number=self.customer.phone_number,
            subtotal="180.00",
            delivery_fee="20.00",
            total="200.00",
            paid_at=timezone.now(),
            delivered_at=timezone.now(),
        )
        self.order.items.create(
            product=self.product,
            product_name=self.product.name,
            unit_price="90.00",
            quantity=2,
        )
        self.client.force_login(self.system_admin)

    def test_system_admin_can_register_company_with_document_and_audit_log(self):
        response = self.client.post(
            reverse("accounts:create_company"),
            {
                "name": "Aqua Capital",
                "description": "Regional bottler",
                "location": "Adama",
                "address": "Adama Industrial Zone",
                "latitude": "8.540000",
                "longitude": "39.270000",
                "contact_email": "ops@aquacapital.example.com",
                "contact_phone": "+251911000080",
                "efda_license_number": "EFDA-2026-001",
                "registration_document": SimpleUploadedFile("license.pdf", b"fake-pdf", content_type="application/pdf"),
                "admin_first_name": "Marta",
                "admin_last_name": "Tesfaye",
                "admin_email": "marta@aquacapital.example.com",
                "admin_phone_number": "+251911000081",
                "admin_password": "StrongPass123!",
                "next": reverse("accounts:system_companies"),
            },
        )

        self.assertRedirects(response, reverse("accounts:system_companies"))
        company = Company.objects.get(name="Aqua Capital")
        self.assertEqual(company.verification_status, "pending_efda")
        self.assertTrue(bool(company.registration_document))
        self.assertIsNotNone(company.admin)
        self.assertEqual(company.admin.email, "marta@aquacapital.example.com")
        self.assertFalse(company.admin.is_active)
        self.assertEqual(company.admin.managed_company, company)
        self.assertTrue(AuditLog.objects.filter(action="company.created", entity_label="Aqua Capital").exists())

    def test_system_admin_created_company_admin_for_pending_company_stays_inactive(self):
        response = self.client.post(
            reverse("accounts:create_company_admin"),
            {
                "first_name": "Pending",
                "last_name": "Admin",
                "email": "pending-admin@example.com",
                "phone_number": "+251911000082",
                "password": "StrongPass123!",
                "role": UserRole.COMPANY_ADMIN,
                "company_id": self.company.pk,
                "next": reverse("accounts:system_dashboard"),
            },
        )

        self.assertRedirects(response, reverse("accounts:system_dashboard"))
        admin_user = User.objects.get(email="pending-admin@example.com")
        self.assertEqual(admin_user.managed_company, self.company)
        self.assertFalse(admin_user.is_active)
        self.assertTrue(AuditLog.objects.filter(action="user.created", entity_label=admin_user.email).exists())

    def test_system_admin_can_edit_user_role_and_active_state(self):
        response = self.client.post(
            reverse("accounts:system_user_edit", kwargs={"pk": self.driver_user.pk}),
            {
                "first_name": "Fleet",
                "last_name": "Supervisor",
                "email": self.driver_user.email,
                "phone_number": self.driver_user.phone_number,
                "role": UserRole.AGENT_MANAGER,
                "is_active": "",
            },
        )

        self.assertRedirects(response, reverse("accounts:system_users"))
        self.driver_user.refresh_from_db()
        self.assertEqual(self.driver_user.role, UserRole.AGENT_MANAGER)
        self.assertFalse(self.driver_user.is_active)
        self.assertTrue(AuditLog.objects.filter(action="user.updated", entity_label=self.driver_user.email).exists())

    def test_pending_company_admin_assignment_forces_inactive_even_when_activation_is_requested(self):
        response = self.client.post(
            reverse("accounts:system_user_edit", kwargs={"pk": self.driver_user.pk}),
            {
                "first_name": "Fleet",
                "last_name": "Driver",
                "email": self.driver_user.email,
                "phone_number": self.driver_user.phone_number,
                "role": UserRole.COMPANY_ADMIN,
                "managed_company": self.company.pk,
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("accounts:system_users"))
        self.driver_user.refresh_from_db()
        self.assertEqual(self.driver_user.role, UserRole.COMPANY_ADMIN)
        self.assertEqual(self.driver_user.managed_company, self.company)
        self.assertFalse(self.driver_user.is_active)

    def test_system_admin_bulk_action_can_deactivate_selected_users(self):
        response = self.client.post(
            reverse("accounts:system_users_bulk_action"),
            {
                "action": "deactivate",
                "user_ids": [self.customer.pk, self.driver_user.pk],
            },
        )

        self.assertRedirects(response, reverse("accounts:system_users"))
        self.customer.refresh_from_db()
        self.driver_user.refresh_from_db()
        self.assertFalse(self.customer.is_active)
        self.assertFalse(self.driver_user.is_active)

    def test_system_admin_can_send_password_reset_email(self):
        response = self.client.post(reverse("accounts:system_user_send_reset", kwargs={"pk": self.customer.pk}))

        self.assertRedirects(response, reverse("accounts:system_users"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.customer.email, mail.outbox[0].to)
        self.assertTrue(AuditLog.objects.filter(action="user.password_reset_requested", entity_label=self.customer.email).exists())

    def test_system_admin_can_send_announcement_with_delivery_tracking(self):
        response = self.client.post(
            reverse("accounts:system_announcement_create"),
            {
                "title": "Driver policy update",
                "message": "Arrive with uniforms and confirm every QR scan before leaving.",
                "target_role": UserRole.DRIVER,
            },
        )

        self.assertRedirects(response, reverse("accounts:system_announcements"))
        announcement = Announcement.objects.get(title="Driver policy update")
        self.assertEqual(announcement.recipient_count, 1)
        self.assertEqual(announcement.sent_count, 1)
        self.assertEqual(Notification.objects.filter(recipient=self.driver_user, title="Driver policy update").count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_verifying_company_activates_primary_company_admin_and_sends_activation_email(self):
        self.company_admin.is_active = False
        self.company_admin.email_verified_at = None
        self.company_admin.save(update_fields=["is_active", "email_verified_at", "updated_at"])

        response = self.client.post(
            reverse("accounts:verify_company", kwargs={"pk": self.company.pk}),
            {"efda_reference": "EFDA-READY-001"},
        )

        self.assertRedirects(response, reverse("accounts:system_dashboard"))
        self.company.refresh_from_db()
        self.company_admin.refresh_from_db()
        self.assertTrue(self.company.is_verified)
        self.assertEqual(self.company.verification_status, CompanyVerificationStatus.VERIFIED)
        self.assertEqual(self.company_admin.managed_company, self.company)
        self.assertTrue(self.company_admin.is_active)
        self.assertIsNotNone(self.company_admin.email_verified_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.company_admin.email, mail.outbox[0].to)
        self.assertTrue(AuditLog.objects.filter(action="company.verified", entity_label=self.company.name).exists())

    def test_system_admin_can_suspend_and_reactivate_company(self):
        self.company.verification_status = "verified"
        self.company.is_verified = True
        self.company.save(update_fields=["verification_status", "is_verified", "updated_at"])

        home_before = self.client.get(reverse("home"))
        self.assertContains(home_before, self.company.name)

        suspend_response = self.client.post(reverse("accounts:suspend_company", kwargs={"pk": self.company.pk}))
        self.assertRedirects(suspend_response, reverse("accounts:system_dashboard"))
        self.company.refresh_from_db()
        self.assertFalse(self.company.is_active)
        self.assertTrue(AuditLog.objects.filter(action="company.suspended", entity_label=self.company.name).exists())

        home_after_suspend = self.client.get(reverse("home"))
        self.assertNotContains(home_after_suspend, self.company.name)

        reactivate_response = self.client.post(reverse("accounts:reactivate_company", kwargs={"pk": self.company.pk}))
        self.assertRedirects(reactivate_response, reverse("accounts:system_dashboard"))
        self.company.refresh_from_db()
        self.assertTrue(self.company.is_active)
        self.assertTrue(AuditLog.objects.filter(action="company.reactivated", entity_label=self.company.name).exists())

        home_after_reactivate = self.client.get(reverse("home"))
        self.assertContains(home_after_reactivate, self.company.name)

    def test_system_admin_can_export_audit_and_platform_reports(self):
        AuditLog.objects.create(
            actor=self.system_admin,
            action="manual.check",
            entity_type="system",
            entity_id="1",
            entity_label="Health probe",
            old_values={},
            new_values={"status": "ok"},
            ip_address="127.0.0.1",
        )
        date_value = timezone.localdate().isoformat()

        audit_response = self.client.get(reverse("accounts:system_audit_export"))
        excel_response = self.client.get(
            reverse("accounts:system_reports_export", kwargs={"export_format": "excel"}),
            {"date_from": date_value, "date_to": date_value},
        )
        pdf_response = self.client.get(
            reverse("accounts:system_reports_export", kwargs={"export_format": "pdf"}),
            {"date_from": date_value, "date_to": date_value},
        )

        self.assertEqual(audit_response.status_code, 200)
        self.assertIn("text/csv", audit_response["Content-Type"])
        self.assertIn(b"manual.check", audit_response.content)
        self.assertEqual(excel_response.status_code, 200)
        self.assertIn("application/vnd.ms-excel", excel_response["Content-Type"])
        self.assertIn(self.company.name.encode("utf-8"), excel_response.content)
        self.assertEqual(pdf_response.status_code, 200)
        self.assertTrue(pdf_response.content.startswith(b"%PDF"))
