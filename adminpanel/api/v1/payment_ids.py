import hashlib


def numeric_payment_id(seed_value):
    """Return the stable eight-digit Payment ID shown throughout the admin UI."""
    seed = str(seed_value).encode("utf-8")
    value = int(hashlib.sha256(seed).hexdigest()[:12], 16) % 90000000
    return str(10000000 + value)


def payment_id_for_order(order):
    return numeric_payment_id(f"order:{order.merchant_order_id}:{order.id}")


def payment_id_for_protected_order(protected_order):
    if protected_order.order:
        return payment_id_for_order(protected_order.order)
    return numeric_payment_id(
        f"protected:{protected_order.vaultpay_order_id}:{protected_order.id}"
    )
