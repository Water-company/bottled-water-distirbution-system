from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from catalog.models import Company, Product


class CatalogBrowsingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company_one = Company.objects.create(
            name="Blue Spring",
            description="Verified water company",
            location="Addis Ababa",
            is_verified=True,
        )
        cls.company_two = Company.objects.create(
            name="Highland Water",
            description="Regional water supplier",
            location="Adama",
            is_verified=False,
        )
        Product.objects.create(
            company=cls.company_one,
            name="Family Pack",
            description="Large bottled water pack",
            price="10.00",
            available_quantity=20,
        )
        Product.objects.create(
            company=cls.company_two,
            name="Office Pack",
            description="Water delivery for office teams",
            price="7.50",
            available_quantity=15,
        )

    def test_company_list_filters_by_search_and_location(self):
        response = self.client.get(reverse("companies:list"), {"search": "Blue", "location": "Addis"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Blue Spring")
        self.assertNotContains(response, "Highland Water")

    def test_unverified_companies_are_hidden_from_public_browsing(self):
        response = self.client.get(reverse("companies:list"))
        self.assertContains(response, "Blue Spring")
        self.assertNotContains(response, "Highland Water")

    def test_product_list_only_shows_products_from_verified_companies(self):
        response = self.client.get(reverse("products:list"), {"sort": "price_asc"})
        self.assertEqual(response.status_code, 200)
        products = list(response.context["products"])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].name, "Family Pack")

    def test_product_detail_requires_authentication(self):
        product = Product.objects.get(name="Family Pack")
        response = self.client.get(reverse("products:detail", kwargs={"slug": product.slug}))
        self.assertRedirects(response, f"{reverse('accounts:login')}?next={reverse('products:detail', kwargs={'slug': product.slug})}")

    def test_authenticated_user_can_view_product_detail(self):
        user = User.objects.create_user(
            email="catalog@example.com",
            password="StrongPass123!",
            first_name="Catalog",
            last_name="User",
            phone_number="+251911000030",
        )
        self.client.force_login(user)
        product = Product.objects.get(name="Family Pack")
        response = self.client.get(reverse("products:detail", kwargs={"slug": product.slug}))
        self.assertEqual(response.status_code, 200)

# Create your tests here.
