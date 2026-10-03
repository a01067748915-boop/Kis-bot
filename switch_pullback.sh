#!/bin/bash
# 눌림목 매수 전략(RSI<10, 2칸)으로 전환: bash switch_pullback.sh
#  1) 최신 코드 받기(백업 후 교체)  2) .env 백업 후 전략 설정  3) 처음 며칠은 주문 없이(DRY_RUN)
#  4) 종목 점검(실패 시 전부 되돌림)  5) 재시작 후 정상 동작 확인
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "❌ .env 가 없습니다. 봇 폴더(~/kis-bot)에서 실행하세요."; exit 1; }
[ -x venv/bin/python ] || { echo "❌ venv 가 없습니다. 먼저 bash setup.sh 를 실행하세요."; exit 1; }

BRANCH="${BRANCH:-claude/implementable-features-sq6aav}"
BASE="https://raw.githubusercontent.com/a01067748915-boop/Kis-bot/${BRANCH}"
FILES="bot.py kis_api.py signals.py check.py backtest.py strategies.py .env.example"
TARGETS_DEFAULT="SOXX:NAS,QQQM:NAS,SMH:NAS,AMD:NAS,PLTR:NAS,SOFI:NAS,IONQ:NYS,HOOD:NAS,COIN:NAS"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="backup/${STAMP}"

ask() {  # ask "질문" 기본값(y/n) — 한글 자판의 ㅛ 도 예
  local ans
  read -r -p "$1 [$( [ "$2" = y ] && echo Y/n || echo y/N )] " ans || true
  ans="${ans:-$2}"
  [[ "$ans" =~ ^[Yyㅛ] ]]
}
setenv() {  # 항목이 있으면 값만 바꾸고, 없으면 추가
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
show() {
  grep -E "^(KIS_ENV|DRY_RUN|BUDGET_USD|STRATEGY|TARGETS|SLOTS|RSI_MAX|TREND_MA|EXIT_MA|MAX_HOLD_DAYS|PULLBACK_STOP_PCT|DAILY_LOSS_LIMIT_PCT)=" .env \
    | sed 's/^/   /'
}
restore() {
  echo "↩️  이전 코드·설정으로 되돌립니다"
  for f in $FILES; do
    if [ -f "$BACKUP/$f" ]; then cp -p "$BACKUP/$f" "$f"; fi
  done
  cp -p "$BACKUP/.env" .env
  sudo systemctl restart kisbot || true
}

# ─── 1. 백업 & 최신 코드 ──────────────────────────────
mkdir -p "$BACKUP"
cp -p .env "$BACKUP/.env"
for f in $FILES; do
  if [ -f "$f" ]; then cp -p "$f" "$BACKUP/"; fi
done
echo "▶ 백업: $BACKUP"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "▶ 최신 코드 받기"
for f in $FILES; do
  curl -fsSL "$BASE/$f" -o "$TMP/$f" || { echo "❌ $f 다운로드 실패 — 아무것도 바꾸지 않았습니다"; exit 1; }
done
python3 -m py_compile "$TMP"/*.py || { echo "❌ 받은 코드가 손상됨 — 아무것도 바꾸지 않았습니다"; exit 1; }
for f in $FILES; do cp "$TMP/$f" "$f"; done

# ─── 2. 전략 설정 ─────────────────────────────────────
echo ""
echo "▶ 기존 설정"
show
setenv STRATEGY pullback
setenv TARGETS "$TARGETS_DEFAULT"   # 백테스트한 9종목 그대로
setenv SLOTS 2                      # 예산을 2칸으로(칸당 약 BUDGET/2)
setenv RSI_MAX 10
setenv TREND_MA 200
setenv EXIT_MA 5
setenv MAX_HOLD_DAYS 10
setenv PULLBACK_STOP_PCT 0          # 백테스트에서 손절은 성과를 낮춤
setenv DAILY_LOSS_LIMIT_PCT 15      # 며칠 보유라 평소 출렁임은 허용, 하루 -15%급 사고만 차단

echo ""
echo "▶ 권장: 3~5거래일은 DRY_RUN=true (텔레그램 알림만, 실제 주문 없음)"
if ask "  DRY_RUN=true 로 둘까요?" y; then
  setenv DRY_RUN true
else
  echo "   → 현재 DRY_RUN 값 유지"
fi
chmod 600 .env
echo ""
echo "▶ 적용된 설정"
show

# ─── 3. 점검 ──────────────────────────────────────────
echo ""
echo "▶ 종목 점검 (모두 ✅ 이어야 함, 시세 9종목 × 3회 조회라 30초~1분)"
CHECK="$(venv/bin/python check.py 2>&1 || true)"
echo "$CHECK"
FAILED="$(echo "$CHECK" | grep -cE "^❌ [A-Z]+\(" || true)"
if [ "$FAILED" != "0" ] || ! echo "$CHECK" | grep -q "전략: 눌림목"; then
  echo ""
  echo "❌ 점검 실패 (${FAILED}종목) — 전부 되돌립니다"
  restore
  exit 1
fi
if echo "$CHECK" | grep -q "1주도 못 삼"; then
  echo "⚠️  1주 가격이 칸 예산보다 비싼 종목은 매수 때 건너뜁니다 (봇은 계속 동작)"
fi

# ─── 4. 재시작 & 확인 ─────────────────────────────────
echo ""
echo "▶ 봇 재시작"
sudo systemctl restart kisbot
sleep 20
if systemctl is-active --quiet kisbot && [ "$(systemctl show -p NRestarts --value kisbot)" = "0" ]; then
  echo "✅ 봇 정상 실행 중 — 텔레그램 시작 알림에 '전략: 눌림목 매수 (2칸, RSI<10)' 확인"
else
  echo "❌ 봇이 정상 실행되지 않습니다:"
  journalctl -u kisbot -n 30 --no-pager || true
  restore
  exit 1
fi

cat <<MSG

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 앞으로 (권장)
 1. 매일 22:35(한국, 서머타임 기준) 이후 텔레그램 '눌림목 계획' 확인
    → 보유 종목 매도 여부, 매수 후보(RSI 낮은 순)가 옵니다. 신호는 일주일에 1번꼴
 2. /status 로 언제든 보유 일수·칸 사용 현황 확인
 3. DRY_RUN 으로 3~5거래일 지켜본 뒤 실제 주문 전환:
    sed -i 's/^DRY_RUN=.*/DRY_RUN=false/' .env && sudo systemctl restart kisbot
 4. 며칠씩 들고 가므로 실적 발표 주간에는 /pause (보유분 매도는 계속됨)
 5. 되돌리기: cp ${BACKUP}/.env .env && cp ${BACKUP}/*.py . && sudo systemctl restart kisbot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MSG
