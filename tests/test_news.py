from datetime import datetime, timedelta

import pytest

import news_backtest as nb
from minute_backtest import ET


def test_classify_rules():
    assert nb.classify("Acme Beats Q3 Estimates, Raises FY Guidance") == ("호재", "실적")
    assert nb.classify("FDA Approves Acme's Drug for Migraine") == ("호재", "승인·임상")
    assert nb.classify("Acme Agrees To Be Acquired By MegaCorp For $5B")[0] == "호재"
    assert nb.classify("Morgan Stanley Upgrades Acme To Overweight") == ("호재", "투자의견")
    assert nb.classify("Acme Misses Estimates, Cuts Outlook") == ("악재", "")
    assert nb.classify("Acme Announces $200M Public Offering") == ("악재", "")
    assert nb.classify("Acme Beats On Revenue But Lowers Guidance") == (None, "")      # 섞이면 판정 안 함
    assert nb.classify("Acme Shares Are Trading Higher After Earnings Beat") == (None, "")  # 사후 기사 제외
    assert nb.classify("Acme Stock Soars After FDA Approval") == (None, "")
    assert nb.classify("Acme To Present At Conference") == (None, "")


def day(d, start_px, n=390, step=0.0):
    """09:30부터 n분 1분봉, 가격은 start_px 에서 분마다 step 씩"""
    t0 = datetime.strptime(d + "0930", "%Y%m%d%H%M")
    out = []
    for i in range(n):
        p = start_px + i * step
        out.append({"date": d, "time": (t0 + timedelta(minutes=i)).strftime("%H:%M"),
                    "open": p, "high": p, "low": p, "close": p, "volume": 1})
    return (d, out)


def et(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)


DAYS = [day("20240102", 100), day("20240103", 100, step=0.01), day("20240104", 110),
        day("20240105", 111), day("20240108", 112), day("20240109", 113), day("20240110", 120)]


def test_measure_intraday_news_with_delay():
    r = nb.measure(DAYS, et("2024-01-03 10:00:20"), 5, fee_pct=0, slip_pct=0)
    # 10:06 봉 시가에 매수 (기사 다음 완성된 분 10:01 + 5분), 직전 가격은 09:59 봉 종가
    assert r["moved"] == pytest.approx((100 + 36 * 0.01) / (100 + 29 * 0.01) - 1)
    assert r["30분"] == pytest.approx((100 + 66 * 0.01) / (100 + 36 * 0.01) - 1)
    assert r["당일종가"] == pytest.approx((100 + 389 * 0.01) / (100.36) - 1)
    assert r["다음날시가"] == pytest.approx(110 / 100.36 - 1)
    assert r["5일뒤종가"] == pytest.approx(120 / 100.36 - 1)
    assert not r["night"]


def test_measure_after_close_and_weekend_go_to_next_open():
    r = nb.measure(DAYS, et("2024-01-04 17:30:00"), 1, fee_pct=0, slip_pct=0)
    assert r["night"] and r["moved"] == pytest.approx(111 / 110 - 1)      # 다음날(1/5) 시가 진입
    r = nb.measure(DAYS, et("2024-01-06 12:00:00"), 1, fee_pct=0, slip_pct=0)
    assert r["night"] and r["moved"] == pytest.approx(112 / 111 - 1)      # 토요일 기사 → 월요일 시가
    r = nb.measure(DAYS, et("2024-01-03 08:00:00"), 15, fee_pct=0, slip_pct=0)
    assert r["night"] and r["moved"] == pytest.approx(0)                  # 장 전 기사 → 그날 시가
    assert nb.measure(DAYS, et("2024-01-10 17:00:00"), 1) is None         # 다음 거래일 데이터 없음


def test_measure_costs():
    r = nb.measure(DAYS, et("2024-01-04 11:00:00"), 0, fee_pct=0.25, slip_pct=0.05)
    assert r["당일종가"] == pytest.approx(0.9995 * 0.9975 * 0.9975 / 1.0005 - 1)


def test_events_dedup_and_filters():
    items = [
        {"t": "2024-01-03T15:00:00Z", "h": "Acme Beats Estimates", "s": ["ACME"]},
        {"t": "2024-01-03T16:00:00Z", "h": "Acme Raises Dividend", "s": ["ACME"]},       # 같은 날 두 번째 호재 → 제외
        {"t": "2024-01-03T17:00:00Z", "h": "Acme Misses On Margins", "s": ["ACME"]},     # 악재는 따로
        {"t": "2024-01-04T15:00:00Z", "h": "5 Stocks Upgraded Today", "s": ["A", "B", "C", "D", "ACME"]},
        {"t": "2024-01-05T15:00:00Z", "h": "Other Beats Estimates", "s": ["OTHER"]},
    ]
    ev = nb.events("ACME", items)
    assert [(e[0].strftime("%Y-%m-%d %H:%M"), e[1]) for e in ev] == [("2024-01-03 10:00", "호재"),
                                                                      ("2024-01-03 12:00", "악재")]
    assert nb.months_needed(ev) == [(2024, 1)]


class R:
    def __init__(self, data):
        self.status_code, self._d, self.text = 200, data, ""

    def json(self):
        return self._d


class H:
    def __init__(self):
        self.calls = []

    def get(self, url, headers, params, timeout):
        self.calls.append(dict(params))
        if not params.get("page_token"):
            return R({"news": [{"created_at": "2024-01-03T15:00:00Z", "headline": "A", "symbols": ["X"]}],
                      "next_page_token": "p2"})
        return R({"news": [{"created_at": "2024-01-04T15:00:00Z", "headline": "B", "symbols": ["X"]}],
                  "next_page_token": None})


def test_news_pagination_and_cache(tmp_path):
    from datetime import date
    h = H()
    api = nb.AlpacaNews("k", "s", cache=tmp_path, session=h)
    got = api.news_month("X", 2024, 1, today=date(2024, 3, 1))
    assert [n["h"] for n in got] == ["A", "B"] and len(h.calls) == 2
    api.news_month("X", 2024, 1, today=date(2024, 3, 1))
    assert len(h.calls) == 2                                              # 지난 달은 저장분 사용


def test_report_smoke(capsys):
    t = et("2024-01-03 10:00:20")
    res = {0: nb.measure(DAYS, t, 0), 1: nb.measure(DAYS, t, 1)}
    results = [("ACME", t, "호재", "실적", "Acme Beats", res), ("ACME", t, "악재", "", "Acme Misses", res)]
    nb.report(results, [0, 1], examples=1)
    out = capsys.readouterr().out
    assert "호재 뉴스" in out and "악재 뉴스" in out and "장중(바로 매수)" in out and "실적" in out
