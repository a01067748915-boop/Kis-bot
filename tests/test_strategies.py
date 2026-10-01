import pytest

from strategies import rotation_holds, simulate, summary, trend_holds


def bar(d, o, c):
    return {"date": d, "open": o, "high": max(o, c), "low": min(o, c), "close": c}


def test_simulate_buy_hold_and_switch_costs():
    dates = ["d1", "d2", "d3"]
    data = {"A": {"d1": bar("d1", 100, 100), "d2": bar("d2", 110, 120), "d3": bar("d3", 120, 132)},
            "B": {"d1": bar("d1", 50, 50), "d2": bar("d2", 50, 50), "d3": bar("d3", 50, 55)}}
    curve, orders = simulate(dates, data, ["A", "A", "A"], fee_pct=0, slip_pct=0)
    assert curve[-1] == pytest.approx(1.32) and orders == 1
    # d3 시가에 A 매도(120/120) → B 매수(50→55)
    curve, orders = simulate(dates, data, ["A", "A", "B"], fee_pct=0, slip_pct=0)
    assert curve[-1] == pytest.approx(1.2 * 1.1) and orders == 3
    curve, _ = simulate(dates, data, ["A", "A", "A"], fee_pct=0.25, slip_pct=0)
    assert curve[-1] == pytest.approx(1.32 * 0.9975)


def test_trend_exits_below_ma_and_band_reduces_flips():
    closes = [100] * 5 + [110, 111, 99, 101, 98, 120]
    bars = [bar(f"d{i:02d}", c, c) for i, c in enumerate(closes)]
    holds = trend_holds(bars, "A", ma=5)
    assert holds[6] == "A"      # 전날 110 > 평균 102
    assert holds[8] is None     # 전날 99 < 평균
    banded = trend_holds(bars, "A", ma=5, band=5)
    flips = lambda h: sum(1 for x, y in zip(h, h[1:]) if x != y)
    assert flips(banded) <= flips(holds)


def test_rotation_picks_leader_monthly_and_goes_safe():
    dates = [f"2024{m:02d}{d:02d}" for m in (1, 2, 3) for d in (1, 2, 3)]
    up = {d: bar(d, 100 + i, 100 + i) for i, d in enumerate(dates)}
    flat = {d: bar(d, 100, 100) for d in dates}
    down = {d: bar(d, 100 - i, 100 - i) for i, d in enumerate(dates)}
    holds = rotation_holds(dates, {"UP": up, "FLAT": flat}, ["UP", "FLAT"], "SAFE", lookback=2)
    assert holds[3] == "UP" and holds[4] == "UP"   # 2월 첫날 교체 후 한 달 유지
    holds = rotation_holds(dates, {"DN": down, "FLAT": flat}, ["DN", "FLAT"], "SAFE", lookback=2)
    assert holds[3] == "SAFE"                       # 1등도 0% 이하 → 대피


def test_summary_drawdown():
    r = summary([1.0, 1.2, 0.9, 1.1])
    assert r["낙폭"] == pytest.approx(-25.0) and r["수익"] == pytest.approx(10.0)
