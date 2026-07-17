from decimal import Decimal, ROUND_HALF_UP

from catalog.models import Company
from orders.models import OrderStatus


MONEY_PLACES = Decimal("0.01")


def quantize_money(value):
    return Decimal(value).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def get_customer_company_streak(customer, company):
    if not customer or not company:
        return 0

    delivered_orders = (
        customer.orders.filter(status=OrderStatus.DELIVERED, delivered_at__isnull=False)
        .select_related("company")
        .order_by("-delivered_at", "-created_at")
    )
    streak = 0
    for order in delivered_orders:
        if order.company_id != company.id:
            break
        # A rewarded order closes the previous streak and starts a fresh cycle.
        if order.premium_discount_percent and order.premium_discount_percent > 0:
            break
        streak += 1
    return streak


def get_company_premium_offer(customer, company):
    streak = get_customer_company_streak(customer, company)
    if not company:
        return {
            "streak": streak,
            "threshold": 0,
            "discount_percent": Decimal("0.00"),
            "eligible": False,
            "feature_enabled": False,
        }

    discount_percent = quantize_money(company.premium_discount_percent or 0)
    threshold = company.premium_streak_threshold or 0
    eligible = bool(
        company.premium_feature_enabled
        and discount_percent > 0
        and threshold > 0
        and streak >= threshold
    )
    return {
        "streak": streak,
        "threshold": threshold,
        "discount_percent": discount_percent if eligible else Decimal("0.00"),
        "eligible": eligible,
        "feature_enabled": company.premium_feature_enabled,
    }


def get_cart_pricing_summary(cart):
    subtotal = quantize_money(cart.subtotal)
    delivery_fee = quantize_money(cart.delivery_fee)
    company = cart.company
    premium_offer = get_company_premium_offer(cart.user, company)
    discount_amount = Decimal("0.00")
    if premium_offer["eligible"]:
        discount_amount = quantize_money(subtotal * premium_offer["discount_percent"] / Decimal("100"))
    total = quantize_money(max(Decimal("0.00"), subtotal - discount_amount + delivery_fee))
    return {
        "company": company,
        "subtotal": subtotal,
        "delivery_fee": delivery_fee,
        "discount_amount": discount_amount,
        "total": total,
        "premium_offer": premium_offer,
    }


def get_customer_loyalty_summary(customer):
    company_ids = (
        customer.orders.filter(status=OrderStatus.DELIVERED)
        .values_list("company_id", flat=True)
        .distinct()
    )
    companies = Company.objects.filter(id__in=company_ids).order_by("name")
    summaries = []
    for company in companies:
        offer = get_company_premium_offer(customer, company)
        summaries.append(
            {
                "company": company,
                "streak": offer["streak"],
                "threshold": offer["threshold"],
                "discount_percent": offer["discount_percent"],
                "eligible": offer["eligible"],
                "feature_enabled": offer["feature_enabled"],
            }
        )
    summaries.sort(key=lambda item: (0 if item["eligible"] else 1, item["company"].name.lower()))
    return summaries
