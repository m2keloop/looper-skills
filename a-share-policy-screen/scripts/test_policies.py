"""Boundary tests using synthetic data only; never stock-screen evidence."""
import unittest
from datetime import date, timedelta

from policies import aggregate_iso_weeks, kdj, run_policy, validate


def bars(n, price=10):
    return [{"date": (date(2025, 1, 1)+timedelta(days=i)).isoformat(), "open": price,
             "high": price, "low": price, "close": price, "volume": 100.0} for i in range(n)]


class PolicyTests(unittest.TestCase):
    def test_flat_kdj_zero_range(self):
        self.assertEqual(kdj(bars(21)), {"K": 50.0, "D": 50.0, "J": 50.0})

    def test_stale_is_not_evaluated(self):
        r = run_policy("B1", bars(21), "2026-09-29")
        self.assertEqual(r["status"], "not_evaluated")

    def test_missing_finance_is_not_zero_hits(self):
        b = bars(120)
        r = run_policy("LOW_VOLUME_STABLE_PRICE", b, b[-1]["date"], boards={"concept_tags": ["沪股通"]})
        self.assertEqual(r["status"], "not_evaluated")
        self.assertIn("missing_total_shares", r["reasons"])

    def test_exact_market_cap_threshold(self):
        b = bars(120)
        r = run_policy("LOW_VOLUME_STABLE_PRICE", b, b[-1]["date"],
                       finance={"total_shares": 800_000_000}, boards={"concept_tags": ["沪股通"]})
        self.assertEqual(r["status"], "hit")
        self.assertEqual(r["metrics"]["market_cap_wan"], 800000)

    def test_board_exclusion(self):
        b = bars(120)
        r = run_policy("LOW_VOLUME_STABLE_PRICE", b, b[-1]["date"],
                       finance={"total_shares": 800_000_000}, boards={"concept_tags": ["创业板综"]})
        self.assertEqual(r["status"], "not_hit")

    def test_b1_window_reset_ignores_earlier_prices(self):
        b = bars(60)
        for row in b[:39]:
            row.update(open=100, high=100, low=100, close=100)
        r = run_policy("B1", b, b[-1]["date"])
        self.assertEqual(r["metrics"]["J"], 50)
        self.assertEqual(r["metrics"]["window"], 21)

    def test_iso_year_and_partial_week(self):
        b = bars(3)
        for row, day in zip(b, ["2020-12-31", "2021-01-04", "2021-01-05"]):
            row["date"] = day
        w = aggregate_iso_weeks(b)
        self.assertEqual([r["iso_week"] for r in w], ["2020-W53", "2021-W01"])
        self.assertEqual(w[-1]["volume"], 200)
        self.assertEqual(w[-1]["trading_days"], 2)

    def test_bias_default_does_not_require_strong_zone(self):
        b = bars(120)
        r = run_policy("LOG_BIAS_STRENGTH_FILTER", b, b[-1]["date"])
        self.assertEqual(r["status"], "hit")
        self.assertFalse(r["metrics"]["strong_zone"])
        self.assertEqual(run_policy("LOG_BIAS_STRENGTH_FILTER", b, b[-1]["date"], require_pullback=True)["status"], "not_hit")



    def test_b1_large_drop_inclusive_threshold(self):
        b = bars(21, 1000)
        b[-1].update(open=905, close=905, high=905, low=905)
        r = run_policy("B1", b, b[-1]["date"])
        self.assertFalse(r["checks"]["no_drop_9_5"])

    def test_bad_price_rejected_and_741_required(self):
        b = bars(741)
        with self.assertRaisesRegex(ValueError, "insufficient"):
            validate(b[:-1], 741, b[-2]["date"])
        b[-1]["close"] = 0
        self.assertEqual(run_policy("MA_GOLDEN_CROSS_FIRST_PULLBACK", b, b[-1]["date"])["status"], "not_evaluated")

    def test_b1_v2_confirmation_and_surge_boundaries(self):
        b = bars(114)
        for i, c, v in [(53, 9, 100), (93, 11, 200), (110, 11, 200), (111, 9.5, 100), (112, 11, 400), (113, 8, 100)]:
            b[i].update(open=c, high=c, low=c, close=c, volume=v)
        r = run_policy("B1_V2", b, b[-1]["date"])
        self.assertEqual([p["date"] for p in r["metrics"]["confirmed_lows"]], [b[53]["date"], b[111]["date"]])
        self.assertEqual(r["metrics"]["surge_date"], b[110]["date"])
        self.assertTrue(r["checks"]["contracted_volume"])
        self.assertEqual(r["metrics"]["volume_fraction"], .5)

    def test_annual_can_hit_with_bearish_target_candle(self):
        b = bars(741)
        prices = ([20-i*.03 for i in range(400)] + [8+i*.05 for i in range(280)]
                  + [21.95-i/60*2 for i in range(61)])
        for row, c in zip(b, prices):
            row.update(open=c, close=c, high=c*1.01, low=c*.99)
        b[-1].update(open=20, high=20.1, low=18.5, volume=200)
        r = run_policy("MA_GOLDEN_CROSS_FIRST_PULLBACK", b, b[-1]["date"])
        self.assertLess(b[-1]["close"], b[-1]["open"])
        self.assertEqual(r["status"], "hit")
        self.assertEqual(r["metrics"]["support"][0]["ma"], 250)


    def test_b1_v2_exact_minus_2pct_included(self):
        b = bars(114, 100)
        b[-1].update(close=98, low=98)
        r = run_policy("B1_V2", b, b[-1]["date"])
        self.assertTrue(r["checks"]["return_range"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
