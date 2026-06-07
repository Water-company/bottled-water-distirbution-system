from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.utils import timezone


class SessionInactivityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            inactivity_limit_seconds = settings.SESSION_INACTIVITY_MINUTES * 60
            now_timestamp = timezone.now().timestamp()
            last_activity = request.session.get("last_activity_at")

            try:
                last_activity = float(last_activity) if last_activity is not None else None
            except (TypeError, ValueError):
                last_activity = None

            if last_activity and now_timestamp - last_activity > inactivity_limit_seconds:
                logout(request)
                messages.info(
                    request,
                    f"Your session expired after {settings.SESSION_INACTIVITY_MINUTES} minutes of inactivity. Please log in again.",
                )
            else:
                request.session["last_activity_at"] = now_timestamp
                if not request.session.get_expire_at_browser_close():
                    request.session.set_expiry(inactivity_limit_seconds)
                request.session.modified = True

        return self.get_response(request)
