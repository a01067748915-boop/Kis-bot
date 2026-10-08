from datetime import date

import pytest

import minute_backtest as mb


def m(t, o, h, l, c):
    return {"date": "20240102", "time": t, "open": o, "high": h, "low": l, "close": c, "volume": 1}


class FakeResp:
    def __init__(self, code, data=None, text=""):
        self.status_code, self._d, self.text = code, data, text

    def json(self):
        return self._d


class FakeHTTP:
    """페이지 2개로 나눠 주고, sip 은 403(무료 계정) 흉내"""

    def __init__(self, sip_ok=True):
        self.calls, self.sip_ok = [], sip_ok

    def get(self, url, headers, params, timeout):
        self.calls.append(dict(params))
        if params["feed"] == "sip" and not self.sip_ok:
            return FakeResp(403, text="forbidden")
        if not params.get("page_token"):
            return FakeResp(200, {"bars": [  # 1월 = 표준시(UTC-5): 14:30Z → 09:30 ET
                {"t": "2024-01-02T14:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10},
                {"t": "2024-01-02T13:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}],  # 장 전 → 제외
                "next_page_token": "p2"})
        return FakeResp(200, {"bars": [  # 7월 = 서머타임(UTC-4): 13:30Z → 09:30 ET
            {"t": "2024-07-01T13:30:00Z", "o": 3, "h": 4, "l": 2, "c": 3.5, "v": 5},
            {"t": "2024-07-01T20:00:00Z", "o": 3, "h": 3, "l": 3, "c": 3, "v": 5}],  # 16:00 → 제외
            "next_page_token": None})


def test_download_pages_timezone_and_cache(tmp_path):
    http = FakeHTTP()
    api = mb.AlpacaData("k", "s", cache=tmp_path, session=http)
    rows = api.month("SOXX", "1Min", 2024, 1, today=date(2024, 3, 5))
    assert [(r["date"], r["time"]) for r in rows] == [("20240102", "09:30"), ("20240701", "09:30")]
    assert len(http.calls) == 2 and http.calls[1]["page_token"] == "p2"
    assert http.calls[0]["adjustment"] == "all"
    # 지난 달은 저장 → 다시 받지 않음
    again = api.month("SOXX", "1Min", 2024, 1, today=date(2024, 3, 5))
    assert again == rows and len(http.calls) == 2
    # 이번 달은 저장 안 함
    api.month("SOXX", "1Min", 2024, 3, today=date(2024, 3, 5))
    assert not (tmp_path / "1Min" / "SOXX" / "2024-03.csv").exists()


def test_falls_back_to_iex_on_free_plan(tmp_path):
    http = FakeHTTP(sip_ok=False)
    api = mb.AlpacaData("k", "s", cache=tmp_path, session=http)
    api.month("X", "1Min", 2024, 1, today=date(2024, 3, 5))
    assert api.feed == "iex" and http.calls[-1]["feed"] == "iex"


def test_missing_keys():
    with pytest.raises(ValueError):
        mb.AlpacaData("", "", session=FakeHTTP())


def test_run_trade_stop_after_entry_vs_low_before_breakout():
    # 돌파(101) 뒤 손절선(99) 이탈 → 손절
    bars = [m("09:35", 100, 100.5, 99.5, 100), m("09:36", 100.5, 101.5, 100.4, 101.2),
            m("09:37", 101, 101, 98.5, 99), m("15:45", 99, 99, 99, 99)]
    assert mb.run_trade(bars, 101, 99, "09:35", "15:00", "15:45", 1) == (101, 99, "손절")
    # 저가(98.5)는 돌파 전에 나오고 이후 상승 → 청산 시각 시가에 매도
    bars = [m("09:35", 100, 100.2, 98.5, 100), m("09:36", 100.5, 101.5, 100.4, 101.2),
            m("15:44", 103, 103, 103, 103), m("15:45", 104, 104, 104, 104)]
    assert mb.run_trade(bars, 101, 99, "09:35", "15:00", "15:45", 1) == (101, 104, "청산")
    # 갭으로 목표가보다 너무 위에서 시작 → 추격 안 함
    bars = [m("09:35", 105, 106, 104, 105), m("15:45", 105, 105, 105, 105)]
    assert mb.run_trade(bars, 101, 99, "09:35", "15:00", "15:45", 1) is None


def day(d, rows):
    return d, [{"date": d, "time": t, "open": o, "high": h, "low": l, "close": c, "volume": 1}
               for t, o, h, l, c in rows]


def test_simulate_vb_and_orb_with_costs():
    days = [day("20240102", [("09:30", 100, 102, 98, 100), ("15:59", 100, 100, 100, 100)]),  # 전일 변동폭 4
            day("20240103", [("09:30", 100, 100.5, 99.8, 100.2), ("09:35", 100.2, 102.5, 100.1, 102.4),
                             ("15:45", 104, 104, 104, 104), ("15:59", 104, 104, 104, 104)])]
    r = mb.simulate(days, "vb", k=0.5, ma=0, fee_pct=0, slip_pct=0)
    assert r["trades"][0][0] == pytest.approx(104 / 102 - 1)   # 목표 102 매수 → 15:45 시가 104
    r2 = mb.simulate(days, "vb", k=0.5, ma=0, fee_pct=0.25, slip_pct=0.05)
    assert r2["trades"][0][1] == pytest.approx(104 / 102 - 1)  # 비용 전 수익은 같고
    assert r2["trades"][0][0] < r["trades"][0][0] - 0.005       # 비용 후는 약 0.6%p 낮음
    o = mb.simulate(days, "orb", orb_min=5, fee_pct=0, slip_pct=0)
    assert o["trades"][0][0] == pytest.approx(104 / 100.5 - 1)  # 첫 5분 고가 100.5 돌파
