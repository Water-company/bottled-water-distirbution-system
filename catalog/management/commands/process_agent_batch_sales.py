from django.core.management.base import BaseCommand

from catalog.services import auto_confirm_stale_agent_batch_sales, notify_overdue_agent_batch_sales


class Command(BaseCommand):
    help = "Auto-confirm stale approved batch sales and notify company admins about overdue received sales."

    def handle(self, *args, **options):
        auto_summary = auto_confirm_stale_agent_batch_sales()
        overdue_summary = notify_overdue_agent_batch_sales()
        self.stdout.write(
            self.style.SUCCESS(
                "Processed agent batch sales: "
                f"{auto_summary['confirmed']} auto-confirmed, "
                f"{auto_summary['failed']} failed, "
                f"{overdue_summary['notified']} overdue notifications sent."
            )
        )
