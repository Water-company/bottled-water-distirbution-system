import os
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_local_env():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env()

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-dev-key")
DEBUG = os.getenv("DJANGO_DEBUG", "True").lower() == "true"
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core',
    'accounts',
    'catalog',
    'cart',
    'orders',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'core.middleware.SessionInactivityMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'cart.context_processors.cart_summary',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 8},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Addis_Ababa'

USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTH_USER_MODEL = 'accounts.User'
LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = 'accounts:dashboard'
LOGOUT_REDIRECT_URL = 'accounts:login'

EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "false").lower() in {"1", "true", "yes", "on"}
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "20"))
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend" if EMAIL_HOST else "django.core.mail.backends.console.EmailBackend",
)
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@water.local')

DEFAULT_DELIVERY_FEE = Decimal('5.00')
REGISTRATION_OTP_EXPIRY_MINUTES = int(os.getenv("REGISTRATION_OTP_EXPIRY_MINUTES", "10"))
REGISTRATION_OTP_RESEND_SECONDS = int(os.getenv("REGISTRATION_OTP_RESEND_SECONDS", "60"))
ORDER_CANCELLATION_WINDOW_MINUTES = int(os.getenv("ORDER_CANCELLATION_WINDOW_MINUTES", "30"))
ORDER_CANCELLATION_FEE_PERCENT = Decimal(os.getenv("ORDER_CANCELLATION_FEE_PERCENT", "10.00"))
ORDER_REFUND_REQUEST_WINDOW_DAYS = int(os.getenv("ORDER_REFUND_REQUEST_WINDOW_DAYS", "7"))
AGENT_REQUEST_RESPONSE_MINUTES = int(os.getenv("AGENT_REQUEST_RESPONSE_MINUTES", "2"))
CHAPA_PUBLIC_KEY = os.getenv("CHAPA_PUBLIC_KEY", "")
CHAPA_SECRET_KEY = os.getenv("CHAPA_SECRET_KEY", "")
CHAPA_BASE_URL = os.getenv("CHAPA_BASE_URL", "https://api.chapa.co/v1")
NOMINATIM_BASE_URL = os.getenv("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org")
NOMINATIM_CONTACT_EMAIL = os.getenv("NOMINATIM_CONTACT_EMAIL", "")
NOMINATIM_USER_AGENT = os.getenv("NOMINATIM_USER_AGENT", "AquaFlow Platform/1.0")
NOMINATIM_VIEWBOX = os.getenv("NOMINATIM_VIEWBOX", "38.6485,9.0840,38.8472,8.8780")
QR_TOKEN_SECRET = os.getenv("QR_TOKEN_SECRET", SECRET_KEY)
QR_TOKEN_EXPIRY_HOURS = int(os.getenv("QR_TOKEN_EXPIRY_HOURS", "24"))
DRIVER_DELIVERY_EARNING_AMOUNT = Decimal(os.getenv("DRIVER_DELIVERY_EARNING_AMOUNT", "50.00"))
DRIVER_ON_TIME_TARGET_MINUTES = int(os.getenv("DRIVER_ON_TIME_TARGET_MINUTES", "60"))
ETA_AVERAGE_SPEED_KMH = Decimal(os.getenv("ETA_AVERAGE_SPEED_KMH", "25"))
ACCOUNT_LOCKOUT_THRESHOLD = int(os.getenv("ACCOUNT_LOCKOUT_THRESHOLD", "5"))
ACCOUNT_LOCKOUT_MINUTES = int(os.getenv("ACCOUNT_LOCKOUT_MINUTES", "15"))
SESSION_INACTIVITY_MINUTES = int(os.getenv("SESSION_INACTIVITY_MINUTES", "30"))
