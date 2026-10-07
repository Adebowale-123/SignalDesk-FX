from django.core.management.base import BaseCommand, CommandError

from apps.signals.models import StrategyProfile
from apps.signals.services import backtest


class Command(BaseCommand):
    help = "Replay a strategy profile over past prices and store the results (used for confidence)."

    def add_arguments(self, parser):
        parser.add_argument("profile_id", nargs="?", type=int, help="Profile id (default: every active profile)")
        parser.add_argument("--no-download", action="store_true", help="Use stored candles only")

    def handle(self, *args, profile_id=None, no_download=False, **options):
        profiles = StrategyProfile.objects.filter(pk=profile_id) if profile_id else \
            StrategyProfile.objects.filter(is_active=True)
        if not profiles:
            raise CommandError("No matching profile.")
        for profile in profiles:
            run = backtest(profile, download=not no_download)
            self.stdout.write(self.style.SUCCESS(
                f"{profile}: {run.trades} trades, win rate {run.win_rate}%, avg {run.avg_r}R, "
                f"profit factor {run.profit_factor}, total {run.total_r}R"))
            for symbol, stats in run.per_pair.items():
                self.stdout.write(f"   {symbol}: {stats}")
