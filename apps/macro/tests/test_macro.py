import io
import zipfile
from datetime import date, timedelta

import numpy as np
import pandas as pd
from django.test import TestCase

from apps.macro.services import MacroData, cot_positioning, daily_drivers, parse_cot
from apps.market.models import Instrument

HEADER = ["Market and Exchange Names", "As of Date in Form YYMMDD", "As of Date in Form YYYY-MM-DD",
          "CFTC Contract Market Code", "x", "y", "z", "Open Interest (All)", "Noncommercial Positions-Long (All)",
          "Noncommercial Positions-Short (All)"]


def cot_zip(rows):
    text = io.StringIO()
    text.write(",".join(f'"{h}"' for h in HEADER) + "\n")
    for r in rows:
        text.write(",".join(str(v) for v in r) + "\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("annual.txt", text.getvalue())
    return buf.getvalue()


def trending(days, start=100.0, step=0.5):
    first = date(2024, 1, 1)
    return {first + timedelta(days=i): start + step * i for i in range(days)}


class CotTests(TestCase):
    def test_parse_keeps_only_tracked_markets(self):
        rows = parse_cot(cot_zip([
            ["EURO FX", "260929", "2026-09-29", "099741", 0, 0, 0, 700000, 100000, 160000],
            ["WHEAT", "260929", "2026-09-29", "001602", 0, 0, 0, 1, 1, 1],
            ["GOLD", "260929", "2026-09-29", " 088691", 0, 0, 0, 500000, 250000, 30000],
        ]))
        self.assertEqual([(r["currency"], r["long"] - r["short"]) for r in rows], [("EUR", -60000), ("XAU", 220000)])

    def test_positioning_is_used_only_after_publication(self):
        rows = [{"currency": "EUR", "report_date": date(2026, 9, 29), "long": 10, "short": 5}]
        frame = cot_positioning(rows)["EUR"]
        self.assertEqual(frame.index[0], pd.Timestamp("2026-10-03", tz="UTC"))  # Tuesday data, usable Saturday


class DriverTests(TestCase):
    def bars(self, days=120, dxy_step=0.5):
        return {"SPX": trending(days), "VIX": {d: 14.0 for d in trending(days)}, "DXY": trending(days, step=dxy_step),
                "US10Y": trending(days, 4.0, 0.01), "OIL": {d: 80.0 for d in trending(days)}}

    def test_day_close_is_usable_next_day(self):
        drivers = daily_drivers(self.bars())
        self.assertEqual(drivers.index[0], pd.Timestamp("2024-01-02", tz="UTC"))

    def test_rising_dollar_and_yields_favour_usd_and_hurt_gold(self):
        data = MacroData(daily_drivers(self.bars()), {})
        last = data.drivers.iloc[-1]
        self.assertEqual((last["mood"], last["dxy"], last["yld"], last["oil"]), (1.0, 1.0, 1.0, 0.0))
        eur = data.frame_for(Instrument(symbol="EUR/USD")).iloc[-1]
        gold = data.frame_for(Instrument(symbol="XAU/USD")).iloc[-1]
        usdjpy = data.frame_for(Instrument(symbol="USD/JPY")).iloc[-1]
        self.assertLess(eur["m_fund"], 0)
        self.assertLess(gold["m_fund"], eur["m_fund"])
        self.assertGreater(usdjpy["m_fund"], 0)
        self.assertGreater(usdjpy["m_sent"], 0)  # risk-on lifts USD/JPY

    def test_attach_uses_only_published_values(self):
        data = MacroData(daily_drivers(self.bars()), {})
        times = pd.date_range("2024-03-01", periods=96 * 3, freq="15min", tz="UTC")
        features = pd.DataFrame({"close": 1.0}, index=times)
        features["close_time"] = features.index + pd.Timedelta(minutes=15)
        out = data.attach(features, Instrument(symbol="EUR/USD"))
        frame = data.frame_for(Instrument(symbol="EUR/USD"))
        for t, row in out.iloc[::17].iterrows():
            known = frame[frame.index <= row["close_time"]]
            self.assertEqual(row["m_fund"], known["m_fund"].iloc[-1])

    def test_stale_data_counts_as_missing(self):
        data = MacroData(daily_drivers(self.bars()), {})
        times = pd.date_range("2025-06-01", periods=10, freq="15min", tz="UTC")  # long after the last bar
        features = pd.DataFrame({"close": 1.0}, index=times)
        features["close_time"] = features.index + pd.Timedelta(minutes=15)
        out = data.attach(features, Instrument(symbol="EUR/USD"))
        self.assertTrue(np.isnan(out["m_fund"]).all())

    def test_no_data_means_neutral_columns(self):
        times = pd.date_range("2025-06-01", periods=5, freq="15min", tz="UTC")
        features = pd.DataFrame({"close": 1.0}, index=times)
        features["close_time"] = features.index
        out = MacroData(pd.DataFrame(), {}).attach(features, Instrument(symbol="EUR/USD"))
        self.assertTrue(np.isnan(out["m_sent"]).all())
