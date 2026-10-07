import time

from django.core.management.base import BaseCommand

from apps.core.models import SiteSettings
from apps.signals.services import run_cycle


class Command(BaseCommand):
    help = "Run the analysis now (--once) or keep running it every few minutes."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, once=False, **options):
        while True:
            started = time.monotonic()
            summary = run_cycle()
            self.stdout.write(f"{time.strftime('%H:%M:%S')} {summary}")
            if once:
                return
            interval = SiteSettings.load().engine_interval_minutes * 60
            time.sleep(max(30, interval - (time.monotonic() - started)))
