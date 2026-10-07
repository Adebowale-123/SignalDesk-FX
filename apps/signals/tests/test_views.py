from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.market.models import Instrument
from apps.signals.models import StrategyProfile


class PortalTests(TestCase):
    def setUp(self):
        self.instrument = Instrument.objects.create(symbol="EUR/USD", pip_size=0.0001, yahoo_symbol="EURUSD=X")
        self.profile = StrategyProfile.objects.create(name="Intraday", entry_timeframe="15m",
                                                      confirm_timeframes=["1h", "4h"])
        self.admin = get_user_model().objects.create_superuser("admin", "a@example.com", "pw")
        self.member = get_user_model().objects.create_user("trader", "t@example.com", "pw")

    def test_portal_is_private(self):
        response = self.client.get(reverse("signals:board"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.assertEqual(self.client.get("/healthz/").status_code, 200)

    def test_team_member_sees_board_but_not_settings(self):
        self.client.force_login(self.member)
        self.assertContains(self.client.get(reverse("signals:board")), "EUR/USD")
        for name in ("signals:history", "signals:backtests"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(self.client.get(reverse("signals:settings")).status_code, 302)

    def test_admin_sets_own_timeframes(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("signals:settings")).status_code, 200)
        form = self.client.get(reverse("signals:profile_edit", args=[self.profile.pk])).context["form"]
        data = {k: v for k, v in form.initial.items() if v is not None and k != "instruments"}
        data.update(entry_timeframe="1h", confirm_timeframes=["4h", "1d"], instruments=[self.instrument.pk],
                    sessions=["london"], is_active="on")
        response = self.client.post(reverse("signals:profile_edit", args=[self.profile.pk]), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", {}) and response.context["form"].errors)
        self.profile.refresh_from_db()
        self.assertEqual((self.profile.entry_timeframe, self.profile.confirm_timeframes), ("1h", ["4h", "1d"]))

    def test_confirmation_must_be_larger_than_entry(self):
        self.client.force_login(self.admin)
        form = self.client.get(reverse("signals:profile_edit", args=[self.profile.pk])).context["form"]
        data = {k: v for k, v in form.initial.items() if v is not None and k != "instruments"}
        data.update(entry_timeframe="1h", confirm_timeframes=["15m"], instruments=[self.instrument.pk],
                    sessions=["london"])
        response = self.client.post(reverse("signals:profile_edit", args=[self.profile.pk]), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
