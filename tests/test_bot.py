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


# ─── 보안 ───
def test_redact_secrets_in_message_and_traceback():
    import logging
    f = botmod.RedactSecrets(["SECRETKEY123", "12345678", ""])
    rec = logging.LogRecord("t", logging.ERROR, "", 0, "키 %s 계좌 %s", ("SECRETKEY123", "12345678-01"), None)
    f.filter(rec)
    assert rec.getMessage() == "키 *** 계좌 ***-01"
    try:
        raise ValueError("bad SECRETKEY123")
    except ValueError:
        import sys
        rec = logging.LogRecord("t", logging.ERROR, "", 0, "오류", None, sys.exc_info())
    f.filter(rec)
    assert "SECRETKEY123" not in rec.exc_text


def test_secure_files_closes_env_permissions(tmp_path):
    env = tmp_path / ".env"
    env.write_text("KIS_APP_KEY=x")
    env.chmod(0o644)
    old = __import__("os").umask(0o022)
    try:
        assert botmod.secure_files(env)
        assert env.stat().st_mode & 0o777 == 0o600
        assert botmod.secure_files(env) == []
    finally:
        __import__("os").umask(old)


def test_telegram_allowed_users(monkeypatch):
    tg = Telegram("T", "-100", ["11"])
    now = tg.started + 1
    updates = {"result": [
        {"update_id": 1, "message": {"date": now, "text": "/stop", "chat": {"id": -100}, "from": {"id": 99}}},
        {"update_id": 2, "message": {"date": now, "text": "/status", "chat": {"id": -100}, "from": {"id": 11}}},
    ]}
    monkeypatch.setattr(botmod.requests, "get", lambda *a, **k: type("R", (), {"json": lambda self: updates})())
    assert tg.commands() == ["/status"]
    assert botmod.security_warnings(Config({"TELEGRAM_CHAT_ID": "-100"}))
    assert not botmod.security_warnings(Config({"TELEGRAM_CHAT_ID": "-100", "TELEGRAM_ALLOWED_USERS": "11"}))
    assert not botmod.security_warnings(Config({"TELEGRAM_CHAT_ID": "12345"}))


def test_buy_over_allocation_refused(make):
    b, api, tg = make()
    with pytest.raises(KISError):
        b._execute("buy", "QQQM", 20, 100.0)  # 2000달러 > 배정 1000달러
    assert api.orders == []


# ─── 다음날 시가 청산 ───
def test_next_open_holds_overnight_and_sells_next_morning(make):
    b, api, tg = make(EXIT_MODE="next_open")
    b.step(at(9, 40))
    api.prices["QQQM"] = 102.2
    b.step(at(10, 0))
    assert b.state["positions"]["QQQM"]["qty"] == 9
    b.step(at(15, 50))  # 당일 청산 시각이 지나도 안 팖
    assert "QQQM" in b.state["positions"]
    b.step(at(16, 0))
    assert b.state["closed"] and any("들고 넘어감" in m for m in tg.sent)

    api.daily_bars = lambda *a, **k: bars() + [{"date": "20260106", "open": 105, "high": 105, "low": 104, "close": 104}]
    api.prices["QQQM"] = 104
    tue = datetime(2026, 1, 6, 9, 30, tzinfo=ET)
    b.step(tue)  # 장 초반(ENTRY_START 09:35) 전에는 대기
    assert "QQQM" in b.state["positions"]
    b.step(tue.replace(minute=36))
    assert "QQQM" not in b.state["positions"]
    sell = journal(b)[-1]
    assert sell["reason"] == "다음날 시가 청산" and float(sell["pnl"]) > 0


def test_next_open_still_stops_out_same_day(make):
    b, api, tg = make(EXIT_MODE="next_open")
    b.step(at(9, 40))
    api.prices["QQQM"] = 102.2
    b.step(at(10, 0))
    api.prices["QQQM"] = 99.5
    b.step(at(15, 50))
    assert not b.state["positions"]


def test_next_open_holiday_keeps_position(make):
    b, api, tg = make(EXIT_MODE="next_open")
    b.state = b._fresh("20260102")
    b.state["positions"] = {"QQQM": {"qty": 3, "entry": 100.0, "ex": "NAS"}}
    api.daily_bars = lambda *a, **k: bars()[:-1]  # 오늘 시세 없음 = 휴장
    b.step(at(9, 40))
    assert "QQQM" in b.state["positions"] and b.state["skip_day"]


def test_bad_exit_mode_rejected():
    with pytest.raises(ValueError):
        Config({"EXIT_MODE": "tomorrow"})


# ─── 눌림목 매수 (STRATEGY=pullback) ───
def pb_hist(today, drop=0.0, bounce=False, n=260):
    """꾸준한 상승(200일선 위) 뒤 전날 drop 비율만큼 급락(RSI↓) 또는 bounce(5일선 위 회복) + 오늘 봉"""
    closes = [50 + i * 0.5 for i in range(n)]
    if drop:
        closes[-1] = closes[-2] * (1 - drop)
    if bounce:
        closes[-3] = closes[-4] * 0.9
        closes[-1] = closes[-4] * 1.05
    bars = [{"date": f"2025{i:04d}", "open": c, "high": c, "low": c, "close": c} for i, c in enumerate(closes)]
    return bars + [{"date": today, "open": closes[-1], "high": closes[-1], "low": closes[-1], "close": closes[-1]}]


@pytest.fixture
def pb(make):
    def _pb(**env):
        b, api, tg = make(**{"STRATEGY": "pullback", "TARGETS": "AAA:NAS,BBB:NAS,CCC:NAS", "SLOTS": "2",
                             "DAILY_LOSS_LIMIT_PCT": "0", **env})
        api.prices = {"AAA": 100.0, "BBB": 100.0, "CCC": 100.0}
        api.hist = {s: pb_hist(TODAY) for s in api.prices}
        api.daily_history = lambda sym, ex, count=260: api.hist[sym]
        return b, api, tg
    return _pb


def test_pullback_fills_slots_by_lowest_rsi(pb):
    b, api, tg = pb()
    api.hist["AAA"] = pb_hist(TODAY, drop=0.06)
    api.hist["BBB"] = pb_hist(TODAY, drop=0.10)   # 더 깊이 빠짐 → RSI 더 낮음
    api.hist["CCC"] = pb_hist(TODAY, drop=0.08)
    b.step(at(9, 30))
    assert not b.state["prepared"]                 # 09:35 전에는 대기
    b.step(at(9, 40))
    plans = b.state["plans"]
    assert all(plans[s]["buy"] for s in plans) and plans["BBB"]["rsi"] < plans["CCC"]["rsi"] < plans["AAA"]["rsi"]
    assert set(b.state["positions"]) == {"BBB", "CCC"}  # 2칸, RSI 낮은 순
    assert b.state["positions"]["BBB"]["qty"] == 4      # 1000/2칸=500 → 100달러짜리 4주
    assert journal(b)[0]["reason"].startswith("눌림목 RSI")


def test_pullback_holds_overnight_then_sells_on_recovery(pb):
    b, api, tg = pb()
    api.hist["AAA"] = pb_hist(TODAY, drop=0.10)
    b.step(at(9, 40))
    b.step(at(16, 0))
    assert "AAA" in b.state["positions"] and any("보유 유지" in m for m in tg.sent)
    tue = datetime(2026, 1, 6, 9, 40, tzinfo=ET)
    api.hist["AAA"] = pb_hist("20260106", drop=0.02)  # 전날 종가가 5일선 아래 → 유지
    b.step(tue)
    assert b.state["positions"]["AAA"]["days"] == 1 and "AAA" in b.state["positions"]
    wed = datetime(2026, 1, 7, 9, 40, tzinfo=ET)
    api.hist["AAA"] = pb_hist("20260107", bounce=True)
    api.prices["AAA"] = 105
    b.step(wed)
    assert "AAA" not in b.state["positions"]
    assert journal(b)[-1]["reason"] == "5일선 회복" and float(journal(b)[-1]["pnl"]) > 0


def test_pullback_max_hold_days(pb):
    b, api, tg = pb(MAX_HOLD_DAYS="2")
    b.state = b._fresh("20260102")
    b.state["positions"] = {"AAA": {"qty": 3, "entry": 100.0, "ex": "NAS", "days": 1}}
    api.hist["AAA"] = pb_hist(TODAY, drop=0.02)    # 회복 안 됨
    b.step(at(9, 40))
    assert "AAA" not in b.state["positions"]
    assert journal(b)[0]["reason"] == "최대 보유 2일"


def test_pullback_no_buy_below_trend_or_when_paused(pb):
    b, api, tg = pb()
    down = pb_hist(TODAY, drop=0.10)
    for bar_ in down[:-2]:
        bar_["close"] = 400 - bar_["close"]           # 하락 추세 → 200일선 아래
    api.hist["AAA"] = down
    tg.queue = ["/pause"]
    api.hist["BBB"] = pb_hist(TODAY, drop=0.10)
    b.step(at(9, 40))
    assert not b.state["plans"]["AAA"]["buy"]
    assert not b.state["positions"]                  # BBB 신호 있지만 /pause


def test_pullback_holiday_does_not_count_day(pb):
    b, api, tg = pb()
    b.state = b._fresh("20260102")
    b.state["positions"] = {"AAA": {"qty": 3, "entry": 100.0, "ex": "NAS", "days": 1}}
    api.hist = {s: pb_hist(TODAY)[:-1] for s in api.prices}  # 오늘 봉 없음 = 휴장
    b.step(at(9, 40))
    assert b.state["skip_day"] and b.state["positions"]["AAA"]["days"] == 1


def test_pullback_daily_loss_uses_previous_close(pb):
    b, api, tg = pb(DAILY_LOSS_LIMIT_PCT="5")       # 한도 50달러
    b.state = b._fresh("20260102")
    b.state["positions"] = {"AAA": {"qty": 4, "entry": 150.0, "ex": "NAS", "days": 1}}
    api.hist["AAA"] = pb_hist(TODAY, drop=0.02)       # 전날 종가 약 175.4, 5일선 아래라 보유 유지
    api.prices["AAA"] = 170.0                         # 매수가보다 높지만 전날 대비 약 -22달러 → 한도 안
    b.step(at(9, 40))
    assert not b.state["halted"] and "AAA" in b.state["positions"]
    api.prices["AAA"] = 160.0                         # 전날 대비 약 -62달러 → 한도 초과
    b.step(at(9, 41))
    assert b.state["halted"] and not b.state["positions"]


def test_kis_daily_history_pages_back(tmp_path):
    api = KIS("real", "k", "s", "12345678-01", tmp_path)
    days = [f"2025{m:02d}{d:02d}" for m in range(1, 13) for d in range(1, 29)]  # 336일
    calls = []

    def daily_bars(sym, ex, base_date=""):
        calls.append(base_date)
        upto = [d for d in days if not base_date or d <= base_date]
        return [{"date": d, "open": 1, "high": 1, "low": 1, "close": 1} for d in upto[-100:]]

    api.daily_bars = daily_bars
    bars = api.daily_history("X", "NAS", 260)
    assert len(bars) >= 260 and bars == sorted(bars, key=lambda b: b["date"])
    assert len(calls) == 3 and calls[0] == ""
