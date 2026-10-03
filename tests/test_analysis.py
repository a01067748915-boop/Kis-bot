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


def test_real_price_sizing_blocks_unaffordable_shares_before_split():
    from strategies import slot_engine
    closes = [100 + i for i in range(60)]
    adj = bars_from(closes)
    split_at = 30  # 이전엔 실제 가격이 10배 (10:1 분할 전)
    raw = [dict(b, open=b["open"] * (10 if i < split_at else 1), close=b["close"] * (10 if i < split_at else 1))
           for i, b in enumerate(adj)]
    ind = {"X": {b["date"]: {} for b in adj}}
    run = lambda raw_: slot_engine({"X": adj}, ind, 500, 1, lambda s, y, p: False,
                                   lambda y, d, ready, held: ["X"] if ready("X", d) else [],
                                   fee_pct=0, slip_pct=0, raw=raw_)
    plain, real = run(None), run({"X": raw})
    assert plain["skips"] == 0 and plain["open"] == 1             # 수정주가로는 첫날부터 매수
    assert real["skips"] >= split_at - 2 and real["open"] == 1      # 분할 전엔 실제 1주(1,000달러대)를 못 삼
    # 분할 뒤 실제 가격으로 산 주식 수 = 500 // 그날 실제 가격, 남은 돈은 현금
    buy = next(i for i in range(len(adj)) if i >= split_at - 1 and 500 // raw[i]["open"] > 0)
    shares = 500 // raw[buy]["open"]
    expect = 500 - shares * raw[buy]["open"] + shares * adj[-1]["close"] * raw[buy]["open"] / adj[buy]["open"]
    assert real["curve"][-1] == pytest.approx(expect)


def test_fetch_bars_cache(tmp_path):
    from backtest import fetch_bars

    class A:
        calls = 0

        def daily_history(self, sym, ex, since=None, adjusted=True):
            A.calls += 1
            return [{"date": "20260101", "close": 1.0 if adjusted else 10.0}]
    a = A()
    assert fetch_bars(a, "X", "NAS", 1, cache=tmp_path)[0]["close"] == 1.0
    assert fetch_bars(a, "X", "NAS", 1, adjusted=False, cache=tmp_path)[0]["close"] == 10.0
    fetch_bars(a, "X", "NAS", 1, cache=tmp_path)
    assert A.calls == 2                                              # 같은 날 다시 부르면 저장분 사용


def test_run_verify_smoke(capsys):
    import random
    random.seed(4)

    def walk(drift):
        p, cl = 100.0, []
        for _ in range(500):
            p *= 1 + drift + random.gauss(0, 0.015)
            cl.append(p)
        return bars_from(cl)
    ds = {f"S{i}": walk(random.gauss(0.0005, 0.001)) for i in range(10)}
    raw = {s: b for s, b in ds.items()}

    class F:
        def __init__(self, g):
            self.g, self.rev = g, [1]

        def growth(self, y, min_prev=None, cap=None):
            return self.g
    funds = {s: F(random.random()) for s in ds}
    groups = [("A", {s: ds[s] for s in list(ds)[:5]}), ("B", {s: ds[s] for s in list(ds)[5:]}), ("합계", ds)]
    analysis.run_verify(groups, raw, funds, 1100, {"fee_pct": 0.25, "slip_pct": 0.05})
    out = capsys.readouterr().out
    assert "① 종목 묶음별" in out and "② 설정값" in out and "③ 실제 가격 반영 효과" in out
    assert "모멘텀+매출성장" in out and "12개월" in out


def test_psr_uses_shares_summed_per_filing_and_point_in_time():
    rev = [q(f"2023-{m:02d}-01", f"2023-{m + 2:02d}-28", 100e6, f"2023-{m + 3:02d}-15") for m in (1, 4, 7)]
    rev += [q("2023-01-01", "2023-12-31", 400e6, "2024-02-10")]
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": rev}}},
                       "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                           {"end": "2024-01-31", "val": 6e6, "filed": "2024-02-10", "accn": "x1"},   # A주
                           {"end": "2024-01-31", "val": 4e6, "filed": "2024-02-10", "accn": "x1"},   # C주
                           {"end": "2023-04-30", "val": 1e6, "filed": "2023-05-10", "accn": "x0"}]}}}}}
    f = fu.Fundamentals(facts)
    assert f.revenue("20240301") == pytest.approx(400e6)
    assert f.psr("20240301", 20.0) == pytest.approx(20 * 10e6 / 400e6)     # 같은 공시의 A·C 합산
    assert f.psr("20231201", 20.0) is None                                  # 4분기 매출 아직 없음


def test_garp_rules_and_picks_check(capsys):
    import random
    random.seed(7)

    def walk(drift):
        p, cl = 100.0, []
        for _ in range(520):
            p *= 1 + drift + random.gauss(0, 0.015)
            cl.append(p)
        return bars_from(cl)
    ds = {f"S{i}": walk(random.gauss(0.0004, 0.001)) for i in range(8)}
    ds["JOBY"] = walk(0.0)

    class F:
        def __init__(self, g, rev):
            self.g, self.rev_, self.rev = g, rev, [1]

        def growth(self, y, min_prev=None, cap=None):
            return self.g

        def revenue(self, y):
            return self.rev_

        def psr(self, y, price):
            return 5.0
    funds = {s: F(0.1 + i * 0.05, 200e6) for i, s in enumerate(ds) if s != "JOBY"}
    funds["JOBY"] = F(9.0, 1e6)                    # 매출 100만 달러 — 성장률이 튀어도 제외돼야 함
    a, b, c = analysis.garp_scores(ds, funds, ds)
    y = ds["S0"][300]["date"]
    assert a("JOBY", y) is None and b("JOBY", y) is None
    assert b("S7", y) > b("S1", y)                 # 같은 PSR 이면 성장률 높은 쪽
    assert a("S0", y) is None                      # 성장 하위 절반은 A 대상 아님
    groups = [("A", {k: ds[k] for k in list(ds)[:4]}), ("B", {k: ds[k] for k in list(ds)[4:]}), ("합계", ds)]
    analysis.run_garp(groups, ds, funds, 1100, {"fee_pct": 0.25, "slip_pct": 0.05}, ["JOBY", "ACHR"])
    out = capsys.readouterr().out
    assert "A 덜오른고성장" in out and "관심 종목 점검" in out
    assert "JOBY: 데이터" in out and "A·B·C 에서 제외" in out and "ACHR: 시세 없음" in out


def test_growth_floor_and_cap():
    def yr(y, v):
        return [q(f"{y}-01-01", f"{y}-03-31", v / 4, f"{y}-05-01"), q(f"{y}-04-01", f"{y}-06-30", v / 4, f"{y}-08-01"),
                q(f"{y}-07-01", f"{y}-09-30", v / 4, f"{y}-11-01"), q(f"{y}-01-01", f"{y}-12-31", v, f"{y + 1}-02-10")]
    tiny = fu.Fundamentals({"facts": {"us-gaap": {"Revenues": {"units": {"USD": yr(2022, 0.1e6) + yr(2023, 116e6)}}}}})
    small = fu.Fundamentals({"facts": {"us-gaap": {"Revenues": {"units": {"USD": yr(2022, 36e6) + yr(2023, 72e6)}}}}})
    d = "20240301"
    assert tiny.growth(d) == pytest.approx(1159.0)            # 거의 0 → +115,900%
    assert tiny.growth(d, min_prev=10e6) is None              # 1천만$ 기준에 걸림
    assert small.growth(d, min_prev=10e6) == pytest.approx(1.0)
    assert small.growth(d, min_prev=50e6) is None             # 5천만$ 기준이면 CELH 초기 같은 회사도 빠짐
    assert tiny.growth(d, cap=3.0) == 3.0                     # 상한은 종목을 남기고 값만 자름


def test_run_growthfix_ranks_picks(capsys):
    import random
    random.seed(9)

    def walk(d):
        p, cl = 100.0, []
        for _ in range(600):
            p *= 1 + d + random.gauss(0, 0.015)
            cl.append(p)
        return bars_from(cl)
    ds = {f"S{i}": walk(0.0004) for i in range(8)}
    ds["JOBY"], ds["ACHR"] = walk(0.0), walk(0.0)

    class F:
        def __init__(self, g, prev):
            self.g, self.prev, self.rev = g, prev, [1]

        def growth(self, y, min_prev=None, cap=None):
            if min_prev and self.prev < min_prev:
                return None
            return min(self.g, cap) if cap is not None else self.g
    funds = {s: F(0.2 + i * 0.1, 40e6) for i, s in enumerate(ds) if s not in ("JOBY", "ACHR")}
    funds["JOBY"] = F(1185.0, 0.1e6)
    groups = [("기본", {k: ds[k] for k in list(ds)[:5]}), ("추가", {k: ds[k] for k in list(ds)[5:]}), ("합계", ds)]
    analysis.run_growthfix(groups, ds, funds, 1100, {"fee_pct": 0.25, "slip_pct": 0.05}, ["JOBY", "ACHR"])
    out = capsys.readouterr().out
    rows = {l.split(":")[0].strip(): l for l in out.splitlines() if "JOBY" in l and "위" in l}
    assert "JOBY 1위" in rows["매출성장 조건없음"]
    assert "JOBY 순위 밖" in rows["매출성장 1년전≥1천만$"]          # 거의 0에서 튄 성장은 기준에 걸림
    assert "JOBY 순위 밖" in rows["+상한300%"]                       # 1천만$ 기준 + 상한 → 기준에서 걸림
    assert "ACHR 순위 밖" in rows["매출성장 조건없음"]
