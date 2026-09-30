"""
한국투자증권 오픈API 클라이언트 — 미국 주식/ETF

- 미국 주식은 일반 시장가 주문이 없어 '현재가보다 약간 유리한 지정가'로 주문
- 시세 조회는 거래소 코드 NAS/NYS/AMS, 주문·잔고는 NASD/NYSE/AMEX 사용
- 토큰 캐싱(발급 횟수 제한 대응), 호출 간격 제한, 조회 재시도
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
}

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

    # ─── 내부 ─────────────────────────────────────────
    def _throttle(self):
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _token(self):
        if self.token_file.exists():
            saved = json.loads(self.token_file.read_text())
            if saved["expires_at"] > time.time() + 600:
                return saved["access_token"]
        self._throttle()
        res = requests.post(
            f"{self.base}/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret},
            timeout=10,
        )
        data = res.json()
        if "access_token" not in data:
            raise KISError(f"토큰 발급 실패: {data}")
        self.token_file.write_text(json.dumps({
            "access_token": data["access_token"],
            "expires_at": time.time() + int(data.get("expires_in", 86400)),
        }))
        os.chmod(self.token_file, 0o600)
        return data["access_token"]

    def _request(self, method, path, tr_id, params=None, body=None, retries=3):
        last = None
        for attempt in range(retries):
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
                time.sleep(1 + attempt)
                continue
            if data.get("rt_cd") == "0":
                return data
            last = f"{data.get('msg_cd', '')} {data.get('msg1', data)}"
            if "EGW00201" not in last:  # 호출 한도 초과만 재시도
                break
            time.sleep(1 + attempt)
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
