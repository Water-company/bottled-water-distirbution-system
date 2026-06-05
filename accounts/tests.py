from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.contrib.messages import get_messages

from accounts.models import RegistrationOTP, User, UserRole


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
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

    def test_otp_verification_activates_user_and_sends_success_email(self):
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
        self.assertContains(login_response, "Invalid email or password.")

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

# Create your tests here.
