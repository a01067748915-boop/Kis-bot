"""
변동성 돌파 단타 봇 — 미국 주식/ETF, 당일 청산

전략
  목표가 = 오늘 시가 + K × (전일 고가 − 전일 저가)
  현재가가 목표가를 돌파하면 매수 → 손절 또는 장 마감 전 전량 매도
  (미국은 시장가 주문이 없어 현재가보다 약간 유리한 지정가로 주문, 미체결분은 취소)

시간은 모두 뉴욕 현지시간 기준이라 서머타임이 바뀌어도 자동으로 맞춰집니다.

안전장치
  - 봇이 산 수량만 매도 (같은 계좌의 기존 보유분은 건드리지 않음)
  - 종목별 배정 금액 초과 매수 불가, 하루 1회만 진입
  - 종목별 손절, 일일 손실 한도 도달 시 전량 청산 후 당일 중단
  - 목표가보다 너무 위에서는 추격 매수하지 않음
  - 텔레그램 /stop 으로 즉시 전량 청산·정지
"""

import json
import logging
import math
import os
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from kis_api import KIS, KISError, parse_targets

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).resolve().parent

log = logging.getLogger("bot")


def hm(s):
    h, m = s.split(":")
    return int(h), int(m)


def usd(x):
    return f"${x:,.2f}"


# ─── 설정 ─────────────────────────────────────────────
class Config:
    def __init__(self, env=os.environ):
        g = env.get
        self.env = g("KIS_ENV", "mock")
        self.dry_run = g("DRY_RUN", "true").lower() == "true"
        self.budget = float(g("BUDGET_USD", "950"))
        self.targets = parse_targets(g("TARGETS", "QQQM:NAS,SOXX:NAS"))
        self.k = float(g("K", "0.5"))
        self.ma_filter = int(g("MA_FILTER", "5"))
        self.stop_loss_pct = float(g("STOP_LOSS_PCT", "2"))
        self.daily_loss_pct = float(g("DAILY_LOSS_LIMIT_PCT", "3"))
        self.max_chase_pct = float(g("MAX_CHASE_PCT", "1"))
        self.limit_slip_pct = float(g("LIMIT_SLIP_PCT", "0.3"))  # 지정가를 현재가보다 얼마나 유리하게 걸지
        self.fee_pct = float(g("FEE_PCT", "0.25"))               # 편도 수수료(%) — 본인 수수료율로
        self.entry_start = hm(g("ENTRY_START", "09:35"))         # 뉴욕 시간
        self.entry_end = hm(g("ENTRY_END", "15:00"))
        self.exit_time = hm(g("EXIT_TIME", "15:45"))
        self.market_close = (16, 0)
        self.poll_sec = int(g("POLL_SEC", "10"))
        self.fill_wait_sec = int(g("FILL_WAIT_SEC", "12"))
        self.tg_token = g("TELEGRAM_TOKEN", "")
        self.tg_chat = g("TELEGRAM_CHAT_ID", "")

    @property
    def alloc(self):
        return self.budget / max(1, len(self.targets))

    @property
    def daily_loss_limit(self):
        return self.budget * self.daily_loss_pct / 100


# ─── 텔레그램 ─────────────────────────────────────────
class Telegram:
    def __init__(self, token, chat_id):
        self.token, self.chat = token, str(chat_id)
        self.offset = None
        self.enabled = bool(token and chat_id)

    def send(self, text):
        log.info("[알림] %s", text.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat, "text": text}, timeout=10)
        except requests.RequestException as e:
            log.warning("텔레그램 전송 실패: %s", e)

    def commands(self):
        if not self.enabled:
            return []
        try:
            params = {"timeout": 0}
            if self.offset is not None:
                params["offset"] = self.offset
            data = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                params=params, timeout=10).json()
        except (requests.RequestException, ValueError):
            return []
        cmds = []
        for u in data.get("result", []):
            self.offset = u["update_id"] + 1
            msg = u.get("message") or {}
            text = (msg.get("text") or "").strip()
            if text and str(msg.get("chat", {}).get("id")) == self.chat:  # 본인 채팅만
                cmds.append(text.split()[0].lower())
        return cmds


# ─── 봇 ───────────────────────────────────────────────
class Bot:
    def __init__(self, cfg, api, tg, state_file=HERE / "state.json"):
        self.cfg, self.api, self.tg = cfg, api, tg
        self.state_file = Path(state_file)
        self.state = self._load()

    @staticmethod
    def _fresh(date):
        return {"date": date, "plans": {}, "positions": {}, "traded": [], "realized": 0.0,
                "halted": False, "paused": False, "closed": False, "skip_day": False,
                "prepared": False, "trades": []}

    def _load(self):
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except ValueError:
                pass
        return self._fresh("")

    def _save(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))
        tmp.replace(self.state_file)

    def ex(self, sym):
        return self.cfg.targets.get(sym) or self.state["positions"].get(sym, {}).get("ex", "NAS")

    # ─── 하루 준비: 목표가 계산 ───────────────────────
    def prepare_day(self, now):
        today = now.strftime("%Y%m%d")
        plans = {}
        for sym, ex in self.cfg.targets.items():
            bars = self.api.daily_bars(sym, ex)
            if not bars:
                self.tg.send(f"⚠️ {sym}({ex}) 일봉이 비어 있습니다. 거래소 코드를 확인하세요 (NAS/NYS/AMS).")
                continue
            if bars[-1]["date"] != today:
                self.state.update(skip_day=True, prepared=True)
                self._save()
                self.tg.send(f"뉴욕 {today} 시세가 없어 휴장일로 판단, 오늘은 쉽니다.")
                return
            prev = bars[:-1]
            if len(prev) < max(2, self.cfg.ma_filter):
                continue
            t_open, y = bars[-1]["open"], prev[-1]
            target = t_open + self.cfg.k * (y["high"] - y["low"])
            ma, ok = None, True
            if self.cfg.ma_filter:
                ma = sum(b["close"] for b in prev[-self.cfg.ma_filter:]) / self.cfg.ma_filter
                ok = t_open > ma
            plans[sym] = {"target": round(target, 2), "open": t_open, "ma": ma, "active": ok}
        self.state["plans"] = plans
        self.state["prepared"] = True
        self._save()
        mode = ("모의" if self.cfg.env == "mock" else "실전") + (", 주문없음" if self.cfg.dry_run else "")
        lines = [f"[뉴욕 {today}] 오늘 계획 ({mode})"]
        for s, p in plans.items():
            lines.append(f"{s}: 시가 {usd(p['open'])} → 목표 {usd(p['target'])}"
                         + ("" if p["active"] else " (추세필터로 제외)"))
        lines.append(f"종목당 {usd(self.cfg.alloc)}, 손절 -{self.cfg.stop_loss_pct}%, "
                     f"일일한도 -{usd(self.cfg.daily_loss_limit)}")
        self.tg.send("\n".join(lines))

    # ─── 주문 실행: 지정가 → 체결 확인 → 잔량 취소 ────
    def _execute(self, side, sym, qty, ref_price):
        """실제 체결 수량과 평균 체결가 반환"""
        slip = self.cfg.limit_slip_pct / 100
        limit = round(ref_price * (1 + slip) if side == "buy" else ref_price * (1 - slip), 2)
        if self.cfg.dry_run:
            return qty, ref_price
        ex = self.ex(sym)
        before = self.api.holdings(ex).get(sym, {"qty": 0, "avg": 0.0})
        order_no = self.api.limit_order(side, sym, ex, qty, limit)

        def filled_now():
            after = self.api.holdings(ex).get(sym, {"qty": 0, "avg": 0.0})
            return after, (after["qty"] - before["qty"]) if side == "buy" else (before["qty"] - after["qty"])

        waited, after, filled = 0, before, 0
        while waited < self.cfg.fill_wait_sec:
            time.sleep(3)
            waited += 3
            after, filled = filled_now()
            if filled >= qty:
                break
        if filled < qty:
            self.api.cancel(sym, ex, order_no, qty)
            time.sleep(2)
            after, filled = filled_now()
        filled = max(0, min(filled, qty))
        if side == "buy" and filled > 0:
            price = (after["avg"] * after["qty"] - before["avg"] * before["qty"]) / filled
        else:
            price = ref_price
        return filled, price

    def _buy(self, sym, price):
        per_share = price * (1 + self.cfg.limit_slip_pct / 100)
        qty = math.floor(self.cfg.alloc / per_share)
        self.state["traded"].append(sym)  # 결과와 관계없이 하루 1회만
        self._save()
        if qty <= 0:
            self.tg.send(f"↪️ {sym} 1주 가격({usd(price)})이 종목당 배정액({usd(self.cfg.alloc)})보다 커서 매수 불가")
            return
        try:
            filled, entry = self._execute("buy", sym, qty, price)
        except KISError as e:
            self.tg.send(f"❌ {sym} 매수 실패: {e}")
            return
        if filled <= 0:
            self.tg.send(f"↪️ {sym} 지정가 미체결로 매수 취소 (급등 구간)")
            return
        self.state["positions"][sym] = {"qty": filled, "entry": entry, "ex": self.ex(sym)}
        self.state["trades"].append({"sym": sym, "side": "buy", "qty": filled, "price": entry})
        self._save()
        part = "" if filled == qty else f" (주문 {qty}주 중 일부)"
        self.tg.send(f"🟢 매수 {sym} {filled}주 @ {usd(entry)}{part} / 목표가 {usd(self.state['plans'][sym]['target'])}")

    def _sell(self, sym, price, reason):
        pos = self.state["positions"].get(sym)
        if not pos:
            return
        qty = pos["qty"]
        if not self.cfg.dry_run:
            held = self.api.holdings(self.ex(sym)).get(sym, {"qty": 0})["qty"]
            qty = min(qty, held)  # 봇이 산 만큼만, 실제 보유 이내로
            if qty <= 0:
                self.state["positions"].pop(sym)
                self._save()
                self.tg.send(f"⚠️ {sym} 보유 수량이 없어 매도 생략 (앱에서 직접 판 경우)")
                return
        try:
            sold, fill = self._execute("sell", sym, qty, price)
        except KISError as e:
            self.tg.send(f"❌ {sym} 매도 실패: {e} — 다음 확인 때 재시도")
            return
        if sold <= 0:
            log.info("%s 매도 미체결, 다음 주기 재시도", sym)
            return
        pnl = self._pnl(pos["entry"], fill, sold)
        self.state["realized"] += pnl
        pos["qty"] -= sold
        if pos["qty"] <= 0:
            self.state["positions"].pop(sym)
        self.state["trades"].append({"sym": sym, "side": "sell", "qty": sold, "price": fill, "pnl": pnl})
        self._save()
        rest = f" (잔량 {pos['qty']}주 재시도)" if pos["qty"] > 0 else ""
        self.tg.send(f"🔴 매도 {sym} {sold}주 @ ~{usd(fill)} ({reason}) 손익 {pnl:+,.2f}달러{rest}")

    def _pnl(self, entry, price, qty):
        return (price - entry) * qty - (entry + price) * qty * self.cfg.fee_pct / 100

    def sell_all(self, prices, reason):
        for sym in list(self.state["positions"]):
            price = prices.get(sym) or self.api.price(sym, self.ex(sym))
            self._sell(sym, price, reason)

    # ─── 텔레그램 명령 ────────────────────────────────
    def handle_commands(self):
        for cmd in self.tg.commands():
            if cmd == "/stop":
                self.state["paused"] = True
                self.state["liquidate"] = True
                self._save()
                self.sell_all({}, "긴급정지")
                self.tg.send("⛔ 정지: 봇 보유분 매도, 신규 매수 중단. /resume 로 재개")
            elif cmd == "/pause":
                self.state["paused"] = True
                self._save()
                self.tg.send("⏸ 신규 매수 중단 (보유분 손절·청산은 계속). /resume 로 재개")
            elif cmd == "/resume":
                self.state["paused"] = False
                self.state["liquidate"] = False
                self._save()
                self.tg.send("▶️ 재개")
            elif cmd == "/status":
                self.tg.send(self.status_text())
            elif cmd in ("/help", "/start"):
                self.tg.send("/status 현황\n/pause 신규매수 중단\n/stop 전량매도+정지\n/resume 재개")

    def status_text(self):
        s = self.state
        now_kst = datetime.now(KST).strftime("%m/%d %H:%M")
        lines = [f"[뉴욕 {s['date']} / 한국 {now_kst}] 실현손익 {s['realized']:+,.2f}달러"
                 + (" | 정지중" if s["paused"] else "") + (" | 한도도달" if s["halted"] else "")]
        for sym, p in s["positions"].items():
            lines.append(f"보유 {sym} {p['qty']}주 @ {usd(p['entry'])}")
        for sym, p in s["plans"].items():
            mark = "완료" if sym in s["traded"] else ("대기" if p["active"] else "제외")
            lines.append(f"{sym} 목표 {usd(p['target'])} ({mark})")
        return "\n".join(lines)

    # ─── 한 번의 점검 주기 (now = 뉴욕 시간) ───────────
    def step(self, now):
        today = now.strftime("%Y%m%d")
        if self.state["date"] != today:
            paused = self.state.get("paused", False)
            leftover = self.state.get("positions", {})
            if leftover:
                self.tg.send(f"⚠️ 전날 청산 못 한 봇 보유분 {list(leftover)} → 오늘 장 초반에 매도합니다.")
            self.state = self._fresh(today)
            self.state["paused"] = paused
            for p in leftover.values():
                p["carry"] = True
            self.state["positions"] = leftover
            self._save()

        self.handle_commands()
        s = self.state
        t = (now.hour, now.minute)
        if now.weekday() >= 5 or s["skip_day"] or s["closed"] or t < self.cfg.entry_start:
            return
        if t >= self.cfg.market_close:
            if s["positions"]:
                self.tg.send(f"⚠️ 장 마감까지 매도 못 한 보유분 {list(s['positions'])} → 다음 거래일 초반에 매도")
            s["closed"] = True
            self._save()
            self.tg.send("📊 오늘 마감\n" + self.status_text())
            return

        if not s["prepared"]:
            self.prepare_day(now)  # 오류면 다음 주기에 재시도
            if s["skip_day"]:
                return

        syms = set(s["plans"]) | set(s["positions"])
        if not syms:
            return
        prices = {sym: self.api.price(sym, self.ex(sym)) for sym in syms}

        # 1) 장 마감 전 청산 (미체결이면 16:00까지 매 주기 재시도)
        if t >= self.cfg.exit_time:
            if s["positions"]:
                self.sell_all(prices, "장마감 청산")
            if not self.state["positions"]:
                s["closed"] = True
                self._save()
                self.tg.send("📊 오늘 마감\n" + self.status_text())
            return

        # 2) 청산 모드(/stop·손실한도)면 남은 보유분 계속 매도, 아니면 이월분 정리 + 손절
        if s.get("liquidate") and s["positions"]:
            self.sell_all(prices, "청산 재시도")
        for sym, pos in list(s["positions"].items()):
            if pos.get("carry"):
                self._sell(sym, prices[sym], "전날 이월분 정리")
            elif prices[sym] <= pos["entry"] * (1 - self.cfg.stop_loss_pct / 100):
                self._sell(sym, prices[sym], f"손절 -{self.cfg.stop_loss_pct}%")

        # 3) 일일 손실 한도
        unrealized = sum(self._pnl(p["entry"], prices[x], p["qty"]) for x, p in s["positions"].items())
        if not s["halted"] and s["realized"] + unrealized <= -self.cfg.daily_loss_limit:
            s["halted"] = True
            s["liquidate"] = True
            self._save()
            self.sell_all(prices, "일일 손실한도")
            self.tg.send(f"🛑 일일 손실한도 도달 ({s['realized']:+,.2f}달러). 오늘 신규 매매 종료")
            return

        # 4) 진입
        if s["halted"] or s["paused"] or t > self.cfg.entry_end:
            return
        for sym, plan in s["plans"].items():
            if not plan["active"] or sym in s["traded"] or sym in s["positions"]:
                continue
            price = prices[sym]
            if price >= plan["target"]:
                if price > plan["target"] * (1 + self.cfg.max_chase_pct / 100):
                    s["traded"].append(sym)
                    self._save()
                    self.tg.send(f"↪️ {sym} 목표가보다 {self.cfg.max_chase_pct}% 넘게 올라 추격 안 함")
                    continue
                self._buy(sym, price)


# ─── 실행 ─────────────────────────────────────────────
def setup_logging():
    handler = RotatingFileHandler(HERE / "bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[handler, logging.StreamHandler(sys.stdout)])


def main():
    load_dotenv(HERE / ".env")
    setup_logging()
    cfg = Config()
    api = KIS(cfg.env, os.environ["KIS_APP_KEY"], os.environ["KIS_APP_SECRET"], os.environ["KIS_ACCOUNT"], HERE)
    tg = Telegram(cfg.tg_token, cfg.tg_chat)
    bot = Bot(cfg, api, tg)
    tg.send(f"🤖 미국 단타 봇 시작 ({'모의' if cfg.env == 'mock' else '실전'}"
            f"{', 주문없음(DRY_RUN)' if cfg.dry_run else ''})\n"
            f"종목 {', '.join(cfg.targets)} / 예산 {usd(cfg.budget)}")

    errors = 0
    while True:
        now = datetime.now(ET)
        try:
            bot.step(now)
            errors = 0
        except Exception as e:  # 네트워크 장애 등으로 봇이 죽지 않게
            errors += 1
            log.exception("오류")
            if errors in (1, 10, 50):
                tg.send(f"⚠️ 오류 {errors}회 연속: {e}")
        in_session = now.weekday() < 5 and (9, 20) <= (now.hour, now.minute) <= (16, 5)
        time.sleep(cfg.poll_sec if in_session else 60)


if __name__ == "__main__":
    main()
