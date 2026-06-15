import re

from django.core.exceptions import ValidationError


ETHIOPIAN_PHONE_PATTERN = re.compile(r"^(?:\+251|251|0)?9\d{8}$")


def normalize_ethiopian_phone_number(value, *, required=True):
    raw_value = (value or "").strip()
    if not raw_value:
        if required:
            raise ValidationError("Enter a phone number.")
        return ""

    sanitized = raw_value.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not ETHIOPIAN_PHONE_PATTERN.match(sanitized):
        raise ValidationError("Enter a valid Ethiopian phone number in the format +2519XXXXXXXX.")

    if sanitized.startswith("+251"):
        return sanitized
    if sanitized.startswith("251"):
        return f"+{sanitized}"
    if sanitized.startswith("09"):
        return f"+251{sanitized[1:]}"
    if sanitized.startswith("9"):
        return f"+251{sanitized}"

    raise ValidationError("Enter a valid Ethiopian phone number in the format +2519XXXXXXXX.")


def validate_ethiopian_phone_number(value):
    normalize_ethiopian_phone_number(value)
