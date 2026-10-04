#!/bin/bash
# 오래 걸리는 분석(analysis.py) 진행 상황·남은 시간 대략 보기
#   bash progress.sh                      # stress_sec.log, universes/sec_auto.txt 기준
#   bash progress.sh 로그파일 종목파일
cd "$(dirname "$0")"
LOG="${1:-stress_sec.log}"
LIST="${2:-universes/sec_auto.txt}"
[ -f "$LOG" ] || { echo "❌ $LOG 없음 — 봇 폴더에서 실행했는지 확인"; exit 1; }

PID=$(pgrep -f "python[^ ]* (-u )?analysis\.py" | head -1)
if [ -z "$PID" ]; then
  if grep -q "과거 성과가 미래 수익을" "$LOG"; then
    echo "✅ 끝났습니다 → 결과 보기: tail -60 $LOG"
  else
    echo "⛔ 분석이 돌고 있지 않은데 결과도 없습니다 (중간에 꺼짐)"
    echo "   원인 확인: tail -5 $LOG   /   dmesg -T | grep -i 'out of memory' | tail -2"
  fi
  exit 0
fi

ELAPSED=$(ps -o etimes= -p "$PID" | tr -d ' ')
TOTAL=$(grep -cE '^[A-Z]' "$LIST" 2>/dev/null)
TOTAL=${TOTAL:-0}
ADJ=$(grep -c '일봉 수집 중' "$LOG")
RAW=$(grep -c '실제 가격(분할 미반영) 수집 중' "$LOG")
TRIAL=$(grep -oE '[0-9]+/[0-9]+회' "$LOG" | tail -1)

hm() { printf "%d시간 %d분" $(($1 / 3600)) $(($1 % 3600 / 60)); }
eta() {  # eta 완료수 전체수 → 지금까지 속도로 남은 시간
  [ "$1" -gt 0 ] && [ "$2" -gt "$1" ] && echo "약 $(hm $((ELAPSED * ($2 - $1) / $1))) 남음 (지금까지 속도 기준)"
}

echo "⏱  실행 시간: $(hm "$ELAPSED")   (메모리 $(ps -o rss= -p "$PID" | awk '{printf "%d", $1/1024}')MB 사용 중)"
if [ -n "$TRIAL" ]; then
  DONE=${TRIAL%%/*}; ALL=${TRIAL#*/}; ALL=${ALL%회}
  echo "4/4단계 무작위 묶음 검증: ${DONE}/${ALL}회 (25~50회마다 표시)"
  echo "   (이 단계는 남은 시간 계산 불가 — 횟수가 늘어나는 간격으로 가늠하세요)"
elif grep -qE "견고성 검증|모멘텀 변형 비교" "$LOG"; then
  echo "3/4단계 전체 종목 계산 중 — 다음 단계(무작위 검증)로 넘어가면 횟수가 표시됨"
elif grep -q "SEC 재무제표" "$LOG"; then
  SEC=$(grep -oE 'SEC [0-9]+/[0-9]+종목' "$LOG" | tail -1)
  echo "2/4단계 SEC 재무제표 받는 중 ${SEC:+— ${SEC#SEC }} (25종목마다 표시, 받은 자료는 7일간 재사용)"
elif [ "$RAW" -gt 0 ]; then
  echo "1/4단계 시세 받기 — 실제 가격: ${RAW}/${TOTAL}종목 (수정주가는 끝)"
  eta "$((ADJ + RAW))" "$((TOTAL * 2))"
else
  echo "1/4단계 시세 받기 — 수정주가: ${ADJ}/${TOTAL}종목"
  eta "$ADJ" "$((TOTAL * 2))"
fi
echo "마지막 줄: $(tail -1 "$LOG")"
