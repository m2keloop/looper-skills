"""Synthetic behavioral checks of signal timing, invalidation and shape ranking."""
import copy
import unittest

from policies import b1_volume_structure, run_policy
from test_policies import bars


def shape_rows(n=21, age=3, fractions=(.5, .4, .3)):
    rows = bars(n)
    i = n-1-age
    rows[i].update(open=10, high=11, low=9.5, close=10.2, volume=300)
    for j in range(1, age+1):
        c = 9.8 if j==1 else 9.6
        fraction = fractions[j-1] if j<=len(fractions) else fractions[-1]
        rows[i+j].update(open=c+.05, high=c+.1, low=c-.05, close=c, volume=300*fraction)
    return rows


class B1StructureTests(unittest.TestCase):
    def selected(self, rows):
        return b1_volume_structure(rows)['selected_signal']

    def test_one_complete_event_can_hit_daily_b1(self):
        rows=shape_rows()
        result=run_policy('B1', rows, rows[-1]['date'],
                          finance={'total_shares':1_000_000_000}, boards={'listing_board':'MAIN'})
        self.assertEqual(result['status'], 'hit')
        self.assertEqual(result['metrics']['recent_surge_count'], 1)
        self.assertIsNotNone(result['metrics']['shape_score'])

    def test_two_days_complete_but_one_is_too_early(self):
        self.assertEqual(self.selected(shape_rows(age=2))['contraction_days'], 2)
        self.assertIsNone(self.selected(shape_rows(age=1)))

    def test_watch_continues_beyond_day_three_and_stops_after_ten(self):
        for age in (3, 5, 10):
            self.assertEqual(self.selected(shape_rows(age=age))['age_bars'], age)
        self.assertIsNone(self.selected(shape_rows(age=11)))

    def test_shrinkage_must_start_immediately(self):
        rows=shape_rows(age=5,fractions=(1.8, .7, .3, .2, .2))
        self.assertIsNone(self.selected(rows))
        self.assertIn('immediate_mean_contraction_not_completed', b1_volume_structure(rows)['recent_surges'][0]['reasons'])

    def test_third_day_can_complete_mean_contraction(self):
        rows=shape_rows(fractions=(1.2,.7,.3))
        self.assertEqual(self.selected(rows)['contraction_days'],3)
        self.assertIsNone(self.selected(shape_rows(age=2,fractions=(1.2,.7))))

    def test_daily_sixty_percent_is_score_quality_not_a_hit_gate(self):
        basic=self.selected(shape_rows(fractions=(.85,.6,.6)))
        deep=self.selected(shape_rows(fractions=(.6,.6,.6)))
        self.assertIsNotNone(basic)
        self.assertFalse(basic['deep_contraction'])
        self.assertTrue(deep['deep_contraction'])
        self.assertGreater(deep['shape_score'],basic['shape_score'])

    def test_two_days_still_qualify_when_third_reexpands(self):
        self.assertEqual(self.selected(shape_rows(fractions=(.5,.4,.8)))['contraction_days'], 2)

    def test_shrink_threshold_includes_boundary(self):
        self.assertEqual(self.selected(shape_rows(fractions=(.8,.8,.8)))['contraction_days'],3)
        self.assertIsNone(self.selected(shape_rows(fractions=(.80001,.80001,.80001))))

    def test_close_support_inclusive_and_intraday_dip_allowed(self):
        rows=shape_rows()
        floor=9.5*.97
        rows[-1].update(close=floor, low=9.1, open=9.3, high=9.4)
        self.assertIsNotNone(self.selected(rows))
        rows[-1]['close']=floor-.001
        self.assertIsNone(self.selected(rows))

    def test_small_support_dip_is_retained_but_scores_lower(self):
        rows=shape_rows();strong=self.selected(rows)
        rows[-1].update(close=9.3,low=9.2,open=9.5,high=9.6)
        event=self.selected(rows)
        self.assertIsNotNone(event)
        self.assertFalse(event['strict_support_held'])
        self.assertLess(event['shape_score'],strong['shape_score'])

    def test_broken_old_signal_does_not_revive_on_price_recovery(self):
        rows=shape_rows(age=5)
        rows[-3].update(close=9.1, low=9)
        rows[-1].update(close=10, high=10.1, open=9.7)
        self.assertIsNone(self.selected(rows))
        self.assertIn('post_signal_close_broke_tolerated_support', b1_volume_structure(rows)['recent_surges'][0]['reasons'])

    def test_deeper_contraction_and_three_days_score_higher(self):
        weak=self.selected(shape_rows(fractions=(.6,.6,.6)))
        strong=self.selected(shape_rows(fractions=(.3,.25,.2)))
        self.assertGreater(strong['shape_score'], weak['shape_score'])
        two=self.selected(shape_rows(age=2, fractions=(.4,.4,.4)))
        three=self.selected(shape_rows(age=3, fractions=(.4,.4,.4)))
        self.assertGreater(three['shape_score'], two['shape_score'])

    def test_fresher_same_shape_scores_higher(self):
        self.assertGreater(self.selected(shape_rows(age=3))['shape_score'],
                           self.selected(shape_rows(age=8))['shape_score'])

    def test_price_preservation_scores_higher_and_score_is_bounded(self):
        weak=shape_rows();strong=copy.deepcopy(weak)
        for row in strong[-3:]:row.update(close=10.1, open=10.1, low=10, high=10.2)
        self.assertGreater(self.selected(strong)['shape_score'], self.selected(weak)['shape_score'])
        for rows in (weak,strong,shape_rows(fractions=(.02,.02,.02)),shape_rows(age=10)):
            event=self.selected(rows)
            self.assertGreaterEqual(event['shape_score'],0)
            self.assertLessEqual(event['shape_score'],100)
            self.assertAlmostEqual(sum(event['score_parts'].values()),event['shape_score'],places=2)

    def test_previous_day_low_volume_alone_does_not_get_high_volume_score(self):
        rows=shape_rows();ordinary=self.selected(rows)
        rows[-5]['volume']=20
        rows[-4]['volume']=100
        for row in rows[-3:]:row['volume']/=3
        event=self.selected(rows)
        self.assertGreater(event['volume_multiple'],ordinary['volume_multiple'])
        self.assertLess(event['score_parts']['volume_strength'],20)

    def test_shape_score_does_not_override_kdj_or_finance_gate(self):
        rows=shape_rows()
        for row in rows[-3:]:row.update(close=10.9,open=10.8,low=10.7,high=11)
        result=run_policy('B1',rows,rows[-1]['date'],finance={'total_shares':1_000_000_000},boards={'listing_board':'MAIN'})
        self.assertIsNotNone(result['metrics']['shape_score'])
        self.assertFalse(result['checks']['j_below_15'])
        self.assertEqual(result['status'],'not_hit')
        missing=run_policy('B1',shape_rows(),shape_rows()[-1]['date'],boards={'listing_board':'MAIN'})
        self.assertEqual(missing['status'],'not_evaluated')

    def test_weekly_keeps_original_two_surges_without_score(self):
        rows=shape_rows(n=160)
        result=run_policy('B1_WEEKLY',rows,rows[-1]['date'],finance={'total_shares':1_000_000_000},boards={'listing_board':'MAIN'})
        self.assertIn('two_surges',result['checks'])
        self.assertNotIn('shape_score',result['metrics'])


if __name__=='__main__':unittest.main()
