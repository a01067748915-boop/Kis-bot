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


def test_rsi_extremes():
    from strategies import rsi
    assert rsi([1, 2, 3, 4])[3] == 100.0
    assert rsi([4, 3, 2, 1])[3] == pytest.approx(0.0)


def test_pullback_buys_dip_sells_on_recovery_with_costs():
    from strategies import pullback
    closes = [50 + i * 2 for i in range(30)] + [100, 94, 96, 104, 108, 110]  # 20일선 위 눌림
    bars = [bar(f"d{i:02d}", c, c) for i, c in enumerate(closes)]
    curve, trades, start = pullback(bars, rsi_max=10, trend_ma=20, fee_pct=0, slip_pct=0)
    assert len(trades) == 1
    ret, days, i = trades[0]
    # 94 급락(RSI<10) → 다음날 시가 96 매수 → 104 가 5일선 회복 → 다음날 시가 108 매도
    assert ret == pytest.approx(108 / 96 - 1) and days == 2
    _, costly, _ = pullback(bars, rsi_max=10, trend_ma=20, fee_pct=0.25, slip_pct=0.05)
    assert costly[0][0] < ret


def test_pullback_stop_loss():
    from strategies import pullback
    closes = [50 + i * 2 for i in range(30)] + [100, 94]
    bars = [bar(f"d{i:02d}", c, c) for i, c in enumerate(closes)]
    bars.append({"date": "x", "open": 96, "high": 96, "low": 85, "close": 90})  # 매수 당일 -8% 이탈
    bars.append(bar("y", 90, 90))
    _, trades, _ = pullback(bars, rsi_max=10, trend_ma=20, stop_pct=8, fee_pct=0, slip_pct=0)
    assert trades[0][0] == pytest.approx(-0.08)


def _dip_series(dip_day, drop, n=40):
    """꾸준히 오르다 dip_day 에 drop 만큼 급락, 다음 날부터 회복"""
    closes = [50 + i * 3 for i in range(n)]
    closes[dip_day] = closes[dip_day - 1] - drop
    closes[dip_day + 1] = closes[dip_day] + 2
    return [bar(f"d{i:02d}", c, c) for i, c in enumerate(closes)]


def test_portfolio_fills_slots_by_lowest_rsi_and_whole_shares():
    from strategies import portfolio
    data = {"A": _dip_series(30, 16), "B": _dip_series(30, 22), "C": _dip_series(34, 16)}
    kw = dict(rsi_max=20, trend_ma=20, fee_pct=0, slip_pct=0)
    one = portfolio(data, budget=1000, slots=1, **kw)
    # 같은 날 A·B 신호 → 더 깊이 빠진(RSI 낮은) B 를 117에 사서 149에 팖(A는 칸이 없어 건너뜀), 이후 C 135→161
    assert [round(t[0], 4) for t in one["trades"]] == [round(149 / 117 - 1, 4), round(161 / 135 - 1, 4)]
    two = portfolio(data, budget=1000, slots=2, **kw)
    assert len(two["trades"]) == 3  # 칸이 2개면 A도 삼
    poor = portfolio(data, budget=50, slots=2, **kw)  # 칸 예산 25달러로는 1주도 못 삼
    assert poor["trades"] == [] and poor["curve"][-1] == 50


def test_yearly_returns():
    from strategies import yearly
    assert yearly(["20230101", "20231231", "20240101", "20241231"], [110, 121, 121, 133.1], 100) \
        == "2023 +21% 2024 +10%"


def mbar(d, c, v):
    return {"date": d, "open": c, "high": c, "low": c, "close": c, "volume": v}


def test_market_indicators_panic_and_trend():
    from signals import market_indicators
    bars = [mbar(f"d{i:02d}", 100 + i, 1000) for i in range(30)]
    bars.append(mbar("d30", 120, 2000))   # 하락 + 거래량 2배 → 투매
    bars.append(mbar("d31", 121, 1000))
    bars.append(mbar("d32", 122, 1000))
    bars.append(mbar("d33", 123, 1000))
    bars.append(mbar("d34", 115, 1200))   # 하락했지만 거래량 1.2배 → 투매 아님
    m = market_indicators(bars, trend_ma=20, vol_ma=20)
    assert m["d30"]["panic"] and m["d30"]["vol_ratio"] == 2.0
    assert m["d32"]["panic_recent"] and not m["d33"]["panic_recent"]   # 3거래일 창
    assert not m["d34"]["panic"] and m["d29"]["trend"]


def test_portfolio_market_filters_gate_entries():
    from signals import market_indicators
    from strategies import portfolio
    data = {"A": _dip_series(30, 16), "B": _dip_series(30, 22), "C": _dip_series(34, 16)}
    kw = dict(budget=1000, slots=2, rsi_max=20, trend_ma=20, fee_pct=0, slip_pct=0)
    up = [mbar(f"d{i:02d}", 100 + i, 1000) for i in range(40)]
    base = portfolio(data, **kw)
    m_up = market_indicators(up, trend_ma=20)
    assert portfolio(data, market=m_up, mfilter="trend", **kw)["trades"] == base["trades"]  # 상승장: 그대로
    assert portfolio(data, market=m_up, mfilter="panic", **kw)["trades"] == []              # 투매 없음: 쉼
    down = [mbar(f"d{i:02d}", 200 - i, 1000) for i in range(40)]
    r = portfolio(data, market=market_indicators(down, trend_ma=20), mfilter="trend", **kw)
    assert r["trades"] == [] and r["open_days"] == 0                                        # 하락장: 쉼
    # 시장 과매도(계속 하락 → RSI 0): 종목 RSI 기준 없이 200일선 위 종목을 삼
    r = portfolio(data, market=market_indicators(down, trend_ma=20), mfilter="mrsi", **kw)
    assert len(r["trades"]) >= len(base["trades"])


def test_portfolio_late_symbol_joins_and_expensive_skipped():
    from strategies import portfolio
    base = {"A": _dip_series(30, 16), "B": _dip_series(30, 22)}
    late = _dip_series(34, 16)[10:]                     # 10일 늦게 시작 → 지표 준비도 늦음
    pricey = [dict(b, open=b["open"] * 100, close=b["close"] * 100, high=b["high"] * 100, low=b["low"] * 100)
              for b in _dip_series(30, 22)]
    kw = dict(budget=1000, slots=3, rsi_max=20, trend_ma=20, fee_pct=0, slip_pct=0)
    ref = portfolio(base, **kw)
    r = portfolio({**base, "L": late, "P": pricey}, start=ref["dates"][0], **kw)
    assert r["dates"][0] == ref["dates"][0]             # 늦은 종목 때문에 기간이 줄지 않음
    traded = {t[2] for t in r["trades"]}
    assert {"A", "B"} <= traded and "P" in r["skipped"]


def test_parse_targets_from_file(tmp_path):
    from kis_api import parse_targets
    f = tmp_path / "u.txt"
    f.write_text("# 제목\nNVDA:NAS   # 설명\n\nCRM:NYS, amd\n", encoding="utf-8")
    assert parse_targets(f"@{f}") == {"NVDA": "NAS", "CRM": "NYS", "AMD": "NAS"}
    assert len(parse_targets("@universes/growth_balanced.txt")) == 39


def test_common_start_and_hold_curve_with_late_listing():
    from strategies import common_start, hold_curve
    full = {k: _dip_series(30, 5) for k in "ABC"}
    full["L"] = _dip_series(30, 5)[25:]               # 시작일 뒤 상장
    start = common_start(full, trend_ma=20)
    assert start == "d19"                              # 4개 중 3개(75%) 준비된 날
    dates = [b["date"] for b in full["A"] if b["date"] >= start]
    curve, n = hold_curve(full, dates, 1000, fee_pct=0, slip_pct=0)
    assert n == 3 and curve[0] == pytest.approx(1000)  # 시작일에 시세 있는 3개만 균등 매수


def test_breakout_indicators_and_portfolio():
    from signals import breakout_indicators
    from strategies import breakout_portfolio
    closes = [100] * 30 + [101 + i for i in range(10)] + [110 - 3 * i for i in range(10)]
    bars = [bar(f"d{i:02d}", c, c) for i, c in enumerate(closes)]
    ind = breakout_indicators(bars, entry_n=5, exit_n=3, trend_ma=0, mom_n=5)
    assert ind["d30"]["entry"] and not ind["d29"]["entry"]   # 횡보 후 첫 신고가
    assert ind["d41"]["exit"] and not ind["d39"]["exit"]     # 하락 전환 후 3일 최저 이탈
    flat = [bar(f"d{i:02d}", 50, 50) for i in range(len(closes))]
    r = breakout_portfolio({"UP": bars, "FLAT": flat}, budget=1000, slots=1, entry_n=5, exit_n=3,
                           trend_ma=0, mom_n=5, fee_pct=0, slip_pct=0)
    assert [t[2] for t in r["trades"]] == ["UP"]
    ret, days, _ = r["trades"][0]
    assert ret == pytest.approx(closes[42] / closes[31] - 1)  # d31 시가 매수 → d42 시가 매도
    assert days == 11
