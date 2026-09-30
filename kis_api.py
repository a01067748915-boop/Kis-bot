"""
한국투자증권 오픈API 클라이언트 — 미국 주식/ETF

- 미국 주식은 일반 시장가 주문이 없어 '현재가보다 약간 유리한 지정가'로 주문
- 시세 조회는 거래소 코드 NAS/NYS/AMS, 주문·잔고는 NASD/NYSE/AMEX 사용
- 토큰 캐싱(발급 횟수 제한 대응), 만료·무효 토큰 자동 재발급, 호출 간격 제한, 조회 재시도
- 체결 여부·체결가는 주문체결내역 조회로 확인
"""

import json
import os
import time
from pathlib import Path

import requests

BASE_URL = {
    "real": "https://openapi.koreainvestment.com:9443",
    "mock": "https://openapivts.koreainvestment.com:29443",
}

TR = {
    "balance": {"real": "TTTS3012R", "mock": "VTTS3012R"},
    "buy": {"real": "TTTT1002U", "mock": "VTTT1002U"},
    "sell": {"real": "TTTT1006U", "mock": "VTTT1001U"},
    "cancel": {"real": "TTTT1004U", "mock": "VTTT1004U"},
    "fills": {"real": "TTTS3035R", "mock": "VTTS3035R"},
}

# 토큰 만료·무효 → 재발급 후 한 번 더 시도
TOKEN_ERRORS = ("EGW00121", "EGW00123")

# 시세용 코드 → 주문/잔고용 코드
ORDER_EXCH = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}


class KISError(Exception):
    pass


def parse_targets(text):
    """'QQQM:NAS,SOXX:NAS' → {'QQQM': 'NAS', 'SOXX': 'NAS'}"""
    out = {}
    for item in text.split(","):
        item = item.strip().upper()
        if not item:
            continue
        sym, _, ex = item.partition(":")
        ex = ex or "NAS"
        if ex not in ORDER_EXCH:
            raise ValueError(f"{item}: 거래소는 NAS/NYS/AMS 중 하나")
        out[sym] = ex
    return out


class KIS:
    def __init__(self, env, app_key, app_secret, account, token_dir="."):
        if env not in BASE_URL:
            raise ValueError("KIS_ENV는 mock 또는 real")
        if not account or "-" not in account:
            raise ValueError("계좌번호는 12345678-01 형식")
        self.env = env
        self.base = BASE_URL[env]
        self.key, self.secret = app_key, app_secret
        self.cano, self.prdt = account.split("-")
        self.token_file = Path(token_dir) / f".token_{env}.json"
        self.min_interval = 0.6 if env == "mock" else 0.12
        self._last_call = 0.0
        self._cached = None  # (access_token, expires_at)

    # ─── 내부 ─────────────────────────────────────────
    def _throttle(self):
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _token(self):
        if self._cached and self._cached[1] > time.time() + 600:
            return self._cached[0]
        if self.token_file.exists():
            try:
                saved = json.loads(self.token_file.read_text())
                if saved["expires_at"] > time.time() + 600:
                    self._cached = (saved["access_token"], saved["expires_at"])
                    return saved["access_token"]
            except (ValueError, KeyError):
                pass  # 깨진 캐시 파일 → 새로 발급
        self._throttle()
        res = requests.post(
            f"{self.base}/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret},
            timeout=10,
        )
        data = res.json()
        if "access_token" not in data:
            raise KISError(f"토큰 발급 실패: {data}")
        expires_at = time.time() + int(data.get("expires_in", 86400))
        # 처음부터 본인만 읽을 수 있게 만든 뒤 기록
        fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"access_token": data["access_token"], "expires_at": expires_at}, f)
        os.chmod(self.token_file, 0o600)
        self._cached = (data["access_token"], expires_at)
        return data["access_token"]

    def _drop_token(self):
        self._cached = None
        self.token_file.unlink(missing_ok=True)

    def _request(self, method, path, tr_id, params=None, body=None, retries=3):
        last, attempt, refreshed = None, 0, False
        while attempt < retries:
            self._throttle()
            try:
                res = requests.request(
                    method, f"{self.base}{path}",
                    headers={
                        "content-type": "application/json; charset=utf-8",
                        "authorization": f"Bearer {self._token()}",
                        "appkey": self.key, "appsecret": self.secret,
                        "tr_id": tr_id, "custtype": "P",
                    },
                    params=params, json=body, timeout=10,
                )
                data = res.json()
            except (requests.RequestException, ValueError) as e:
                last = str(e)
                attempt += 1
                if attempt < retries:
                    time.sleep(attempt)
                continue
            if data.get("rt_cd") == "0":
                return data
            msg_cd = data.get("msg_cd", "")
            last = f"{msg_cd} {data.get('msg1', data)}"
            if msg_cd in TOKEN_ERRORS and not refreshed:
                # 서버가 거부한 요청이라 주문도 다시 보내도 중복되지 않음
                refreshed = True
                self._drop_token()
                continue
            if msg_cd != "EGW00201":  # 호출 한도 초과만 재시도
                break
            attempt += 1
            if attempt < retries:
                time.sleep(attempt)
        raise KISError(f"{path} 실패: {last}")

    # ─── 시세 ─────────────────────────────────────────
    def price(self, sym, ex):
        """현재가(USD)"""
        out = self._request(
            "GET", "/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
            params={"AUTH": "", "EXCD": ex, "SYMB": sym},
        ).get("output", {})
        last = out.get("last") or ""
        if not last.strip() or float(last) <= 0:
            raise KISError(f"{sym}({ex}) 현재가 없음 — 종목/거래소 코드를 확인하세요")
        return float(last)

    def daily_bars(self, sym, ex, base_date=""):
        """base_date(YYYYMMDD, 빈값=오늘)부터 과거로 일봉 최대 100개. 오래된 순으로 반환
        ※ 거래소 코드가 틀리면 오류 없이 빈 목록이 옴"""
        data = self._request(
            "GET", "/uapi/overseas-price/v1/quotations/dailyprice", "HHDFS76240000",
            params={"AUTH": "", "EXCD": ex, "SYMB": sym, "GUBN": "0", "BYMD": base_date, "MODP": "1"},
        )
        bars = []
        for b in data.get("output2", []) or []:
            try:
                if b.get("xymd"):
                    bars.append({"date": b["xymd"], "open": float(b["open"]), "high": float(b["high"]),
                                 "low": float(b["low"]), "close": float(b["clos"])})
            except (TypeError, ValueError):
                continue
        return sorted(bars, key=lambda x: x["date"])

    # ─── 계좌 ─────────────────────────────────────────
    def holdings(self, ex="NAS"):
        """해당 거래소 보유 {심볼: {"qty", "avg", "name"}}"""
        data = self._request(
            "GET", "/uapi/overseas-stock/v1/trading/inquire-balance", TR["balance"][self.env],
            params={
                "CANO": self.cano, "ACNT_PRDT_CD": self.prdt,
                "OVRS_EXCG_CD": ORDER_EXCH[ex], "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
            },
        )
        result = {}
        for h in data.get("output1", []) or []:
            qty = int(float(h.get("ovrs_cblc_qty") or 0))
            if qty > 0:
                result[h["ovrs_pdno"]] = {
                    "qty": qty, "avg": float(h.get("pchs_avg_pric") or 0), "name": h.get("ovrs_item_name", ""),
                }
        return result

    def fills(self, ex, date):
        """date(YYYYMMDD, 뉴욕 기준) 주문체결내역 → [{"odno", "side", "sym", "ord_qty", "qty", "price"}]
        side는 "buy"/"sell", qty·price는 체결수량·평균체결가"""
        mock = self.env == "mock"  # 모의투자는 전체조회만 지원
        data = self._request(
            "GET", "/uapi/overseas-stock/v1/trading/inquire-ccnl", TR["fills"][self.env],
            params={
                "CANO": self.cano, "ACNT_PRDT_CD": self.prdt,
                "PDNO": "" if mock else "%", "ORD_STRT_DT": date, "ORD_END_DT": date,
                "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00",
                "OVRS_EXCG_CD": "" if mock else ORDER_EXCH[ex], "SORT_SQN": "DS",
                "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "",
                "CTX_AREA_NK200": "", "CTX_AREA_FK200": "",
            },
        )
        out = []
        for o in data.get("output", []) or []:
            try:
                out.append({
                    "odno": str(o.get("odno", "")),
                    "side": "sell" if o.get("sll_buy_dvsn_cd") == "01" else "buy",
                    "sym": o.get("pdno", ""),
                    "ord_qty": int(float(o.get("ft_ord_qty") or 0)),
                    "qty": int(float(o.get("ft_ccld_qty") or 0)),
                    "price": float(o.get("ft_ccld_unpr3") or 0),
                })
            except (TypeError, ValueError):
                continue
        return out

    def order_fill(self, order_no, ex, date):
        """주문번호의 (체결수량, 평균체결가) — 내역에서 못 찾으면 None"""
        if not order_no:
            return None
        key = str(order_no).lstrip("0")
        for f in self.fills(ex, date):
            if f["odno"].lstrip("0") == key:
                return f["qty"], (f["price"] or None)
        return None

    # ─── 주문 ─────────────────────────────────────────
    def limit_order(self, side, sym, ex, qty, limit_price):
        """지정가 주문 → 주문번호 반환 (중복 방지를 위해 재시도 안 함)"""
        if side not in ("buy", "sell") or qty <= 0 or limit_price <= 0:
            raise ValueError("잘못된 주문")
        data = self._request(
            "POST", "/uapi/overseas-stock/v1/trading/order", TR[side][self.env],
            body={
                "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "OVRS_EXCG_CD": ORDER_EXCH[ex],
                "PDNO": sym, "ORD_QTY": str(qty), "OVRS_ORD_UNPR": f"{limit_price:.2f}",
                "CTAC_TLNO": "", "MGCO_APTM_ODNO": "", "SLL_TYPE": "00" if side == "sell" else "",
                "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
            },
            retries=1,
        )
        return (data.get("output") or {}).get("ODNO", "")

    def cancel(self, sym, ex, order_no, qty):
        """미체결 잔량 취소 (이미 전부 체결됐으면 오류가 날 수 있어 무시)"""
        if not order_no:
            return False
        try:
            self._request(
                "POST", "/uapi/overseas-stock/v1/trading/order-rvsncncl", TR["cancel"][self.env],
                body={
                    "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "OVRS_EXCG_CD": ORDER_EXCH[ex],
                    "PDNO": sym, "ORGN_ODNO": order_no, "RVSE_CNCL_DVSN_CD": "02",
                    "ORD_QTY": str(qty), "OVRS_ORD_UNPR": "0", "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0",
                },
                retries=1,
            )
            return True
        except KISError:
            return False
