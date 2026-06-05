from core.models import Notification


def notify_user(recipient, title, message, link=""):
    return Notification.objects.create(
        recipient=recipient,
        title=title,
        message=message,
        link=link,
    )
