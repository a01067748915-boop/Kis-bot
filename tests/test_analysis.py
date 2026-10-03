import pytest

import analysis
import fundamentals as fu
from signals import bollinger_indicators, ema, ichimoku_indicators, macd_indicators


def bars_from(closes, start=0):
    from datetime import date, timedelta
    d, out = date(2015, 1, 5), []
    while len(out) < len(closes):
        if d.weekday() < 5:
            c = closes[len(out)]
            out.append({"date": d.strftime("%Y%m%d"), "open": c, "high": c * 1.01, "low": c * 0.99, "close": c})
        d += timedelta(days=1)
    return out


def test_ema_and_macd_cross_up_then_down():
    assert ema([1, 1, 1], 3) == [1, 1, 1]
    closes = [100 + i * 0.5 for i in range(260)] + [230 - i * 1.5 for i in range(6)] + [221 + i for i in range(20)]
    ind = macd_indicators(bars_from(closes))
    dates = sorted(ind)
    entries = [d for d in dates if ind[d]["entry"]]
    exits = [d for d in dates if ind[d]["exit"]]
    assert exits and entries and min(exits) < max(entries)   # 하락 때 매도 신호, 반등 때 매수 신호
    weekly = macd_indicators(bars_from(closes), weekly=True)
    assert set(weekly) and all(isinstance(v["entry"], bool) for v in weekly.values())


def test_bollinger_revert_entry_below_lower_band():
    closes = [100 + (i % 2) * 0.5 for i in range(260)] + [90]
    ind = bollinger_indicators(bars_from(closes), "revert")
    last = ind[max(ind)]
    assert last["entry"] and not last["exit"]


def test_ichimoku_uptrend_entry_and_exit_on_drop():
    closes = [100] * 230 + [100 + i for i in range(40)] + [130 - 3 * i for i in range(10)]
    ind = ichimoku_indicators(bars_from(closes))
    dates = sorted(ind)
    assert any(ind[d]["entry"] for d in dates[:-10]) and ind[dates[-1]]["exit"]


# ─── SEC 재무 ───
def q(start, end, val, filed):
    return {"start": start, "end": end, "val": val, "filed": filed}


def test_quarterly_derives_q4_and_ttm_point_in_time():
    rev = [q("2022-01-01", "2022-03-31", 10, "2022-05-01"), q("2022-04-01", "2022-06-30", 10, "2022-08-01"),
           q("2022-07-01", "2022-09-30", 10, "2022-11-01"), q("2022-01-01", "2022-12-31", 50, "2023-02-15"),
           q("2023-01-01", "2023-03-31", 12, "2023-05-01"), q("2023-04-01", "2023-06-30", 12, "2023-08-01"),
           q("2023-07-01", "2023-09-30", 12, "2023-11-01"), q("2023-01-01", "2023-12-31", 60, "2024-02-15"),
           q("2022-01-01", "2022-03-31", 99, "2023-05-01")]   # 나중에 고친 값 → 처음 값 사용
    qs = fu.quarterly(rev)
    assert [x[1] for x in qs] == [10, 10, 10, 20, 12, 12, 12, 24]   # 4분기 = 연간 − 1~3분기
    assert fu.ttm(qs, "20240101") == 20 + 12 + 12 + 12       # 2023 4분기는 아직 미제출
    assert fu.ttm(qs, "20240301") == 60
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": rev}},
        "NetIncomeLoss": {"units": {"USD": [dict(x, val=x["val"] / 10) for x in rev[:8]]}},
        "StockholdersEquity": {"units": {"USD": [{"end": "2023-12-31", "val": 30, "filed": "2024-02-15"}]}}}}}
    f = fu.Fundamentals(facts)
    assert f.growth("20240301") == pytest.approx(60 / 50 - 1)
    assert f.growth("20230101") is None                       # 8분기 안 쌓임
    assert f.roe("20240301") == pytest.approx(6 / 30)
    assert f.roe("20240101") is None                          # 자기자본 미제출


def test_edgar_cik_lookup_and_cache(tmp_path):
    class R:
        def __init__(self, code, data):
            self.status_code, self._d = code, data

        def json(self):
            return self._d

    class H:
        calls = []

        def get(self, url, headers, timeout):
            self.calls.append((url, headers["User-Agent"]))
            if "company_tickers" in url:
                return R(200, {"0": {"cik_str": 1045810, "ticker": "NVDA"}})
            return R(200, {"facts": {}})
    h = H()
    e = fu.Edgar("me test@example.com", cache=tmp_path, session=h)
    assert e.facts("NVDA") == {"facts": {}} and e.facts("SOXX") is None
    assert "CIK0001045810" in h.calls[1][0] and h.calls[0][1] == "me test@example.com"
    e2 = fu.Edgar("me test@example.com", cache=tmp_path, session=h)
    e2.facts("NVDA")
    assert len(h.calls) == 2                                  # 저장분 재사용
    with pytest.raises(ValueError):
        fu.Edgar("no-email")


# ─── 포트폴리오 ───
def test_factor_portfolio_monthly_top_n_and_ranks():
    import random
    random.seed(1)

    def walk(drift):
        p, cl = 100.0, []
        for _ in range(320):
            p *= 1 + drift + random.gauss(0, 0.005)
            cl.append(p)
        return bars_from(cl)
    ds = {"UP": walk(0.003), "MID": walk(0.001), "DOWN": walk(-0.002), "FLAT": walk(0.0)}
    r = analysis.factor_portfolio(ds, analysis.momentum_score(ds), 1000, slots=1, fee_pct=0, slip_pct=0)
    held = [t[2] for t in r["trades"]]
    assert r["curve"][-1] > 1000 and "DOWN" not in held      # 모멘텀 1등(주로 UP) 보유
    ranks = analysis._ranks(["A", "B", "C"], "d", [lambda s, y: {"A": 1, "B": 2, "C": 3}[s],
                                                    lambda s, y: {"A": 3, "B": 2, "C": None}[s]])
    assert set(ranks) == {"A", "B"}                           # 점수 하나라도 없으면 제외
    sig = analysis.signal_portfolio(ds, lambda b: macd_indicators(b), 1000, slots=2, fee_pct=0, slip_pct=0)
    assert sig["dates"] and isinstance(sig["trades"], list)
