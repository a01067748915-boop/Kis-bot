import pytest

from backtest import backtest


def test_backtest_counts_trades_and_stops():
    bars = [{"date": f"d{i}", "open": 100, "high": 100, "low": 98, "close": 100} for i in range(10)]
    bars.append({"date": "win", "open": 100, "high": 106, "low": 99.5, "close": 105})
    bars.append({"date": "stop", "open": 100, "high": 110, "low": 95, "close": 101})
    r = backtest(bars, k=0.5, ma=0)
    assert r["매매횟수"] == 2 and r["손절횟수"] == 1


def test_backtest_short_data_does_not_crash():
    assert backtest([{"date": "a", "open": 1, "high": 1, "low": 1, "close": 1}], ma=5)["거래일"] == 0


def flat(n=10):
    return [{"date": f"d{i:02d}", "open": 100, "high": 100, "low": 98, "close": 100} for i in range(n)]


def test_optimistic_range_only_differs_on_ambiguous_stop():
    # 저가는 손절선 아래지만 종가는 회복 → 최악=손절, 최선=종가 청산
    bars = flat() + [{"date": "x", "open": 100, "high": 106, "low": 95, "close": 105}]
    lo = backtest(bars, k=0.5, ma=0, stop_pct=2)
    hi = backtest(bars, k=0.5, ma=0, stop_pct=2, optimistic=True)
    assert lo["손절횟수"] == 1 and hi["손절횟수"] == 0
    assert hi["누적수익(%)"] > 0 > lo["누적수익(%)"]


def test_next_open_exit_captures_overnight_gap():
    bars = flat() + [{"date": "x", "open": 100, "high": 102, "low": 100, "close": 101},
                     {"date": "y", "open": 105, "high": 105, "low": 104, "close": 104}]
    same_day = backtest(bars, k=0.5, ma=0, fee_pct=0, slip_pct=0)
    overnight = backtest(bars, k=0.5, ma=0, fee_pct=0, slip_pct=0, exit="next_open")
    assert same_day["누적수익(%)"] == pytest.approx(0.0)  # 101에 사서 101에 팜
    assert overnight["누적수익(%)"] == pytest.approx((105 / 101 - 1) * 100, abs=0.01)


def test_next_open_skips_last_day():
    bars = flat() + [{"date": "x", "open": 100, "high": 110, "low": 100, "close": 108}]
    assert backtest(bars, k=0.5, ma=0, exit="next_open")["매매횟수"] == 0
