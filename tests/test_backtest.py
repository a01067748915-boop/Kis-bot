from backtest import backtest


def test_backtest_counts_trades_and_stops():
    bars = [{"date": f"d{i}", "open": 100, "high": 100, "low": 98, "close": 100} for i in range(10)]
    bars.append({"date": "win", "open": 100, "high": 106, "low": 99.5, "close": 105})
    bars.append({"date": "stop", "open": 100, "high": 110, "low": 95, "close": 101})
    r = backtest(bars, k=0.5, ma=0)
    assert r["매매횟수"] == 2 and r["손절횟수"] == 1


def test_backtest_short_data_does_not_crash():
    assert backtest([{"date": "a", "open": 1, "high": 1, "low": 1, "close": 1}], ma=5)["거래일"] == 0
