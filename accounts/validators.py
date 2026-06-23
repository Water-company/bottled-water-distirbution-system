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


def validate_file_size(max_mb):
    max_bytes = int(max_mb * 1024 * 1024)
    max_mb_display = int(max_mb) if float(max_mb).is_integer() else max_mb

    def validator(upload):
        if not upload:
            return
        size = getattr(upload, "size", None)
        if size is not None and size > max_bytes:
            raise ValidationError(f"File size must not exceed {max_mb_display} MB.")

    return validator


def validate_document_content_type(allowed_types):
    allowed_types = tuple(allowed_types)
    allowed_display = ", ".join(allowed_types)

    def validator(upload):
        if not upload:
            return
        content_type = (getattr(upload, "content_type", "") or "").lower()
        if content_type not in allowed_types:
            raise ValidationError(
                f"Unsupported file type. Allowed types: {allowed_display}."
            )

    return validator
