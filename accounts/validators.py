import re

from django.core.exceptions import ValidationError

try:
    import magic
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    magic = None


ETHIOPIAN_PHONE_PATTERN = re.compile(r"^(?:\+251|251|0)?9\d{8}$")
USERNAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,148}[a-z0-9])?$")
ETHIOPIAN_BUSINESS_LICENSE_PATTERN = re.compile(r"^(?=.{8,30}$)(?=.*\d)[A-Z0-9/-]+$")
EFDA_REGISTRATION_PATTERN = re.compile(r"^(?=.{6,30}$)(?=.*\d)[A-Z0-9/-]+$")
TIN_PATTERN = re.compile(r"^\d{10}$")


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


def normalize_username(value, *, required=True):
    raw_value = (value or "").strip().replace(" ", "_").lower()
    if not raw_value:
        if required:
            raise ValidationError("Enter a username.")
        return ""

    if not USERNAME_PATTERN.match(raw_value):
        raise ValidationError(
            "Username must be 3-150 characters and may contain only lowercase letters, numbers, dots, hyphens, and underscores."
        )
    return raw_value


def validate_username(value):
    normalize_username(value)


def normalize_business_license_number(value, *, required=True):
    raw_value = (value or "").strip().upper().replace(" ", "")
    if not raw_value:
        if required:
            raise ValidationError("Enter a business license number.")
        return ""

    if not ETHIOPIAN_BUSINESS_LICENSE_PATTERN.match(raw_value):
        raise ValidationError(
            "Enter a valid Ethiopian business license number using letters, numbers, slashes, or hyphens."
        )
    return raw_value


def validate_ethiopian_business_license_number(value):
    normalize_business_license_number(value)


def normalize_efda_registration_number(value, *, required=True):
    raw_value = (value or "").strip().upper().replace(" ", "")
    if not raw_value:
        if required:
            raise ValidationError("Enter an EFDA registration number.")
        return ""

    if not EFDA_REGISTRATION_PATTERN.match(raw_value):
        raise ValidationError(
            "Enter a valid EFDA registration number using letters, numbers, slashes, or hyphens."
        )
    return raw_value


def validate_efda_registration_number(value):
    normalize_efda_registration_number(value)


def normalize_tin_number(value, *, required=True):
    raw_value = "".join(ch for ch in (value or "").strip() if ch.isdigit())
    if not raw_value:
        if required:
            raise ValidationError("Enter a TIN.")
        return ""

    if not TIN_PATTERN.match(raw_value):
        raise ValidationError("Enter a valid 10-digit Ethiopian TIN.")
    return raw_value


def validate_tin_number(value):
    normalize_tin_number(value)


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

    def detect_content_type(upload):
        if hasattr(upload, "seek"):
            upload.seek(0)
        sample = upload.read(4096)
        if hasattr(upload, "seek"):
            upload.seek(0)

        if magic is not None:
            try:
                detected = (magic.from_buffer(sample, mime=True) or "").lower()
                if detected:
                    return detected
            except Exception:
                pass

        if sample.startswith(b"%PDF-"):
            return "application/pdf"
        if sample.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if sample.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        return ""

    def validator(upload):
        if not upload:
            return
        content_type = detect_content_type(upload)
        if not content_type:
            raise ValidationError("We could not inspect the uploaded file type.")
        if content_type not in allowed_types:
            raise ValidationError(
                f"Unsupported file type. Allowed types: {allowed_display}."
            )

    return validator
