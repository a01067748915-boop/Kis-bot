import csv
import json
from datetime import datetime

import pytest

import bot as botmod
import kis_api
from bot import ET, Bot, Config, Telegram
from kis_api import KIS, KISError

TODAY = "20260105"  # 월요일


def at(h, m):
    return datetime(2026, 1, 5, h, m, tzinfo=ET)


def bars(today_open=100.0):
    prev = [{"date": f"202512{d:02d}", "open": 100, "high": 102, "low": 98, "close": 99} for d in range(20, 31)]
    return prev + [{"date": TODAY, "open": today_open, "high": today_open, "low": today_open, "close": today_open}]


class FakeAPI:
    def __init__(self):
        self.prices = {"QQQM": 100.0}
        self.held = {}
        self.orders = []
        self.cancels = []
        self.fill = None  # (qty, price) 또는 None → 잔고 변화로 판단
        self.fill_error = False

    def price(self, sym, ex):
        return self.prices[sym]

    def daily_bars(self, sym, ex, base_date=""):
        return bars()

    def holdings(self, ex):
        return {s: dict(v) for s, v in self.held.items()}

    def limit_order(self, side, sym, ex, qty, price):
        self.orders.append((side, sym, qty, price))
        return "0001"

    def order_fill(self, order_no, ex, date):
        if self.fill_error:
            raise KISError("조회 실패")
        return self.fill

    def cancel(self, sym, ex, order_no, qty):
        self.cancels.append(qty)
        return True


class FakeTG:
    def __init__(self):
        self.sent, self.queue = [], []

    def send(self, text):
        self.sent.append(text)

    def commands(self):
        q, self.queue = self.queue, []
        return q


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod.time, "sleep", lambda s: None)

    def _make(**env):
        cfg = Config({"TARGETS": "QQQM:NAS", "BUDGET_USD": "1000", "MA_FILTER": "0", **env})
        api, tg = FakeAPI(), FakeTG()
        b = Bot(cfg, api, tg, tmp_path / "state.json", tmp_path / "trades.csv")
        return b, api, tg

    return _make


def journal(b):
    with b.journal_file.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_breakout_buy_stop_loss_and_journal(make):
    b, api, tg = make()
    b.step(at(9, 40))  # 목표 = 100 + 0.5*4 = 102
    assert b.state["plans"]["QQQM"]["target"] == 102
    api.prices["QQQM"] = 102.5
    b.step(at(9, 41))
    assert b.state["positions"]["QQQM"]["qty"] == 9
    api.prices["QQQM"] = 100.0  # -2.4% → 손절
    b.step(at(9, 42))
    assert "QQQM" not in b.state["positions"]
    assert b.state["realized"] < 0
    rows = journal(b)
    assert [r["side"] for r in rows] == ["buy", "sell"]
    assert rows[1]["reason"].startswith("손절")
    api.prices["QQQM"] = 103  # 하루 1회만
    b.step(at(9, 43))
    assert "QQQM" not in b.state["positions"]


def test_no_chase(make):
    b, api, tg = make()
    b.step(at(9, 40))
    api.prices["QQQM"] = 104  # 목표 102의 +1% 초과
    b.step(at(9, 41))
    assert not b.state["positions"] and "QQQM" in b.state["traded"]


def test_exit_time_liquidation(make):
    b, api, tg = make()
    b.step(at(9, 40))
    api.prices["QQQM"] = 102.2
    b.step(at(10, 0))
    api.prices["QQQM"] = 105
    b.step(at(15, 45))
    assert not b.state["positions"] and b.state["closed"]
    assert b.state["realized"] > 0


def test_daily_loss_limit_and_resume_keeps_liquidation(make):
    b, api, tg = make(STOP_LOSS_PCT="50", DAILY_LOSS_LIMIT_PCT="1")
    b.step(at(9, 40))
    api.prices["QQQM"] = 102.2
    b.step(at(9, 41))
    api.prices["QQQM"] = 99  # 약 -3% × 9주 ≈ -30달러 > 한도 10달러
    b.step(at(9, 42))
    assert b.state["halted"] and b.state["liquidate"] and not b.state["positions"]
    tg.queue = ["/resume"]
    b.step(at(9, 43))
    assert b.state["liquidate"] and not b.state["paused"]


def test_stop_command_and_resume(make):
    b, api, tg = make()
    b.step(at(9, 40))
    api.prices["QQQM"] = 102.2
    b.step(at(9, 41))
    tg.queue = ["/stop"]
    b.step(at(9, 42))
    assert b.state["paused"] and not b.state["positions"]
    tg.queue = ["/resume"]
    b.step(at(9, 43))
    assert not b.state["paused"] and not b.state["liquidate"]


def test_carry_over_sold_next_day(make):
    b, api, tg = make()
    b.state = b._fresh("20260102")
    b.state["positions"] = {"QQQM": {"qty": 3, "entry": 100.0, "ex": "NAS"}}
    b._save()
    b.step(at(9, 40))
    assert not b.state["positions"]
    assert journal(b)[0]["reason"] == "전날 이월분 정리"


def test_holiday_skip(make):
    b, api, tg = make()
    api.daily_bars = lambda *a, **k: bars()[:-1]
    b.step(at(9, 40))
    assert b.state["skip_day"] and not b.state["plans"]


def test_old_state_file_gets_missing_keys(make, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"date": TODAY, "plans": {}, "positions": {}}))
    b, api, tg = make()
    assert b.state["liquidate"] is False and b.state["trades"] == []


# ─── 실주문 경로(DRY_RUN=false) ───
def test_sell_uses_real_fill_price(make):
    b, api, tg = make(DRY_RUN="false")
    b.state = b._fresh(TODAY)
    b.state["positions"] = {"QQQM": {"qty": 5, "entry": 100.0, "ex": "NAS"}}
    api.held = {"QQQM": {"qty": 5, "avg": 100.0}}
    api.fill = (5, 101.37)
    b._sell("QQQM", 101.0, "테스트")
    assert b.state["trades"][-1]["price"] == 101.37
    assert b.state["realized"] == pytest.approx(b._pnl(100.0, 101.37, 5))


def test_partial_fill_cancels_only_remainder(make):
    b, api, tg = make(DRY_RUN="false")
    b.state = b._fresh(TODAY)
    api.fill = (3, 100.2)
    filled, price = b._execute("buy", "QQQM", 5, 100.0)
    assert (filled, price) == (3, 100.2)
    assert api.cancels == [2]


def test_fallback_to_holdings_when_fill_inquiry_fails(make):
    b, api, tg = make(DRY_RUN="false")
    b.state = b._fresh(TODAY)
    api.fill_error = True
    api.held = {"QQQM": {"qty": 2, "avg": 90.0}}
    orig = api.limit_order

    def order(*a):
        api.held["QQQM"] = {"qty": 6, "avg": 97.5}  # 4주 @ 101.25 체결
        return orig(*a)

    api.limit_order = order
    filled, price = b._execute("buy", "QQQM", 4, 101.0)
    assert filled == 4 and price == pytest.approx(101.25)
    # 매도는 잔고로 체결가를 알 수 없으므로 지정가(보수적)로 기록
    api.held["QQQM"] = {"qty": 6, "avg": 97.5}
    api.limit_order = lambda *a: api.held.update(QQQM={"qty": 2, "avg": 97.5}) or "2"
    sold, fill = b._execute("sell", "QQQM", 4, 100.0)
    assert sold == 4 and fill == pytest.approx(99.7)


def test_sell_all_continues_after_error(make):
    b, api, tg = make()
    b.state = b._fresh(TODAY)
    b.state["positions"] = {"BAD": {"qty": 1, "entry": 1.0, "ex": "NAS"},
                            "QQQM": {"qty": 1, "entry": 100.0, "ex": "NAS"}}
    b.sell_all({}, "테스트")  # BAD 가격 조회 실패해도 QQQM 은 매도
    assert list(b.state["positions"]) == ["BAD"]


# ─── 텔레그램 ───
def test_telegram_ignores_stale_and_parses_mentions(monkeypatch):
    tg = Telegram("TOKEN123", "42")
    now = tg.started
    updates = {"result": [
        {"update_id": 1, "message": {"date": now - 100, "text": "/stop", "chat": {"id": 42}}},
        {"update_id": 2, "message": {"date": now + 1, "text": "/Status@MyBot", "chat": {"id": 42}}},
        {"update_id": 3, "message": {"date": now + 1, "text": "/stop", "chat": {"id": 7}}},
    ]}

    class R:
        def json(self):
            return updates

    monkeypatch.setattr(botmod.requests, "get", lambda *a, **k: R())
    assert tg.commands() == ["/status"]
    assert tg.offset == 4
    assert "TOKEN123" not in tg._safe(Exception("https://api.telegram.org/botTOKEN123/sendMessage"))


# ─── KIS 클라이언트 ───
class Resp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def test_kis_refreshes_expired_token_once(tmp_path, monkeypatch):
    monkeypatch.setattr(kis_api.time, "sleep", lambda s: None)
    api = KIS("real", "k", "s", "12345678-01", tmp_path)
    issued = []
    monkeypatch.setattr(kis_api.requests, "post",
                        lambda *a, **k: issued.append(1) or Resp({"access_token": f"t{len(issued)}", "expires_in": 86400}))
    calls = []

    def request(method, url, headers, **k):
        calls.append(headers["authorization"])
        if len(calls) == 1:
            return Resp({"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."})
        return Resp({"rt_cd": "0", "output": {"ODNO": "0000123"}})

    monkeypatch.setattr(kis_api.requests, "request", request)
    assert api.limit_order("buy", "QQQM", "NAS", 1, 100.0) == "0000123"  # retries=1 이어도 토큰 재발급 후 재시도
    assert calls == ["Bearer t1", "Bearer t2"]
    assert (tmp_path / ".token_real.json").stat().st_mode & 0o777 == 0o600


def test_kis_order_not_retried_on_business_error(tmp_path, monkeypatch):
    api = KIS("real", "k", "s", "12345678-01", tmp_path)
    api._cached = ("t", 9e12)
    calls = []
    monkeypatch.setattr(kis_api.requests, "request",
                        lambda *a, **k: calls.append(1) or Resp({"rt_cd": "1", "msg_cd": "APBK0000", "msg1": "잔고부족"}))
    with pytest.raises(KISError):
        api.limit_order("buy", "QQQM", "NAS", 1, 100.0)
    assert len(calls) == 1


def test_kis_order_fill(tmp_path, monkeypatch):
    api = KIS("real", "k", "s", "12345678-01", tmp_path)
    api._cached = ("t", 9e12)
    out = [{"odno": "0000123", "sll_buy_dvsn_cd": "02", "pdno": "QQQM", "ft_ord_qty": "5",
            "ft_ccld_qty": "3", "ft_ccld_unpr3": "101.5000"},
           {"odno": "0000999", "sll_buy_dvsn_cd": "01", "pdno": "QQQM", "ft_ord_qty": "1",
            "ft_ccld_qty": "0", "ft_ccld_unpr3": ""}]
    monkeypatch.setattr(kis_api.requests, "request", lambda *a, **k: Resp({"rt_cd": "0", "output": out}))
    assert api.order_fill("123", "NAS", TODAY) == (3, 101.5)
    assert api.order_fill("999", "NAS", TODAY) == (0, None)
    assert api.order_fill("555", "NAS", TODAY) is None
    assert api.order_fill("", "NAS", TODAY) is None


def test_env_example_parses_to_valid_config():
    from pathlib import Path

    from dotenv import dotenv_values
    env = dotenv_values(Path(__file__).resolve().parent.parent / ".env.example")
    cfg = Config({k: v for k, v in env.items() if v})
    assert cfg.env == "mock" and cfg.dry_run and cfg.targets == {"QQQM": "NAS", "SOXX": "NAS"}
    assert cfg.entry_start == (9, 35) and cfg.k == 0.5
