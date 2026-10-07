from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.core.models import SiteSettings
from apps.signals.models import DiscoveryRun, StrategyProfile, TuningRun
from apps.signals.services import schedule_discovery, schedule_self_tuning


class ScheduleTests(TestCase):
    def setUp(self):
        self.profile = StrategyProfile.objects.create(name="T")

    def started(self, fn, *args):
        with mock.patch("apps.core.background.start") as start:
            fn(*args)
        return [c.args[0] for c in start.call_args_list]

    def test_tuning_due_weekly_and_retried_when_cut_off(self):
        self.assertEqual(self.started(schedule_self_tuning), [f"tune:{self.profile.pk}"])  # never tuned
        now = timezone.now()
        StrategyProfile.objects.filter(pk=self.profile.pk).update(last_tuned_at=now - timedelta(hours=1))
        self.assertEqual(self.started(schedule_self_tuning), [])  # probably still running elsewhere
        StrategyProfile.objects.filter(pk=self.profile.pk).update(last_tuned_at=now - timedelta(hours=5))
        self.assertEqual(self.started(schedule_self_tuning), [f"tune:{self.profile.pk}"])  # cut off: retry
        TuningRun.objects.create(profile=self.profile)
        self.assertEqual(self.started(schedule_self_tuning), [])  # finished: wait a week

    def test_discovery_due_monthly_and_retried_when_cut_off(self):
        site = SiteSettings.load()
        self.assertEqual(self.started(schedule_discovery, site), ["discover"])
        site.last_discovered_at = timezone.now() - timedelta(days=2)
        run = DiscoveryRun.objects.create(finished=True)
        self.assertEqual(self.started(schedule_discovery, site), [])
        run.finished = False
        run.save()
        DiscoveryRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(hours=4))
        self.assertEqual(self.started(schedule_discovery, site), ["discover"])
        site.auto_discover = False
        self.assertEqual(self.started(schedule_discovery, site), [])
