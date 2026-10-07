"""
Gate.io 자동매매 봇 (현물 + USDT 무기한 선물)
- 전략: EMA 골든/데드 크로스 + RSI 필터, 손절/익절
- 외부 의존성: requests 만 사용
- 기본값 DRY_RUN=true (실제 주문 안 나감). 백테스트(gate_backtest.py)와 모의 운용을 거친 뒤 false 로 바꾸세요.

안전장치
- 손절·익절은 '실시간 가격'으로 LOOP_SEC(기본 20초)마다 확인
- 선물은 진입 직후 거래소에 손절·익절 예약 주문(가격 도달 시 자동 청산)도 걸어 둠 → 봇이 꺼져 있어도 작동
- 주문 응답이 오류여도 실제 잔고·포지션을 다시 확인해 기록을 맞춤 (주문은 됐는데 기록이 빠지는 일 방지)
- 손익은 실제 체결가와 수수료를 반영 (선물 펀딩비는 미반영)
- 로그·알림에서 API 키와 텔레그램 토큰을 가림, 같은 오류 알림은 30분에 한 번만
"""
import hashlib
import hmac
import json
import math
import os
import re
import time
import traceback
from urllib.parse import urlencode

import requests

BASE_URL = "https://api.gateio.ws"
PREFIX = "/api/v4"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
TRADES_FILE = os.path.join(HERE, "trades.jsonl")


# ───────────────────────── 설정 ─────────────────────────
def load_env(path=".env"):
    path = os.path.join(HERE, path)
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = re.split(r"\s+#", v, maxsplit=1)[0]  # 줄 끝 설명 메모 제거
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def env(key, default=None, cast=str):
    v = os.getenv(key, default)
    if v is None:
        return None
    if cast is bool:
        return str(v).lower() in ("1", "true", "yes", "y")
    return cast(v)


CFG = {
    "key": env("GATE_KEY", ""),
    "secret": env("GATE_SECRET", ""),
    "dry_run": env("DRY_RUN", "true", bool),
    "spot_symbols": [s for s in env("SPOT_SYMBOLS", "BTC_USDT,ETH_USDT").split(",") if s],
    "futures_symbols": [s for s in env("FUTURES_SYMBOLS", "BTC_USDT").split(",") if s],
    "interval": env("INTERVAL", "15m"),
    "order_usdt": env("ORDER_USDT", "20", float),          # 1회 진입 금액(USDT, 선물은 증거금)
    "leverage": min(env("LEVERAGE", "3", int), 10),         # 안전상 최대 10배로 제한
    "allow_short": env("ALLOW_SHORT", "true", bool),
    "reverse_on_signal": env("REVERSE_ON_SIGNAL", "false", bool),  # 반대 신호 때 청산 후 바로 반대 진입
    "ema_fast": env("EMA_FAST", "20", int),
    "ema_slow": env("EMA_SLOW", "50", int),
    "rsi_period": env("RSI_PERIOD", "14", int),
    "rsi_max_long": env("RSI_MAX_LONG", "70", float),
    "rsi_min_short": env("RSI_MIN_SHORT", "30", float),
    "stop_loss_pct": env("STOP_LOSS_PCT", "3", float),
    "take_profit_pct": env("TAKE_PROFIT_PCT", "6", float),
    "exchange_stops": env("EXCHANGE_STOPS", "true", bool),  # 선물: 거래소 손절·익절 예약 주문
    "spot_fee_pct": env("SPOT_FEE_PCT", "0.2", float),      # 모의·백테스트용 수수료(편도 %) — 실거래는 체결 내역 사용
    "fut_fee_pct": env("FUT_FEE_PCT", "0.05", float),
    "daily_loss_limit_usdt": env("DAILY_LOSS_LIMIT_USDT", "10", float),
    "loop_sec": env("LOOP_SEC", "20", int),
    "tg_token": env("TELEGRAM_TOKEN", ""),
    "tg_chat": env("TELEGRAM_CHAT_ID", ""),
    "report_hour": env("REPORT_HOUR", "9", int),           # 매일 이 시각(서버 시간)에 리포트, -1이면 끔
}


# ───────────────────────── 알림/로그 ─────────────────────────
def redact(text):
    """키·시크릿·텔레그램 토큰을 *** 로 (요청 오류 메시지에는 URL 이 통째로 들어감)"""
    text = str(text)
    for s in (CFG["tg_token"], CFG["key"], CFG["secret"]):
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    return re.sub(r"/bot\d+:[\w-]+", "/bot***", text)


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), redact(msg), flush=True)


def notify(msg):
    log(msg)
    if CFG["tg_token"] and CFG["tg_chat"]:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{CFG['tg_token']}/sendMessage",
                json={"chat_id": CFG["tg_chat"], "text": redact(msg)},
                timeout=10,
            )
            if r.status_code != 200:
                log(f"텔레그램 전송 거부 {r.status_code}: {r.text[:120]}")
        except Exception as e:
            log(f"텔레그램 전송 실패: {e}")


_err_seen = {}


def notify_error(e):
    """같은 오류는 30분에 한 번만 텔레그램으로 (거래소 장애 때 알림 폭탄 방지). 로그에는 매번 남김"""
    key = redact(e)[:120]
    now = time.time()
    last, skipped = _err_seen.get(key, (0, 0))
    if now - last >= 1800:
        extra = f" (30분 동안 같은 오류 {skipped}회 더 발생)" if skipped else ""
        notify(f"⚠️ 오류: {redact(e)[:300]}{extra}")
        _err_seen[key] = (now, 0)
    else:
        log(f"⚠️ 오류(알림 생략): {e}")
        _err_seen[key] = (last, skipped + 1)


def log_trade(market, symbol, action, price, pnl=None, reason=""):
    rec = {"ts": int(time.time()), "dry": CFG["dry_run"], "market": market, "symbol": symbol,
           "action": action, "price": price, "pnl": pnl, "reason": reason}
    with open(TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_trades():
    if not os.path.exists(TRADES_FILE):
        return []
    with open(TRADES_FILE, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


# ───────────────────────── Gate.io API ─────────────────────────
class Gate:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret
        self.s = requests.Session()

    def _sign(self, method, path, query, body):
        ts = str(int(time.time()))
        body_hash = hashlib.sha512((body or "").encode()).hexdigest()
        payload = f"{method}\n{PREFIX}{path}\n{query}\n{body_hash}\n{ts}"
        sign = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha512).hexdigest()
        return {"KEY": self.key, "Timestamp": ts, "SIGN": sign}

    def req(self, method, path, params=None, body=None, private=False):
        query = urlencode(params or {})
        body_str = json.dumps(body) if body is not None else ""
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if private:
            headers.update(self._sign(method, path, query, body_str))
        url = f"{BASE_URL}{PREFIX}{path}" + (f"?{query}" if query else "")
        r = self.s.request(method, url, headers=headers, data=body_str or None, timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else None

    # 현물
    def spot_candles(self, pair, interval, limit=200):
        rows = self.req("GET", "/spot/candlesticks",
                        {"currency_pair": pair, "interval": interval, "limit": limit})
        # [t, quote_vol, close, high, low, open, base_vol, closed]
        return [{"t": int(r[0]), "c": float(r[2]), "closed": (str(r[7]).lower() == "true") if len(r) > 7 else True}
                for r in rows]

    def spot_pair(self, pair):
        return self.req("GET", f"/spot/currency_pairs/{pair}")

    def spot_balance(self, currency):
        rows = self.req("GET", "/spot/accounts", {"currency": currency}, private=True)
        return float(rows[0]["available"]) if rows else 0.0

    def spot_market(self, pair, side, amount):
        return self.req("POST", "/spot/orders", body={
            "currency_pair": pair, "side": side, "type": "market",
            "amount": amount, "time_in_force": "ioc",
        }, private=True)

    def spot_last(self, pair):
        rows = self.req("GET", "/spot/tickers", {"currency_pair": pair})
        return float(rows[0]["last"])

    # 선물 (USDT 무기한)
    def fut_candles(self, contract, interval, limit=200):
        rows = self.req("GET", "/futures/usdt/candlesticks",
                        {"contract": contract, "interval": interval, "limit": limit})
        return [{"t": int(r["t"]), "c": float(r["c"]), "closed": True} for r in rows]

    def fut_contract(self, contract):
        return self.req("GET", f"/futures/usdt/contracts/{contract}")

    def fut_position(self, contract):
        try:
            return self.req("GET", f"/futures/usdt/positions/{contract}", private=True)
        except RuntimeError as e:
            if "POSITION_NOT_FOUND" in str(e):
                return {"size": 0, "entry_price": "0"}
            raise

    def fut_account(self):
        return self.req("GET", "/futures/usdt/accounts", private=True)

    def fut_last(self, contract):
        rows = self.req("GET", "/futures/usdt/tickers", {"contract": contract})
        return float(rows[0]["last"])

    def fut_set_leverage(self, contract, lev):
        return self.req("POST", f"/futures/usdt/positions/{contract}/leverage",
                        {"leverage": str(lev)}, private=True)

    def fut_market(self, contract, size, reduce_only=False):
        return self.req("POST", "/futures/usdt/orders", body={
            "contract": contract, "size": int(size), "price": "0",
            "tif": "ioc", "reduce_only": reduce_only,
        }, private=True)

    def fut_stop(self, contract, trigger_price, rule):
        """가격 도달 시 포지션 전체를 시장가로 청산하는 예약 주문 (단방향 모드). rule 1: 가격 ≥ / 2: 가격 ≤"""
        return self.req("POST", "/futures/usdt/price_orders", body={
            "initial": {"contract": contract, "size": 0, "price": "0", "close": True, "tif": "ioc"},
            "trigger": {"strategy_type": 0, "price_type": 0, "price": trigger_price, "rule": rule},
        }, private=True)

    def fut_cancel_stop(self, order_id):
        return self.req("DELETE", f"/futures/usdt/price_orders/{order_id}", private=True)


# ───────────────────────── 지표/전략 ─────────────────────────
def ema(values, n):
    k = 2 / (n + 1)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(values, n=14):
    if len(values) <= n:
        return [50.0] * len(values)
    gains, losses = [0.0], [0.0]
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[1:n + 1]) / n
    avg_l = sum(losses[1:n + 1]) / n
    out = [50.0] * (n + 1)
    for i in range(n + 1, len(values)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
        out.append(100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))
    return out


def decide(f_prev, s_prev, f_now, s_now, r, p=None):
    """교차 판단 (봇·백테스트 공용): 'long' / 'short' / None"""
    p = p or CFG
    if f_prev <= s_prev and f_now > s_now and r < p["rsi_max_long"]:
        return "long"
    if f_prev >= s_prev and f_now < s_now and r > p["rsi_min_short"]:
        return "short"
    return None


def signal(closes, p=None):
    """마감된 봉 기준: ('long' / 'short' / None, RSI)"""
    p = p or CFG
    f, s = ema(closes, p["ema_fast"]), ema(closes, p["ema_slow"])
    r = rsi(closes, p["rsi_period"])[-1]
    return decide(f[-2], s[-2], f[-1], s[-1], r, p), r


def pnl_pct(entry, price, side):
    if not entry:
        return 0.0
    p = (price - entry) / entry * 100
    return p if side == "long" else -p


def exit_reason(entry, price, side):
    """실시간 가격 기준 손절/익절 판단 → '손절' / '익절' / None"""
    p = pnl_pct(entry, price, side)
    if p <= -CFG["stop_loss_pct"]:
        return "손절"
    if p >= CFG["take_profit_pct"]:
        return "익절"
    return None


# ───────────────────────── 상태 저장 ─────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("positions", {})
        st.setdefault("last_candle", {})
        st.setdefault("day", "")
        st.setdefault("day_pnl", 0.0)
        return st
    return {"positions": {}, "last_candle": {}, "day": "", "day_pnl": 0.0}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)  # 저장 중 꺼져도 파일이 깨지지 않게


def record_pnl(st, usdt):
    today = time.strftime("%Y-%m-%d")
    if st["day"] != today:
        st["day"], st["day_pnl"] = today, 0.0
    st["day_pnl"] += usdt


def trading_halted(st):
    today = time.strftime("%Y-%m-%d")
    return st["day"] == today and st["day_pnl"] <= -CFG["daily_loss_limit_usdt"]


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ───────────────────────── 현물 ─────────────────────────
def run_spot(gate, st, pair):
    key = f"spot:{pair}"
    pos = st["positions"].get(key)

    # 보유 중이면 실시간 가격으로 손절/익절 먼저
    if pos:
        price = gate.spot_last(pair)
        why = exit_reason(pos["entry"], price, "long")
        if why:
            spot_sell(gate, st, pair, key, pos, price, why)
            return

    candles = [c for c in gate.spot_candles(pair, CFG["interval"]) if c["closed"]]
    if len(candles) < CFG["ema_slow"] + 5:
        return
    # 새 봉이 마감됐을 때만 신호 판단
    if st["last_candle"].get(key) == candles[-1]["t"]:
        return
    st["last_candle"][key] = candles[-1]["t"]
    sig, r = signal([c["c"] for c in candles])

    if sig == "long" and not pos and not trading_halted(st):
        spot_buy(gate, st, pair, key, r)
    elif sig == "short" and pos:
        spot_sell(gate, st, pair, key, pos, gate.spot_last(pair), "데드크로스")


def spot_buy(gate, st, pair, key, r):
    base = pair.split("_")[0]
    usdt = CFG["order_usdt"]
    if CFG["dry_run"]:
        price = gate.spot_last(pair)
        qty = usdt * (1 - CFG["spot_fee_pct"] / 100) / price  # 수수료는 받는 코인에서 빠짐
    else:
        if gate.spot_balance("USDT") < usdt:
            notify(f"[현물] {pair} 매수 신호지만 USDT 잔고 부족")
            return
        before = gate.spot_balance(base)
        try:
            o = gate.spot_market(pair, "buy", f"{usdt:.2f}")
        except Exception as e:
            o = None
            log(f"[현물] {pair} 매수 응답 오류 → 잔고로 체결 여부 확인: {e}")
            time.sleep(2)
        got = gate.spot_balance(base) - before  # 수수료 뺀 실제 받은 수량
        if got <= 0:
            notify(f"⚠️ [현물] {pair} 매수가 체결되지 않았습니다")
            return
        price = _f((o or {}).get("avg_deal_price")) or gate.spot_last(pair)
        usdt = _f((o or {}).get("filled_total")) or usdt
        qty = got
    st["positions"][key] = {"side": "long", "entry": price, "qty": qty, "usdt": usdt}
    log_trade("spot", pair, "buy", price)
    notify(f"{tag()}[현물 매수] {pair} @ {price:g} / {usdt:.2f} USDT (RSI {r:.0f})")


def spot_sell(gate, st, pair, key, pos, price, reason):
    base = pair.split("_")[0]
    if CFG["dry_run"]:
        proceeds = pos["qty"] * price * (1 - CFG["spot_fee_pct"] / 100)
    else:
        info = gate.spot_pair(pair)
        prec = int(info.get("amount_precision", 6))
        bal = gate.spot_balance(base)
        qty = math.floor(min(pos["qty"], bal) * 10 ** prec) / 10 ** prec
        if qty <= 0:
            notify(f"[현물] {pair} 매도할 잔고 없음 — 포지션 기록 삭제 (직접 팔았거나 거래소 주문으로 팔린 것)")
            st["positions"].pop(key, None)
            return
        try:
            o = gate.spot_market(pair, "sell", f"{qty:.{prec}f}")
        except Exception as e:
            o = None
            log(f"[현물] {pair} 매도 응답 오류 → 잔고로 체결 여부 확인: {e}")
            time.sleep(2)
        if gate.spot_balance(base) > bal - qty * 0.5:  # 잔고가 거의 그대로 → 안 팔림
            notify(f"⚠️ [현물] {pair} 매도가 체결되지 않았습니다 — 다음 확인 때 다시 시도")
            return
        price = _f((o or {}).get("avg_deal_price")) or price
        total = _f((o or {}).get("filled_total")) or qty * price
        fee = _f((o or {}).get("fee")) if (o or {}).get("fee_currency") == "USDT" else total * CFG["spot_fee_pct"] / 100
        proceeds = total - fee
    profit = proceeds - pos["usdt"]
    record_pnl(st, profit)
    st["positions"].pop(key, None)
    log_trade("spot", pair, "sell", price, profit, reason)
    notify(f"{tag()}[현물 매도·{reason}] {pair} @ {price:g} / 손익 {profit:+.2f} USDT "
           f"({profit / pos['usdt'] * 100:+.2f}%, 수수료 포함)")


# ───────────────────────── 선물 ─────────────────────────
_lev_set = set()
_contract_info = {}


def contract_info(gate, contract):
    if contract not in _contract_info:
        _contract_info[contract] = gate.fut_contract(contract)
    return _contract_info[contract]


def round_price(price, tick):
    tick = _f(tick) or 0.1
    decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return f"{round(round(price / tick) * tick, decimals):.{decimals}f}"


def place_stops(gate, contract, pos):
    """거래소에 손절·익절 예약 주문 → 주문 번호 목록 (실패해도 봇의 실시간 확인은 계속)"""
    if CFG["dry_run"] or not CFG["exchange_stops"]:
        return []
    tick = contract_info(gate, contract).get("order_price_round")
    e, sl, tp = pos["entry"], CFG["stop_loss_pct"] / 100, CFG["take_profit_pct"] / 100
    if pos["side"] == "long":
        orders = [(e * (1 - sl), 2), (e * (1 + tp), 1)]
    else:
        orders = [(e * (1 + sl), 1), (e * (1 - tp), 2)]
    ids = []
    for price, rule in orders:
        try:
            o = gate.fut_stop(contract, round_price(price, tick), rule)
            ids.append(o.get("id"))
        except Exception as e:
            notify(f"⚠️ [선물] {contract} 거래소 손절·익절 주문 실패 — 봇이 실시간으로 대신 확인합니다: {e}")
    return [i for i in ids if i]


def cancel_stops(gate, pos):
    for oid in pos.get("stops", []):
        try:
            gate.fut_cancel_stop(oid)
        except Exception as e:
            log(f"예약 주문 {oid} 취소 실패(이미 체결·취소됐을 수 있음): {e}")


def fut_fee(notional):
    return abs(notional) * CFG["fut_fee_pct"] / 100


def run_futures(gate, st, contract):
    key = f"fut:{contract}"
    pos = st["positions"].get(key)

    if pos:
        if not CFG["dry_run"] and int(gate.fut_position(contract).get("size", 0)) == 0:
            # 거래소 예약 주문(또는 직접 청산·강제 청산)으로 이미 닫힘 → 기록 정리
            price = gate.fut_last(contract)
            cancel_stops(gate, pos)
            finish_fut(st, contract, key, pos, price, "거래소 손절·익절 체결")
            pos = None
        else:
            price = gate.fut_last(contract)
            why = exit_reason(pos["entry"], price, pos["side"])
            if why:
                fut_close(gate, st, contract, key, pos, price, why)
                return

    candles = gate.fut_candles(contract, CFG["interval"])[:-1]  # 마지막(진행 중) 봉 제외
    if len(candles) < CFG["ema_slow"] + 5:
        return
    if st["last_candle"].get(key) == candles[-1]["t"]:
        return
    st["last_candle"][key] = candles[-1]["t"]
    sig, r = signal([c["c"] for c in candles])
    if not sig:
        return

    if pos and pos["side"] != sig:
        fut_close(gate, st, contract, key, pos, gate.fut_last(contract), "반대 신호")
        if not CFG["reverse_on_signal"]:
            return
        pos = None
    if pos or trading_halted(st):
        return
    if sig == "short" and not CFG["allow_short"]:
        return
    fut_open(gate, st, contract, key, sig, r)


def fut_open(gate, st, contract, key, sig, r):
    mult = float(contract_info(gate, contract)["quanto_multiplier"])
    price = gate.fut_last(contract)
    contracts = math.floor(CFG["order_usdt"] * CFG["leverage"] / (price * mult))
    if contracts < 1:
        notify(f"[선물] {contract} 주문 금액이 최소 계약 단위보다 작음 (ORDER_USDT 늘려주세요)")
        return
    size = contracts if sig == "long" else -contracts
    if not CFG["dry_run"]:
        if contract not in _lev_set:
            gate.fut_set_leverage(contract, CFG["leverage"])
            _lev_set.add(contract)
        try:
            o = gate.fut_market(contract, size)
        except Exception as e:
            o = None
            log(f"[선물] {contract} 진입 응답 오류 → 포지션으로 체결 여부 확인: {e}")
            time.sleep(2)
        live = gate.fut_position(contract)
        live_size = int(live.get("size", 0))
        if live_size == 0 or (live_size > 0) != (size > 0):
            notify(f"⚠️ [선물] {contract} 진입이 체결되지 않았습니다")
            return
        size = live_size
        price = _f((o or {}).get("fill_price")) or _f(live.get("entry_price")) or price
    pos = {"side": sig, "entry": price, "size": size, "mult": mult,
           "fee": fut_fee(size * mult * price)}
    pos["stops"] = place_stops(gate, contract, pos)
    st["positions"][key] = pos
    log_trade("futures", contract, sig, price)
    stops = " · 거래소 손절·익절 등록" if pos["stops"] else ""
    notify(f"{tag()}[선물 {'롱' if sig == 'long' else '숏'} 진입] {contract} @ {price:g} "
           f"/ {abs(size)}계약 x{CFG['leverage']} (RSI {r:.0f}){stops}")


def fut_close(gate, st, contract, key, pos, price, reason):
    if not CFG["dry_run"]:
        cancel_stops(gate, pos)
        live_size = int(gate.fut_position(contract).get("size", 0))
        if live_size != 0:
            try:
                o = gate.fut_market(contract, -live_size, reduce_only=True)
                price = _f((o or {}).get("fill_price")) or price
            except Exception as e:
                log(f"[선물] {contract} 청산 응답 오류 → 포지션 다시 확인: {e}")
                time.sleep(2)
                if int(gate.fut_position(contract).get("size", 0)) != 0:
                    notify(f"⚠️ [선물] {contract} 청산이 체결되지 않았습니다 — 다음 확인 때 다시 시도")
                    pos["stops"] = place_stops(gate, contract, pos)  # 취소했던 보호 주문 다시 걸기
                    return
    finish_fut(st, contract, key, pos, price, reason)


def finish_fut(st, contract, key, pos, price, reason):
    gross = (price - pos["entry"]) * pos["size"] * pos["mult"]
    profit = gross - pos.get("fee", 0) - fut_fee(pos["size"] * pos["mult"] * price)
    record_pnl(st, profit)
    st["positions"].pop(key, None)
    log_trade("futures", contract, "close", price, profit, reason)
    notify(f"{tag()}[선물 청산·{reason}] {contract} @ {price:g} / 손익 {profit:+.2f} USDT (수수료 포함)")


def reconcile(gate, st):
    """시작할 때 기록과 실제 거래소 상태가 다르면 알림·정리 (실거래 모드만)"""
    if CFG["dry_run"]:
        return
    for c in CFG["futures_symbols"]:
        key = f"fut:{c}"
        live = int(gate.fut_position(c).get("size", 0))
        if key not in st["positions"] and live != 0:
            notify(f"⚠️ [선물] {c} 봇 기록에 없는 포지션 {live}계약이 있습니다 — 봇은 건드리지 않아요. 직접 확인하세요")


# ───────────────────────── 리포트 ─────────────────────────
def summarize(trades):
    closed = [t for t in trades if t["pnl"] is not None]
    wins = sum(1 for t in closed if t["pnl"] > 0)
    pnl = sum(t["pnl"] for t in closed)
    rate = f"{wins / len(closed) * 100:.0f}%" if closed else "-"
    return len(trades), len(closed), wins, rate, pnl


def build_report(gate, st):
    now = time.time()
    trades = [t for t in read_trades() if t["dry"] == CFG["dry_run"]]
    today0 = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    lines = [f"📊 {tag()}Gate.io 봇 리포트  {time.strftime('%m/%d %H:%M')}", ""]

    try:
        spot = gate.spot_balance("USDT")
        fut = float(gate.fut_account().get("total", 0))
        lines.append(f"💰 잔고  현물 {spot:.2f} / 선물 {fut:.2f} USDT")
    except Exception as e:
        lines.append(f"💰 잔고 조회 실패: {redact(e)[:80]}")

    lines.append("")
    if st["positions"]:
        lines.append("📌 보유 포지션")
        for key, p in st["positions"].items():
            market, sym = key.split(":")
            try:
                px = gate.spot_last(sym) if market == "spot" else gate.fut_last(sym)
                pct = pnl_pct(p["entry"], px, p["side"])
                side = "현물" if market == "spot" else ("롱" if p["side"] == "long" else "숏")
                guard = " 🛡" if p.get("stops") else ""
                lines.append(f" · {sym} {side}  {p['entry']:g} → {px:g}  ({pct:+.2f}%){guard}")
            except Exception:
                lines.append(f" · {sym} 진입가 {p['entry']:g}")
    else:
        lines.append("📌 보유 포지션 없음")

    for label, rows in (("오늘", [t for t in trades if t["ts"] >= today0]),
                        ("최근 7일", [t for t in trades if t["ts"] >= now - 7 * 86400]),
                        ("누적", trades)):
        n, c, w, rate, pnl = summarize(rows)
        lines.append("")
        lines.append(f"📈 {label}: 거래 {n}건 · 청산 {c}건 · 승률 {rate} · 손익 {pnl:+.2f} USDT")

    recent = trades[-5:]
    if recent:
        lines += ["", "🧾 최근 거래"]
        names = {"buy": "매수", "sell": "매도", "long": "롱 진입", "short": "숏 진입", "close": "청산"}
        for t in reversed(recent):
            p = f" {t['pnl']:+.2f}" if t["pnl"] is not None else ""
            lines.append(f" · {time.strftime('%m/%d %H:%M', time.localtime(t['ts']))} "
                         f"{t['symbol']} {names.get(t['action'], t['action'])} @ {t['price']:g}{p}")

    lines += ["", "※ 손익은 수수료 포함, 선물 펀딩비 미포함"]
    if trading_halted(st):
        lines += ["", "⛔ 오늘 손실 한도 도달 — 신규 진입 중단 중"]
    return "\n".join(lines)


_tg_offset = None


def skip_old_telegram():
    """재시작 전에 쌓인 명령은 처리하지 않음 (예전 /report 가 한꺼번에 실행되는 것 방지)"""
    global _tg_offset
    if not (CFG["tg_token"] and CFG["tg_chat"]):
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{CFG['tg_token']}/getUpdates",
                         params={"offset": -1, "timeout": 0}, timeout=10).json()
        res = r.get("result", [])
        if res:
            _tg_offset = res[-1]["update_id"] + 1
    except Exception as e:
        log(f"텔레그램 초기화 실패: {e}")


def poll_telegram(gate, st):
    """텔레그램 명령 처리: /report, /status"""
    global _tg_offset
    if not (CFG["tg_token"] and CFG["tg_chat"]):
        return
    try:
        params = {"timeout": 0}
        if _tg_offset is not None:
            params["offset"] = _tg_offset
        r = requests.get(f"https://api.telegram.org/bot{CFG['tg_token']}/getUpdates",
                         params=params, timeout=10).json()
        for u in r.get("result", []):
            _tg_offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(CFG["tg_chat"]):
                continue  # 내 채팅방 명령만 처리
            text = (msg.get("text") or "").strip().lower()
            if text.startswith(("/report", "리포트", "기록")):
                notify(build_report(gate, st))
            elif text.startswith(("/status", "상태")):
                notify(f"✅ {tag()}작동 중 · 보유 {len(st['positions'])}개 · 오늘 손익 "
                       f"{st['day_pnl'] if st['day'] == time.strftime('%Y-%m-%d') else 0:+.2f} USDT")
    except Exception as e:
        log(f"텔레그램 명령 확인 실패: {e}")


def maybe_daily_report(gate, st):
    if CFG["report_hour"] < 0:
        return
    today = time.strftime("%Y-%m-%d")
    if time.localtime().tm_hour == CFG["report_hour"] and st.get("last_report") != today:
        st["last_report"] = today
        notify(build_report(gate, st))


# ───────────────────────── 메인 루프 ─────────────────────────
def tag():
    return "🧪모의 " if CFG["dry_run"] else ""


def main():
    if not CFG["dry_run"] and not (CFG["key"] and CFG["secret"]):
        raise SystemExit("실거래 모드인데 GATE_KEY / GATE_SECRET 이 비어 있습니다.")
    gate = Gate(CFG["key"], CFG["secret"])
    st = load_state()
    skip_old_telegram()
    notify(f"{tag()}봇 시작 — 현물 {CFG['spot_symbols']} / 선물 {CFG['futures_symbols']} "
           f"/ {CFG['interval']} / 레버리지 x{CFG['leverage']} / 손절 {CFG['stop_loss_pct']}%·익절 "
           f"{CFG['take_profit_pct']}% / 하루 손실 한도 {CFG['daily_loss_limit_usdt']} USDT")
    try:
        reconcile(gate, st)
    except Exception as e:
        notify_error(e)
    halted_notified = False
    while True:
        for market, syms, fn in (("spot", CFG["spot_symbols"], run_spot),
                                 ("fut", CFG["futures_symbols"], run_futures)):
            for sym in syms:
                try:  # 한 종목 오류가 다른 종목 관리(손절 등)를 막지 않게
                    fn(gate, st, sym)
                except Exception as e:
                    notify_error(f"{market}:{sym} {e}")
                    log(traceback.format_exc())
        try:
            if trading_halted(st) and not halted_notified:
                notify(f"⛔ 일일 손실 한도({CFG['daily_loss_limit_usdt']} USDT) 도달 — 오늘은 신규 진입 중단")
                halted_notified = True
            elif not trading_halted(st):
                halted_notified = False
            poll_telegram(gate, st)
            maybe_daily_report(gate, st)
        except Exception as e:
            notify_error(e)
        save_state(st)
        time.sleep(CFG["loop_sec"])


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "report":   # 지금 바로 리포트: python3 bot.py report
        notify(build_report(Gate(CFG["key"], CFG["secret"]), load_state()))
    else:
        main()
