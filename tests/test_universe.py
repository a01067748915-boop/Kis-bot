import make_universe as mu
from fundamentals import Edgar
from kis_api import parse_targets


class R:
    def __init__(self, data):
        self.status_code, self._d = 200, data

    def json(self):
        return self._d


class H:
    def get(self, url, headers, timeout):
        if "company_tickers_exchange" in url:
            return R({"fields": ["cik", "name", "ticker", "exchange"], "data": [
                [1, "Big Co", "BIGC", "Nasdaq"], [1, "Big Co", "BIGCW", "Nasdaq"],     # 워런트는 제외, 첫 티커 사용
                [2, "Mid Co", "MID", "NYSE"], [3, "Small Co", "SML", "NYSE"],
                [4, "Otc Co", "OTCX", "OTC"], [5, "Pref Co", "PRF-PA", "NYSE"],
                [6, "Amex Co", "AMX", "NYSE American"]]})
        if "Revenues" in url:
            return R({"data": [{"cik": 1, "val": 5e9, "entityName": "Big Co"},
                               {"cik": 2, "val": 2e8, "entityName": "Mid Co"},
                               {"cik": 3, "val": 5e7, "entityName": "Small Co"},
                               {"cik": 4, "val": 9e9, "entityName": "Otc Co"},
                               {"cik": 6, "val": 1.5e8, "entityName": "Amex Co"}]})
        if "RevenueFromContract" in url:
            return R({"data": [{"cik": 3, "val": 1.2e8, "entityName": "Small Co"}]})  # 다른 항목의 더 큰 값
        return R({"data": []})


def test_build_and_write_universe(tmp_path):
    e = Edgar("t t@example.com", cache=tmp_path, session=H())
    eligible, pick = mu.build(e, 2017, 1e8, count=10, seed=1)
    assert [(t, ex) for t, ex, _, _ in eligible] == [("AMX", "AMS"), ("BIGC", "NAS"), ("MID", "NYS"), ("SML", "NYS")]
    assert len(pick) == 4
    _, pick2 = mu.build(e, 2017, 1e8, count=2, seed=1)
    _, pick3 = mu.build(e, 2017, 1e8, count=2, seed=1)
    assert pick2 == pick3 and len(pick2) == 2                      # 같은 seed 면 같은 결과
    out = tmp_path / "u.txt"
    mu.write(out, eligible, pick, 2017, 1e8, 1)
    assert set(parse_targets(f"@{out}")) == {"AMX", "BIGC", "MID", "SML"}
    assert "사람이 고르지 않음" in out.read_text(encoding="utf-8")
