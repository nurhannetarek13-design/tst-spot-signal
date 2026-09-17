import unittest
from v2_bot.frozen_trend_lab import BAR_MS, RULES, indicators, signal, simulate


class FrozenTrendTests(unittest.TestCase):
    def make_bars(self, count=260):
        return [{'t':1_700_000_000_000+i*BAR_MS,'o':100+i*.1,
                 'h':101+i*.1,'l':99+i*.1,'c':100+i*.1}
                for i in range(count)]

    def test_donchian_excludes_current_high(self):
        bars=self.make_bars();ind=indicators(bars)
        before=signal('DONCHIAN55_SMA200',bars,ind,230)
        bars[230]['h']=10**9
        self.assertEqual(before,signal('DONCHIAN55_SMA200',bars,ind,230))

    def test_no_future_leak_into_prior_signal(self):
        bars=self.make_bars();ind=indicators(bars)
        before=[signal(rule,bars,ind,225) for rule in RULES]
        bars[226]['o']=5000;bars[226]['c']=5000
        self.assertEqual(before,[signal(rule,bars,ind,225) for rule in RULES])

    def test_costs_and_terminal_marker(self):
        bars=self.make_bars();ind=indicators(bars)
        start=bars[220]['t'];end=bars[-1]['t']+BAR_MS
        baseline=simulate(bars,ind,'SMA200_STATE',start,end,5)
        stress=simulate(bars,ind,'SMA200_STATE',start,end,15)
        self.assertEqual(baseline['closed'],1)
        self.assertEqual(baseline['boundary_marks'],1)
        self.assertGreater(baseline['net_usdt'],stress['net_usdt'])
        self.assertEqual(stress['closed'],1)

    def test_gap_stop_executes_at_worse_open(self):
        bars=self.make_bars()
        bars[231]['o']=90;bars[231]['h']=91;bars[231]['l']=89;bars[231]['c']=90
        ind=indicators(bars)
        outcome=simulate(bars,ind,'SMA200_STATE',bars[220]['t'],bars[-1]['t']+BAR_MS,5)
        self.assertTrue(any(t['reason']=='stop_gap_open' for t in outcome['trades']))
        self.assertGreater(outcome['stop_gaps'],0)


if __name__=='__main__':
    unittest.main()
