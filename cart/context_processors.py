from cart.services import get_or_create_cart


def cart_summary(request):
    if not request.user.is_authenticated:
        return {"cart_item_count": 0}

    cart = get_or_create_cart(request.user)
    return {"cart_item_count": cart.items_count}
