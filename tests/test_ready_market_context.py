import unittest

from ready_bot.market_context import (
    classify_market_regime,
    max_open_position_correlation,
    relative_strength_ranking,
    validate_bar_integrity,
)


def bars(n=260, step=3_600_000, drift=.001, start=1_700_000_000_000):
    out=[]
    p=100.0
    for i in range(n):
        o=p
        p=p*(1+drift)
        out.append({"t":start+i*step,"o":o,"h":max(o,p)*1.001,"l":min(o,p)*.999,"c":p})
    return out


class ReadyMarketContextTests(unittest.TestCase):
    def test_data_integrity_accepts_fresh_monotonic_bars(self):
        b=bars(n=40,step=60_000)
        now=b[-1]["t"]+80_000
        x=validate_bar_integrity(b,"1m",now)
        self.assertTrue(x["ok"])

    def test_data_integrity_rejects_large_gap_and_stale_data(self):
        b=bars(n=40,step=60_000)
        b[-1]["t"]+=180_000
        x=validate_bar_integrity(b,"1m",b[-1]["t"]+60_000)
        self.assertFalse(x["ok"])
        self.assertIn("BAR_GAP",x["reasons"])
        stale=validate_bar_integrity(b[:-1],"1m",b[-1]["t"]+600_000)
        self.assertIn("STALE_BARS",stale["reasons"])

    def test_relative_strength_ranks_cross_section_not_just_green(self):
        snaps={
            "AUSDT":{"ret_5m":.03,"ret_15m":.05,"ret_1h":.08},
            "BUSDT":{"ret_5m":.01,"ret_15m":.02,"ret_1h":.03},
            "CUSDT":{"ret_5m":-.01,"ret_15m":0.0,"ret_1h":.01},
        }
        r=relative_strength_ranking(snaps,{"5m":.005,"15m":.01,"1h":.02})
        self.assertGreater(r["AUSDT"]["score"],r["BUSDT"]["score"])
        self.assertTrue(r["AUSDT"]["strong"])
        self.assertTrue(r["CUSDT"]["weak"])

    def test_regime_risk_off_on_btc_dump(self):
        btc1=bars(drift=.001)
        btc4=bars(step=14_400_000,drift=.001)
        btc1[-1]["c"]=btc1[-2]["c"]*.98
        snaps={
            "A":{"ret_1h":-.02,"ret_15m":-.01},
            "B":{"ret_1h":-.01,"ret_15m":-.005},
        }
        x=classify_market_regime(btc1,btc4,snaps)
        self.assertEqual(x["state"],"RISK_OFF")
        self.assertFalse(x["allow_new_longs"])
        self.assertEqual(x["risk_multiplier"],0.0)

    def test_correlation_guard_detects_highly_correlated_positions(self):
        a=bars(n=80,drift=.001)
        b=bars(n=80,drift=.00101)
        snaps={"A":{"_bars1h":a},"B":{"_bars1h":b}}
        x=max_open_position_correlation(snaps["A"],["B"],snaps,48)
        self.assertIsNotNone(x["max_corr"])
        self.assertGreater(x["max_corr"],.95)


if __name__=="__main__":
    unittest.main()
