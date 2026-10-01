"""
미국 주식/ETF 자동매매 봇 — 전략 2가지 (STRATEGY)

STRATEGY=pullback (눌림목 매수, 며칠 보유)
  예산을 SLOTS 칸으로 나눠 TARGETS 전체를 감시
  매일 장 초반(ENTRY_START): 전날 종가 기준으로
    - 보유 종목: 종가가 5일선 위로 회복했거나 MAX_HOLD_DAYS 거래일 지났으면 매도
    - 빈 칸: 200일선 위 + RSI(2) < RSI_MAX 인 종목을 RSI 낮은 순으로 매수
  (백테스트: python strategies.py --only portfolio)

STRATEGY=breakout (변동성 돌파 단타, 기본값)
  목표가 = 오늘 시가 + K × (전일 고가 − 전일 저가)
  현재가가 목표가를 돌파하면 매수 → 손절, 또는 청산 방식(EXIT_MODE)에 따라 매도
    close     : 장 마감 전 전량 매도 (당일 청산)
    next_open : 그날은 손절만 지키고 들고 넘어가 다음 거래일 장 초반(ENTRY_START)에 매도
  (미국은 시장가 주문이 없어 현재가보다 약간 유리한 지정가로 주문, 미체결분은 취소)

시간은 모두 뉴욕 현지시간 기준이라 서머타임이 바뀌어도 자동으로 맞춰집니다.

안전장치
  - 봇이 산 수량만 매도 (같은 계좌의 기존 보유분은 건드리지 않음)
  - 종목별 배정 금액 초과 매수 불가, 하루 1회만 진입
  - 종목별 손절, 일일 손실 한도 도달 시 전량 청산 후 당일 중단
  - 목표가보다 너무 위에서는 추격 매수하지 않음
  - 텔레그램 /stop 으로 즉시 전량 청산·정지
  - 모든 매매는 trades.csv 에 누적 기록
"""

import csv
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
from signals import indicators

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
        self.exit_mode = g("EXIT_MODE", "close").strip().lower()
        if self.exit_mode not in ("close", "next_open"):
            raise ValueError("EXIT_MODE 는 close 또는 next_open")
        self.market_close = (16, 0)
        self.strategy = g("STRATEGY", "breakout").strip().lower()
        if self.strategy not in ("breakout", "pullback"):
            raise ValueError("STRATEGY 는 breakout 또는 pullback")
        self.slots = int(g("SLOTS", "2"))              # pullback: 예산을 몇 칸으로 나눌지
        self.rsi_max = float(g("RSI_MAX", "10"))       # pullback: 전날 RSI(2)가 이보다 낮으면 매수
        self.trend_ma = int(g("TREND_MA", "200"))      # pullback: 전날 종가가 이 이평선 위일 때만
        self.exit_ma = int(g("EXIT_MA", "5"))          # pullback: 종가가 이 이평선 위로 회복하면 매도
        self.max_hold_days = int(g("MAX_HOLD_DAYS", "10"))
        self.pb_stop_pct = float(g("PULLBACK_STOP_PCT", "0"))  # pullback: 0=손절 없음(백테스트 기준)
        self.poll_sec = int(g("POLL_SEC", "10"))
        self.fill_wait_sec = int(g("FILL_WAIT_SEC", "12"))
        self.tg_token = g("TELEGRAM_TOKEN", "")
        self.tg_chat = g("TELEGRAM_CHAT_ID", "")
        # 명령을 보낼 수 있는 텔레그램 사용자 ID (쉼표 구분, 비우면 채팅방 ID만 확인)
        self.tg_users = {u.strip() for u in g("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()}

    @property
    def alloc(self):
        """한 번 매수에 쓸 금액: pullback 은 칸당, breakout 은 종목당"""
        n = self.slots if self.strategy == "pullback" else len(self.targets)
        return self.budget / max(1, n)

    @property
    def daily_loss_limit(self):
        return self.budget * self.daily_loss_pct / 100


# ─── 텔레그램 ─────────────────────────────────────────
class Telegram:
    def __init__(self, token, chat_id, allowed_users=()):
        self.token, self.chat = token, str(chat_id)
        self.users = {str(u) for u in allowed_users}
        self.offset = None
        self.enabled = bool(token and chat_id)
        self.started = time.time()  # 봇 시작 전에 쌓인 명령은 무시

    def _safe(self, e):
        """오류 메시지에 URL과 함께 섞인 봇 토큰 가리기"""
        text = str(e)
        return text.replace(self.token, "***") if self.token else text

    def send(self, text):
        log.info("[알림] %s", text.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat, "text": text}, timeout=10)
        except requests.RequestException as e:
            log.warning("텔레그램 전송 실패: %s", self._safe(e))

    def commands(self):
        if not self.enabled:
            return []
        try:
            params = {"timeout": 0}
            if self.offset is not None:
                params["offset"] = self.offset
            data = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                params=params, timeout=10).json()
        except (requests.RequestException, ValueError) as e:
            log.warning("텔레그램 명령 조회 실패: %s", self._safe(e))
            return []
        cmds = []
        for u in data.get("result", []):
            self.offset = u["update_id"] + 1
            msg = u.get("message") or {}
            text = (msg.get("text") or "").strip()
            if msg.get("date", 0) < self.started:  # 재시작 전 명령(/stop 등)이 뒤늦게 실행되지 않게
                continue
            sender = str((msg.get("from") or {}).get("id", ""))
            if self.users and sender not in self.users:  # 허용한 사람만 (단체방 대비)
                continue
            if text and str(msg.get("chat", {}).get("id")) == self.chat:  # 본인 채팅만
                cmds.append(text.split()[0].split("@")[0].lower())  # /stop@봇이름 도 인식
        return cmds


# ─── 봇 ───────────────────────────────────────────────
class Bot:
    def __init__(self, cfg, api, tg, state_file=HERE / "state.json", journal_file=HERE / "trades.csv"):
        self.cfg, self.api, self.tg = cfg, api, tg
        self.state_file = Path(state_file)
        self.journal_file = Path(journal_file)
        self.state = self._load()

    @staticmethod
    def _fresh(date):
        return {"date": date, "plans": {}, "positions": {}, "traded": [], "realized": 0.0,
                "halted": False, "paused": False, "liquidate": False, "closed": False,
                "skip_day": False, "prepared": False, "trades": []}

    def _load(self):
        if self.state_file.exists():
            try:
                saved = json.loads(self.state_file.read_text())
                return {**self._fresh(saved.get("date", "")), **saved}  # 예전 형식 파일도 빠진 키 보충
            except (ValueError, AttributeError):
                log.error("state.json 이 깨져 새로 시작합니다 — 앱에서 보유 종목을 확인하세요")
        return self._fresh("")

    def _save(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))
        tmp.replace(self.state_file)

    JOURNAL_FIELDS = ["time_et", "date", "env", "dry_run", "sym", "side", "qty", "price", "pnl", "reason"]

    def _record(self, trade):
        """당일 기록(state) + 누적 기록(trades.csv)"""
        self.state["trades"].append(trade)
        self._save()
        row = {"time_et": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"), "date": self.state["date"],
               "env": self.cfg.env, "dry_run": self.cfg.dry_run, **trade}
        try:
            new = not self.journal_file.exists()
            with self.journal_file.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=self.JOURNAL_FIELDS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(row)
        except OSError as e:
            log.warning("매매 기록 저장 실패: %s", e)

    def ex(self, sym):
        return self.cfg.targets.get(sym) or self.state["positions"].get(sym, {}).get("ex", "NAS")

    # ─── 하루 준비 ────────────────────────────────────
    def prepare_day(self, now):
        if self.cfg.strategy == "pullback":
            return self.prepare_pullback(now)
        return self.prepare_breakout(now)

    def prepare_pullback(self, now):
        """전날 종가 기준 신호 계산 → 보유 종목 매도 표시, 매수 후보 저장"""
        today = now.strftime("%Y%m%d")
        cfg, s = self.cfg, self.state
        signals, open_day = {}, None
        need = cfg.trend_ma + 60  # 추세선 + RSI 안정화 여유
        for sym in sorted(set(cfg.targets) | set(s["positions"])):
            bars = self.api.daily_history(sym, self.ex(sym), need)
            if not bars:
                self.tg.send(f"⚠️ {sym}({self.ex(sym)}) 일봉이 비어 있습니다. 거래소 코드를 확인하세요 (NAS/NYS/AMS).")
                continue
            if open_day is None:
                open_day = bars[-1]["date"] == today
                if not open_day:
                    s.update(skip_day=True, prepared=True)
                    self._save()
                    self.tg.send(f"뉴욕 {today} 시세가 없어 휴장일로 판단, 오늘은 쉽니다.")
                    return
            hist = bars[:-1] if bars[-1]["date"] == today else bars  # 오늘 진행 중인 봉 제외
            last = indicators(hist, cfg.trend_ma, cfg.exit_ma).get(hist[-1]["date"]) if hist else None
            if not last:
                self.tg.send(f"⚠️ {sym} 일봉이 {len(hist)}개뿐이라 {cfg.trend_ma}일선을 못 구함 — 제외")
                continue
            signals[sym] = {"rsi": round(last["rsi"], 1), "trend": last["trend"], "recovered": last["recovered"],
                            "prev_close": hist[-1]["close"],
                            "buy": sym in cfg.targets and last["trend"] and last["rsi"] < cfg.rsi_max}
        for sym, pos in s["positions"].items():
            pos["days"] = pos.get("days", 0) + 1  # 보유 거래일
            pos.pop("exit", None)
            sig = signals.get(sym)
            if sig:
                pos["ref"] = sig["prev_close"]  # 오늘 손익 계산 기준
            if sig and sig["recovered"]:
                pos["exit"] = f"{cfg.exit_ma}일선 회복"
            elif pos["days"] >= cfg.max_hold_days:
                pos["exit"] = f"최대 보유 {cfg.max_hold_days}일"
        s["plans"] = signals
        s["prepared"] = True
        self._save()

        mode = ("모의" if cfg.env == "mock" else "실전") + (", 주문없음" if cfg.dry_run else "")
        lines = [f"[뉴욕 {today}] 눌림목 계획 ({mode})"]
        for sym, pos in s["positions"].items():
            lines.append(f"보유 {sym} {pos['days']}일째 → " + (f"매도 ({pos['exit']})" if pos.get("exit") else "유지"))
        free = cfg.slots - sum(1 for p in s["positions"].values() if not p.get("exit"))
        buys = sorted((v["rsi"], k) for k, v in signals.items() if v["buy"] and k not in s["positions"])
        if buys:
            lines.append(f"매수 후보 (빈 칸 {max(0, free)}개, RSI 낮은 순): "
                         + ", ".join(f"{k} {r}" for r, k in buys))
        else:
            near = sorted((v["rsi"], k) for k, v in signals.items() if v["trend"] and k not in s["positions"])[:3]
            lines.append("매수 신호 없음" + (" (근접: " + ", ".join(f"{k} {r}" for r, k in near) + ")" if near else ""))
        lines.append(f"칸당 {usd(cfg.alloc)} × {cfg.slots}칸, RSI<{cfg.rsi_max:g}, {cfg.trend_ma}일선 위"
                     + (f", 손절 -{cfg.pb_stop_pct:g}%" if cfg.pb_stop_pct else ""))
        self.tg.send("\n".join(lines))

    def prepare_breakout(self, now):
        """목표가 계산"""
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
        lines.append("청산: " + ("다음 거래일 장 초반" if self.cfg.exit_mode == "next_open" else "장 마감 전"))
        lines.append(f"종목당 {usd(self.cfg.alloc)}, 손절 -{self.cfg.stop_loss_pct}%, "
                     f"일일한도 -{usd(self.cfg.daily_loss_limit)}")
        self.tg.send("\n".join(lines))

    # ─── 주문 실행: 지정가 → 체결 확인 → 잔량 취소 ────
    def _execute(self, side, sym, qty, ref_price):
        """실제 체결 수량과 평균 체결가 반환"""
        slip = self.cfg.limit_slip_pct / 100
        limit = round(ref_price * (1 + slip) if side == "buy" else ref_price * (1 - slip), 2)
        if side == "buy" and qty * limit > self.cfg.alloc * 1.02:  # 설정·계산 오류로 과다 매수 방지
            raise KISError(f"주문금액 {usd(qty * limit)}이 종목당 배정액 {usd(self.cfg.alloc)} 초과")
        if self.cfg.dry_run:
            return qty, ref_price
        ex = self.ex(sym)
        before = self.api.holdings(ex).get(sym, {"qty": 0, "avg": 0.0})
        order_no = self.api.limit_order(side, sym, ex, qty, limit)
        day = datetime.now(ET).strftime("%Y%m%d")

        def check():
            """(체결수량, 평균체결가 또는 None) — 주문번호로 체결내역 조회, 안 되면 잔고 변화로 추정"""
            try:
                got = self.api.order_fill(order_no, ex, day)
            except KISError as e:
                log.warning("%s 체결내역 조회 실패, 잔고로 확인: %s", sym, e)
                got = None
            if got is not None:
                return got
            after = self.api.holdings(ex).get(sym, {"qty": 0, "avg": 0.0})
            if side == "buy":
                n = after["qty"] - before["qty"]
                avg = (after["avg"] * after["qty"] - before["avg"] * before["qty"]) / n if n > 0 else None
                return n, avg
            return before["qty"] - after["qty"], None

        waited, filled, price = 0, 0, None
        while waited < self.cfg.fill_wait_sec:
            time.sleep(3)
            waited += 3
            filled, price = check()
            if filled >= qty:
                break
        if filled < qty:
            self.api.cancel(sym, ex, order_no, qty - max(0, filled))  # 미체결 잔량만 취소
            time.sleep(2)
            filled, price = check()
        filled = max(0, min(filled, qty))
        if not price or price <= 0:
            price = limit if filled > 0 else ref_price  # 체결가를 모르면 지정가(최악 가격)로 보수적 기록
        return filled, price

    def _buy(self, sym, price, reason="목표가 돌파"):
        per_share = price * (1 + self.cfg.limit_slip_pct / 100)
        qty = math.floor(self.cfg.alloc / per_share)
        self.state["traded"].append(sym)  # 결과와 관계없이 하루 1회만
        self._save()
        if qty <= 0:
            self.tg.send(f"↪️ {sym} 1주 가격({usd(price)})이 1회 배정액({usd(self.cfg.alloc)})보다 커서 매수 불가")
            return
        try:
            filled, entry = self._execute("buy", sym, qty, price)
        except KISError as e:
            self.tg.send(f"❌ {sym} 매수 실패: {e}")
            return
        if filled <= 0:
            self.tg.send(f"↪️ {sym} 지정가 미체결로 매수 취소 (급등 구간)")
            return
        self.state["positions"][sym] = {"qty": filled, "entry": entry, "ex": self.ex(sym), "days": 0}
        self._record({"sym": sym, "side": "buy", "qty": filled, "price": round(entry, 4), "reason": reason})
        part = "" if filled == qty else f" (주문 {qty}주 중 일부)"
        target = self.state["plans"].get(sym, {}).get("target")
        self.tg.send(f"🟢 매수 {sym} {filled}주 @ {usd(entry)}{part} / "
                     + (f"목표가 {usd(target)}" if target else reason))

    def _sell(self, sym, price, reason):
        pos = self.state["positions"].get(sym)
        if not pos:
            return
        qty = pos["qty"]
        try:
            if not self.cfg.dry_run:
                held = self.api.holdings(self.ex(sym)).get(sym, {"qty": 0})["qty"]
                qty = min(qty, held)  # 봇이 산 만큼만, 실제 보유 이내로
                if qty <= 0:
                    self.state["positions"].pop(sym)
                    self._save()
                    self.tg.send(f"⚠️ {sym} 보유 수량이 없어 매도 생략 (앱에서 직접 판 경우)")
                    return
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
        self._record({"sym": sym, "side": "sell", "qty": sold, "price": round(fill, 4),
                      "pnl": round(pnl, 2), "reason": reason})
        rest = f" (잔량 {pos['qty']}주 재시도)" if pos["qty"] > 0 else ""
        self.tg.send(f"🔴 매도 {sym} {sold}주 @ {usd(fill)} ({reason}) 손익 {pnl:+,.2f}달러{rest}")

    def _pnl(self, entry, price, qty):
        return (price - entry) * qty - (entry + price) * qty * self.cfg.fee_pct / 100

    def sell_all(self, prices, reason):
        for sym in list(self.state["positions"]):
            try:  # 한 종목 오류로 나머지 청산이 막히지 않게
                price = prices.get(sym) or self.api.price(sym, self.ex(sym))
                self._sell(sym, price, reason)
            except Exception as e:
                log.exception("%s 청산 오류", sym)
                self.tg.send(f"❌ {sym} 청산 실패: {e} — 다음 확인 때 재시도")

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
                if not self.state["halted"]:  # 손실한도 청산은 /resume 으로도 풀리지 않음
                    self.state["liquidate"] = False
                self._save()
                self.tg.send("▶️ 재개" + (" (오늘은 손실한도 도달로 신규 매수 없음)" if self.state["halted"] else ""))
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
            lines.append(f"보유 {sym} {p['qty']}주 @ {usd(p['entry'])}"
                         + (f", {p.get('days', 0)}일째" if self.cfg.strategy == "pullback" else "")
                         + (f" → 매도 대기({p['exit']})" if p.get("exit") else ""))
        if self.cfg.strategy == "pullback":
            sig = sorted((v["rsi"], k) for k, v in s["plans"].items() if v["trend"])
            if sig:
                lines.append("200일선 위 RSI: " + ", ".join(f"{k} {r}" for r, k in sig[:5]))
            lines.append(f"칸 {len(s['positions'])}/{self.cfg.slots} 사용")
            return "\n".join(lines)
        for sym, p in s["plans"].items():
            mark = "완료" if sym in s["traded"] else ("대기" if p["active"] else "제외")
            lines.append(f"{sym} 목표 {usd(p['target'])} ({mark})")
        return "\n".join(lines)

    def _loss_limit_hit(self, prices):
        """오늘 손익(실현 + 오늘 기준가 대비 평가)이 한도 아래면 전량 청산·당일 중단. DAILY_LOSS_LIMIT_PCT=0 이면 끔"""
        s = self.state
        if s["halted"] or self.cfg.daily_loss_pct <= 0:
            return False
        unrealized = sum(self._pnl(p.get("ref", p["entry"]), prices[x], p["qty"])
                         for x, p in s["positions"].items() if x in prices)
        if s["realized"] + unrealized > -self.cfg.daily_loss_limit:
            return False
        s["halted"] = True
        s["liquidate"] = True
        self._save()
        self.sell_all(prices, "일일 손실한도")
        self.tg.send(f"🛑 일일 손실한도 도달 ({s['realized']:+,.2f}달러). 오늘 신규 매매 종료")
        return True

    def _step_pullback(self, t, prices):
        s, cfg = self.state, self.cfg
        # 1) 청산 모드(/stop·손실한도)면 계속 매도
        if s.get("liquidate") and s["positions"]:
            self.sell_all(prices, "청산 재시도")
        # 2) 아침에 표시된 매도(5일선 회복·최대 보유일) — 미체결이면 매 주기 재시도, 선택적 손절
        for sym, pos in list(s["positions"].items()):
            if pos.get("exit"):
                self._sell(sym, prices[sym], pos["exit"])
            elif cfg.pb_stop_pct and prices[sym] <= pos["entry"] * (1 - cfg.pb_stop_pct / 100):
                self._sell(sym, prices[sym], f"손절 -{cfg.pb_stop_pct:g}%")
        # 3) 일일 손실 한도
        if self._loss_limit_hit(prices):
            return
        # 4) 빈 칸 채우기: RSI 낮은 순
        if s["halted"] or s["paused"] or t > cfg.entry_end:
            return
        cands = sorted((v["rsi"], k) for k, v in s["plans"].items()
                       if v["buy"] and k not in s["positions"] and k not in s["traded"])
        for r, sym in cands:
            if len(s["positions"]) >= cfg.slots:
                break
            self._buy(sym, prices[sym], f"눌림목 RSI {r}")

    # ─── 한 번의 점검 주기 (now = 뉴욕 시간) ───────────
    def step(self, now):
        today = now.strftime("%Y%m%d")
        if self.state["date"] != today:
            paused = self.state.get("paused", False)
            leftover = self.state.get("positions", {})
            pullback = self.cfg.strategy == "pullback"
            if leftover and not pullback:
                if self.cfg.exit_mode == "next_open":
                    self.tg.send(f"🌅 전날 산 {list(leftover)} → 오늘 장 초반에 매도합니다.")
                else:
                    self.tg.send(f"⚠️ 전날 청산 못 한 봇 보유분 {list(leftover)} → 오늘 장 초반에 매도합니다.")
            self.state = self._fresh(today)
            self.state["paused"] = paused
            for p in leftover.values():
                if not pullback:  # 눌림목은 며칠 보유가 정상
                    p["carry"] = True
            self.state["positions"] = leftover
            self._save()

        self.handle_commands()
        s = self.state
        t = (now.hour, now.minute)
        if now.weekday() >= 5 or s["skip_day"] or s["closed"] or t < self.cfg.entry_start:
            return
        if t >= self.cfg.market_close:
            if s["positions"] and self.cfg.strategy == "pullback":
                self.tg.send(f"🌙 {list(s['positions'])} 보유 유지 → 다음 거래일 아침에 매도 여부 판단")
            elif s["positions"] and self.cfg.exit_mode == "next_open":
                self.tg.send(f"🌙 {list(s['positions'])} 들고 넘어감 → 다음 거래일 장 초반에 매도")
            elif s["positions"]:
                self.tg.send(f"⚠️ 장 마감까지 매도 못 한 보유분 {list(s['positions'])} → 다음 거래일 초반에 매도")
            s["closed"] = True
            self._save()
            self.tg.send("📊 오늘 마감\n" + self.status_text())
            return

        if not s["prepared"]:
            self.prepare_day(now)  # 오류면 다음 주기에 재시도
            if s["skip_day"]:
                return

        if self.cfg.strategy == "pullback":  # 보유 종목 + 오늘 매수 후보만 시세 조회
            syms = set(s["positions"]) | {k for k, v in s["plans"].items() if v["buy"] and k not in s["traded"]}
        else:
            syms = set(s["plans"]) | set(s["positions"])
        if not syms:
            return
        prices = {sym: self.api.price(sym, self.ex(sym)) for sym in syms}
        if self.cfg.strategy == "pullback":
            return self._step_pullback(t, prices)

        # 1) 당일 청산 모드: 장 마감 전 청산 (미체결이면 16:00까지 매 주기 재시도)
        #    다음날 시가 모드는 여기서 팔지 않고 손절만 지키다 16:00에 마감
        if t >= self.cfg.exit_time and self.cfg.exit_mode == "close":
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
                self._sell(sym, prices[sym], "다음날 시가 청산" if self.cfg.exit_mode == "next_open" else "전날 이월분 정리")
            elif prices[sym] <= pos["entry"] * (1 - self.cfg.stop_loss_pct / 100):
                self._sell(sym, prices[sym], f"손절 -{self.cfg.stop_loss_pct}%")

        # 3) 일일 손실 한도
        if self._loss_limit_hit(prices):
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


# ─── 보안 ─────────────────────────────────────────────
SECRET_KEYS = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT", "TELEGRAM_TOKEN")


class RedactSecrets(logging.Filter):
    """로그에 키·계좌번호·토큰이 섞여 나가지 않게 가림"""

    def __init__(self, secrets):
        super().__init__()
        self.secrets = sorted((x for x in secrets if x and len(x) >= 6), key=len, reverse=True)

    def redact(self, text):
        for x in self.secrets:
            text = text.replace(x, "***")
        return text

    def filter(self, record):
        msg = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        red = self.redact(msg)
        if red != msg:
            record.msg, record.args = red, None
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        return True


def secure_files(env_file=HERE / ".env"):
    """새로 만드는 파일은 본인만 읽게, .env 권한이 열려 있으면 닫고 경고 목록 반환"""
    os.umask(0o077)
    warnings = []
    if env_file.exists() and env_file.stat().st_mode & 0o077:
        try:
            os.chmod(env_file, 0o600)
            warnings.append(".env 권한이 다른 사용자에게 열려 있어 600으로 바꿨습니다")
        except OSError:
            warnings.append(".env 권한이 다른 사용자에게 열려 있습니다 — chmod 600 .env 하세요")
    return warnings


def security_warnings(cfg):
    out = []
    if cfg.tg_chat.startswith("-") and not cfg.tg_users:
        out.append("텔레그램 단체방이라 방 안의 누구나 /stop 등을 보낼 수 있습니다 — "
                   "TELEGRAM_ALLOWED_USERS 에 본인 사용자 ID를 넣으세요")
    return out


# ─── 실행 ─────────────────────────────────────────────
def setup_logging():
    handler = RotatingFileHandler(HERE / "bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    secrets = [os.environ.get(k, "") for k in SECRET_KEYS]
    secrets.append(os.environ.get("KIS_ACCOUNT", "").split("-")[0])  # 계좌번호 앞 8자리만 찍혀도 가림
    redact = RedactSecrets(secrets)
    handlers = [handler, logging.StreamHandler(sys.stdout)]
    for h in handlers:
        h.addFilter(redact)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def main():
    warnings = secure_files()  # 로그·상태 파일을 만들기 전에
    load_dotenv(HERE / ".env")
    setup_logging()
    cfg = Config()
    api = KIS(cfg.env, os.environ["KIS_APP_KEY"], os.environ["KIS_APP_SECRET"], os.environ["KIS_ACCOUNT"], HERE)
    tg = Telegram(cfg.tg_token, cfg.tg_chat, cfg.tg_users)
    bot = Bot(cfg, api, tg)
    for w in warnings + security_warnings(cfg):
        log.warning(w)
        tg.send(f"🔐 {w}")
    if cfg.strategy == "pullback":
        plan = f"전략: 눌림목 매수 ({cfg.slots}칸, RSI<{cfg.rsi_max:g})"
    else:
        plan = f"전략: 변동성 돌파 (청산: {'다음날 시가' if cfg.exit_mode == 'next_open' else '당일 장 마감 전'})"
    tg.send(f"🤖 미국 주식 봇 시작 ({'모의' if cfg.env == 'mock' else '실전'}"
            f"{', 주문없음(DRY_RUN)' if cfg.dry_run else ''})\n{plan}\n"
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
