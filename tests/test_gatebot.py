import importlib.util
import sys
from pathlib import Path

import pytest

GB = Path(__file__).resolve().parent.parent / "gatebot"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, GB / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gb(monkeypatch, tmp_path):
    """gatebot/bot.py 를 (저장소의 한투 bot.py 와 겹치지 않게) 따로 불러옴"""
    m = _load("gatebot_bot", "bot.py")
    monkeypatch.setattr(m, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(m, "TRADES_FILE", str(tmp_path / "trades.jsonl"))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    m.CFG.update(key="KEY123456", secret="SECRET123456", tg_token="", tg_chat="", dry_run=True,
                 stop_loss_pct=3.0, take_profit_pct=6.0, spot_fee_pct=0.2, fut_fee_pct=0.05,
                 order_usdt=20.0, leverage=3, exchange_stops=True, reverse_on_signal=False,
                 daily_loss_limit_usdt=10.0, allow_short=True, ema_fast=3, ema_slow=6, rsi_period=3,
                 rsi_max_long=101.0, rsi_min_short=-1.0, tg_alerts=True, manual_trading=False,
                 manual_max_usdt=30.0, alert_symbols=["SOL_USDT"], alert_move_1h_pct=3.0,
                 alert_move_24h_pct=8.0, alert_check_sec=300, alert_cooldown_min=120)
    sent = []
    monkeypatch.setattr(m, "notify", lambda msg: sent.append(msg))
    m.sent = sent
    m._contract_info.clear()
    m._lev_set.clear()
    m._err_seen.clear()
    return m


class FakeGate:
    def __init__(self, last=100.0, closes=None):
        self.last = last
        self.closes = closes or [100.0] * 40
        self.bal = {"USDT": 1000.0, "BTC": 0.0}
        self.size = 0
        self.calls = []
        self.fail_order = False
        self.stop_id = 0

    def spot_last(self, pair):
        return self.last

    fut_last = spot_last

    def spot_ticker(self, pair):
        return {"last": self.last, "change_24h": getattr(self, "chg24", 0.0)}

    def spot_closes(self, pair, interval, limit):
        return [getattr(self, "hour_ago", self.last)] * limit

    def spot_candles(self, pair, interval):
        return [{"t": i, "c": c, "closed": True} for i, c in enumerate(self.closes)]

    def fut_candles(self, contract, interval):
        return [{"t": i, "c": c, "closed": True} for i, c in enumerate(self.closes + [self.closes[-1]])]

    def spot_balance(self, cur):
        return self.bal.get(cur, 0.0)

    def spot_pair(self, pair):
        return {"amount_precision": 6}

    def spot_market(self, pair, side, amount):
        self.calls.append(("spot", side, amount))
        base = pair.split("_")[0]
        if side == "buy":
            self.bal[base] = self.bal.get(base, 0.0) + float(amount) / self.last * 0.998
            self.bal["USDT"] -= float(amount)
        else:
            self.bal[base] -= float(amount)
        if self.fail_order:
            raise RuntimeError("POST /spot/orders 502: timeout")
        return {"avg_deal_price": str(self.last), "filled_total": str(float(amount) if side == "buy"
                                                                       else float(amount) * self.last),
                "fee": "0", "fee_currency": "BTC"}

    def fut_contract(self, c):
        return {"quanto_multiplier": "0.0001", "order_price_round": "0.1"}

    def fut_position(self, c):
        return {"size": self.size, "entry_price": str(self.last)}

    def fut_set_leverage(self, c, lev):
        self.calls.append(("lev", lev))

    def fut_market(self, c, size, reduce_only=False):
        self.calls.append(("fut", size, reduce_only))
        self.size += size
        if self.fail_order:
            raise RuntimeError("POST /futures/usdt/orders 502")
        return {"fill_price": str(self.last)}

    def fut_stop(self, c, price, rule):
        self.stop_id += 1
        self.calls.append(("stop", price, rule))
        return {"id": self.stop_id}

    def fut_cancel_stop(self, oid):
        self.calls.append(("cancel", oid))


def test_redact_hides_token_key_and_bot_urls(gb):
    gb.CFG["tg_token"] = "123456:ABCdef_ghi"
    msg = "Max retries exceeded with url: /bot123456:ABCdef_ghi/sendMessage KEY123456"
    out = gb.redact(msg)
    assert "ABCdef" not in out and "KEY123456" not in out and "/bot***" in out
    assert gb.redact("/bot987:zzz-yy/getUpdates") == "/bot***/getUpdates"


def test_same_error_is_notified_once_per_30min(gb):
    gb.notify_error("GET /spot/tickers 503")
    gb.notify_error("GET /spot/tickers 503")
    assert len(gb.sent) == 1


def test_spot_stop_loss_uses_live_price_and_fees(gb):
    g = FakeGate(last=100.0)
    st = gb.load_state()
    gb.spot_buy(g, st, "BTC_USDT", "spot:BTC_USDT", 50)
    assert st["positions"]["spot:BTC_USDT"]["entry"] == 100.0
    g.last = 96.0  # 마감 봉은 100 그대로지만 실시간 가격이 -4%
    gb.run_spot(g, st, "BTC_USDT")
    assert "spot:BTC_USDT" not in st["positions"]
    pnl = gb.read_trades()[-1]["pnl"]
    assert pnl == pytest.approx(20 * 0.998 * 0.96 * 0.998 - 20)  # 양쪽 수수료 포함
    assert "손절" in gb.sent[-1]


def test_live_spot_buy_recovers_when_order_response_fails(gb):
    gb.CFG["dry_run"] = False
    g = FakeGate(last=100.0)
    g.fail_order = True  # 주문은 체결됐는데 응답이 오류
    st = gb.load_state()
    gb.spot_buy(g, st, "BTC_USDT", "spot:BTC_USDT", 50)
    pos = st["positions"]["spot:BTC_USDT"]
    assert pos["qty"] == pytest.approx(0.2 * 0.998) and pos["entry"] == 100.0


def test_live_futures_places_and_cancels_exchange_stops(gb):
    gb.CFG["dry_run"] = False
    g = FakeGate(last=50000.0)
    st = gb.load_state()
    gb.fut_open(g, st, "BTC_USDT", "fut:BTC_USDT", "long", 50)
    pos = st["positions"]["fut:BTC_USDT"]
    assert pos["size"] == 12  # 20 USDT × 3배 ÷ (50000 × 0.0001)
    stops = [c for c in g.calls if c[0] == "stop"]
    assert stops == [("stop", "48500.0", 2), ("stop", "53000.0", 1)]  # 손절 ≤, 익절 ≥
    gb.fut_close(g, st, "BTC_USDT", "fut:BTC_USDT", pos, 50000.0, "반대 신호")
    assert ("cancel", 1) in g.calls and ("cancel", 2) in g.calls
    assert ("fut", -12, True) in g.calls and g.size == 0
    pnl = gb.read_trades()[-1]["pnl"]
    assert pnl == pytest.approx(-2 * 12 * 0.0001 * 50000 * 0.0005)  # 본전 + 왕복 수수료


def test_short_stop_prices(gb):
    gb.CFG["dry_run"] = False
    g = FakeGate(last=50000.0)
    st = gb.load_state()
    gb.fut_open(g, st, "BTC_USDT", "fut:BTC_USDT", "short", 50)
    assert [c for c in g.calls if c[0] == "stop"] == [("stop", "51500.0", 1), ("stop", "47000.0", 2)]


def test_futures_closed_by_exchange_is_reconciled(gb):
    gb.CFG["dry_run"] = False
    g = FakeGate(last=50000.0)
    st = gb.load_state()
    gb.fut_open(g, st, "BTC_USDT", "fut:BTC_USDT", "long", 50)
    g.size = 0          # 거래소 손절 예약 주문이 체결됨
    g.last = 48500.0
    gb.run_futures(g, st, "BTC_USDT")
    assert "fut:BTC_USDT" not in st["positions"]
    assert gb.read_trades()[-1]["reason"] == "거래소 손절·익절 체결"


def test_opposite_signal_closes_without_reversing_by_default(gb):
    closes = [100.0] * 30 + [101.0, 102, 103, 104, 105, 106, 107]
    while gb.signal(closes)[0] != "short":  # 데드크로스가 마지막 봉에서 나도록 하락 봉 추가
        closes.append(closes[-1] - 1)
    g = FakeGate(last=100.0, closes=closes)
    st = gb.load_state()
    st["positions"]["fut:BTC_USDT"] = {"side": "long", "entry": 100.0, "size": 1, "mult": 1.0, "fee": 0.0}
    g.last = 99.0  # 손절선(-3%) 안쪽
    gb.run_futures(g, st, "BTC_USDT")
    assert "fut:BTC_USDT" not in st["positions"]               # 청산만 하고
    assert not any("숏 진입" in m for m in gb.sent)             # 반대로 들어가지 않음


def test_round_price(gb):
    assert gb.round_price(48499.96, "0.1") == "48500.0"
    assert gb.round_price(1.23456, "0.001") == "1.235"
    assert gb.round_price(123.4, "1") == "123"


# ─── 백테스트 ─────────────────────────────────────────
@pytest.fixture()
def bt(gb, monkeypatch):
    monkeypatch.setitem(sys.modules, "bot", gb)
    return _load("gate_backtest", "gate_backtest.py")


def bars(closes, wick=0.0):
    return [{"t": 1700000000 + i * 900, "o": c, "h": c * (1 + wick), "l": c * (1 - wick), "c": c}
            for i, c in enumerate(closes)]


def test_backtest_trend_trade_with_fees_and_take_profit(gb, bt):
    p = dict(gb.CFG)
    closes = [100.0] * 20 + [100 + i for i in range(1, 30)]
    res = bt.simulate(bars(closes), p, "spot", fee_pct=0.2, slip_pct=0.0)
    st = bt.stats(res)
    assert st["n"] >= 1 and st["takes"] >= 1
    assert st["gross"] > st["total"]  # 수수료만큼 차이
    assert res["trades"][0][2] == "long"


def test_backtest_stop_loss_when_both_hit_assumes_stop(gb, bt):
    p = dict(gb.CFG)
    closes = [100.0] * 20 + [101, 102, 103, 104, 105, 106] + [106] * 5
    b = bars(closes)
    for x in b[27:]:
        x["h"], x["l"] = x["c"] * 1.10, x["c"] * 0.90  # 한 봉에서 손절·익절 둘 다 닿음
    res = bt.simulate(b, p, "fut", fee_pct=0.0, slip_pct=0.0)
    assert res["trades"][0][5] == "손절"
    assert res["trades"][0][3] == pytest.approx(-20 * 3 * 0.03, rel=1e-6)


def test_backtest_daily_loss_limit_blocks_new_entries(gb, bt):
    p = dict(gb.CFG, daily_loss_limit_usdt=0.5)
    seq = []
    for _ in range(6):  # 같은 날 반복되는 상승 교차 → 바로 손절
        seq += [100.0] * 8 + [102, 104, 106, 90]
    b = bars(seq)
    res = bt.simulate(b, p, "spot", fee_pct=0.0, slip_pct=0.0)
    days = {}
    for t in res["trades"]:
        days.setdefault(bt.datetime.fromtimestamp(t[1]).date(), []).append(t)
    assert all(sum(x[3] for x in ts[:-1]) > -0.5 for ts in days.values())  # 한도 넘은 뒤엔 새 진입 없음


def test_backtest_fetch_pages_back_and_stops_at_limit(bt, tmp_path, monkeypatch):
    now = 1_700_000_000
    monkeypatch.setattr(bt.time, "time", lambda: now)
    monkeypatch.setattr(bt.time, "sleep", lambda s: None)

    class G:
        calls = 0

        def req(self, method, path, params):
            G.calls += 1
            if G.calls == 3:
                raise RuntimeError("GET /spot/candlesticks 400: Candlestick too long ago")
            ts = range(params["from"], params["to"] + 1, 3600)
            return [[str(t), "1", "10", "11", "9", "10", "1", "true"] for t in ts]
    got = bt.fetch(G(), "spot", "BTC_USDT", "1h", cache_dir=str(tmp_path))
    assert len(got) == 2000 and got == sorted(got, key=lambda b: b["t"])
    assert got[-1]["t"] + 3600 <= now and got[0]["o"] == 10.0 and got[0]["h"] == 11.0
    assert bt.fetch(G(), "spot", "BTC_USDT", "1h", cache_dir=str(tmp_path)) == got and G.calls == 3  # 저장분


def test_backtest_main_alts_summary(bt, monkeypatch, capsys):
    import math
    wave = [100 + 20 * math.sin(i / 15) + i * 0.05 for i in range(800)]
    monkeypatch.setattr(bt, "fetch", lambda g, m, s, iv: bars(wave, wick=0.002))
    monkeypatch.setattr(sys, "argv", ["gate_backtest.py", "--spot", "SOL_USDT,XRP_USDT", "--fut", "none",
                                      "--grid", "--brief"])
    bt.main()
    out = capsys.readouterr().out
    assert "코인 전체 요약" in out and "현물  1h" in out and " /2 |" in out.replace(" 0/2", " /2").replace(" 1/2", " /2").replace(" 2/2", " /2")
    assert "현재 설정 판정" in out and "선물 SOL" not in out


def _tg_on(gb):
    gb.CFG["tg_token"], gb.CFG["tg_chat"] = "1:x", "42"


def test_price_alert_fires_once_with_buy_hint(gb):
    _tg_on(gb)
    g = FakeGate(last=100.0)
    st = gb.load_state()
    gb.handle_command(g, st, "/alert sol 110")
    assert st["alerts"][0]["dir"] == "up" and st["alerts"][0]["pair"] == "SOL_USDT"
    st["last_move_check"] = 10 ** 12  # 급등락 확인은 이번엔 건너뜀
    gb.check_alerts(g, st)
    assert len(gb.sent) == 1  # 아직 미도달
    g.last = 111.0
    gb.check_alerts(g, st)
    assert "알림 #1" in gb.sent[-1] and "/buy sol 10" in gb.sent[-1] and st["alerts"] == []


def test_move_alert_with_cooldown(gb):
    _tg_on(gb)
    g = FakeGate(last=104.0)
    g.hour_ago = 100.0  # 1시간 +4%
    st = gb.load_state()
    gb.check_alerts(g, st)
    assert any("1시간 +4.0%" in m for m in gb.sent)
    n = len(gb.sent)
    st["last_move_check"] = 0
    gb.check_alerts(g, st)
    assert len(gb.sent) == n  # 2시간 안엔 같은 알림 안 보냄


def test_manual_buy_needs_switch_and_confirmation(gb):
    _tg_on(gb)
    g = FakeGate(last=100.0)
    st = gb.load_state()
    gb.handle_command(g, st, "/buy sol 10")
    assert "수동 매매가 꺼져" in gb.sent[-1] and not g.calls
    gb.CFG["manual_trading"] = True
    gb.handle_command(g, st, "/buy sol 50")
    assert "최대 30" in gb.sent[-1]
    gb.handle_command(g, st, "/buy sol 10")
    code = st["pending"]["code"]
    assert not g.calls  # 확인 전엔 주문 안 나감
    gb.handle_command(g, st, "/confirm 9999" if code != "9999" else "/confirm 0000")
    assert not g.calls and "pending" not in st  # 틀린 코드 → 취소
    gb.handle_command(g, st, "/buy sol 10")
    gb.handle_command(g, st, f"/confirm {st['pending']['code']}")
    assert ("spot", "buy", "10.00") in g.calls and "수동 매수" in gb.sent[-1]
    t = gb.read_trades()[-1]
    assert t["market"] == "manual" and t["dry"] is False  # 자동매매가 모의여도 실제 주문 기록


def test_manual_order_expires(gb, monkeypatch):
    _tg_on(gb)
    gb.CFG["manual_trading"] = True
    g = FakeGate(last=100.0)
    st = gb.load_state()
    gb.handle_command(g, st, "/buy sol 10")
    st["pending"]["until"] = 0
    gb.handle_command(g, st, f"/confirm {st['pending']['code']}")
    assert not g.calls and "60초" in gb.sent[-1]


def test_manual_sell_percent(gb):
    _tg_on(gb)
    gb.CFG["manual_trading"] = True
    g = FakeGate(last=100.0)
    g.bal["BTC"] = 0.2
    st = gb.load_state()
    gb.handle_command(g, st, "/sell btc 50%")
    assert st["pending"]["qty"] == pytest.approx(0.1)
    gb.handle_command(g, st, f"/confirm {st['pending']['code']}")
    assert ("spot", "sell", "0.100000") in g.calls and g.bal["BTC"] == pytest.approx(0.1)


def test_help_and_price(gb):
    _tg_on(gb)
    g = FakeGate(last=1.5)
    st = gb.load_state()
    gb.handle_command(g, st, "/price xrp")
    assert "XRP_USDT 1.5" in gb.sent[-1]
    gb.handle_command(g, st, "/help")
    assert "/alert" in gb.sent[-1]


class AnalyzeGate(FakeGate):
    def spot_bars(self, pair, interval, limit):
        if pair == "NEW_USDT":
            return [{"t": i, "o": 1, "h": 1, "l": 1, "c": 1, "qv": 1} for i in range(10)]
        n = min(limit, 300)
        return [{"t": i, "o": 100 + i * 0.5, "h": 101 + i * 0.5, "l": 99 + i * 0.5, "c": 100 + i * 0.5,
                 "qv": 2e6 if i != n - 2 else 6e6} for i in range(n)]

    def spot_tickers(self):
        return [{"pair": f"C{i}_USDT", "last": 1.0 + i, "chg": i - 10.0, "qv": 1e7 * (i + 1)} for i in range(20)] + \
               [{"pair": "TINY_USDT", "last": 1.0, "chg": 500.0, "qv": 100.0}]


def test_analyze_uptrend_report(gb):
    _tg_on(gb)
    g = AnalyzeGate()
    st = gb.load_state()
    gb.handle_command(g, st, "/analyze sol")
    out = gb.sent[-1]
    assert "SOL_USDT 분석" in out and "강한 상승 추세" in out and "RSI(14)" in out
    assert "7일 평균의 3.0배" in out and "/buy sol 10" in out and "추천이 아닙니다" in out
    gb.handle_command(g, st, "분석 new")
    assert "데이터가 부족" in gb.sent[-1]


def test_compare_and_top(gb):
    _tg_on(gb)
    g = AnalyzeGate()
    st = gb.load_state()
    gb.handle_command(g, st, "/compare sol xrp new")
    out = gb.sent[-1]
    assert "📈SOL" in out and "📈XRP" in out and "NEW" in out and "데이터 부족" in out
    gb.handle_command(g, st, "/top")
    out = gb.sent[-1]
    assert "TINY" not in out  # 거래대금 작은 코인 제외
    assert out.index("C19") < out.index("📉 하락") and "C0 " in out.split("📉 하락")[1]
