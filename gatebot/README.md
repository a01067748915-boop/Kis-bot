# Gate.io 자동매매 봇

현물 + USDT 무기한 선물을 함께 운용합니다. 외부 라이브러리는 `requests` 하나만 씁니다.

## 설치·업데이트 (한 줄)
```bash
curl -fsSL https://raw.githubusercontent.com/a01067748915-boop/Kis-bot/claude/implementable-features-sq6aav/gatebot/install_gatebot.sh -o install_gatebot.sh && bash install_gatebot.sh
```
- 처음이면 API 키·시크릿·텔레그램을 묻고, 이미 설치돼 있으면 `.env`·기록을 그대로 두고 코드만 바꿉니다
- 봇은 `/opt/gatebot` 에서 전용 계정 `gatebot` 으로 실행 (root 아님). `~/gatebot` 은 바로가기라 예전 명령도 그대로 됩니다
- 끝에 API 키 연결 확인과 보안 상태 요약이 나옵니다

## 전략
- **진입**: 마감된 봉에서 EMA20이 EMA50을 상향 돌파 → 현물 매수 / 선물 롱, 하향 돌파 → 선물 숏
- **RSI 필터**: RSI 70 이상이면 매수 안 함, 30 이하면 숏 안 함
- **청산**: 손절 3% / 익절 6% (가격 기준) 또는 반대 신호 (`REVERSE_ON_SIGNAL=true` 면 반대로 바로 재진입)

## 안전장치
| 항목 | 내용 |
|---|---|
| 손절·익절 | 실시간 가격으로 20초마다 확인 (`LOOP_SEC`) |
| 거래소 예약 주문 | 선물은 진입 직후 거래소에 손절·익절 주문 → 봇이 꺼져도 작동 (`EXCHANGE_STOPS`) |
| 주문 오류 | 응답이 실패해도 실제 잔고·포지션을 다시 조회해 기록을 맞춤 |
| 손익 | 실제 체결가·수수료 반영 (선물 펀딩비는 미반영) |
| 하루 손실 한도 | 기본 10 USDT 넘으면 그날 신규 진입 중단 |
| 비밀 정보 | 로그·텔레그램에서 키·토큰을 `***` 로 가림, 같은 오류 알림은 30분에 한 번 |
| 서비스 | 전용 계정, 봇 폴더 외 쓰기 금지, 권한 상승 금지 등 systemd 격리 |

## 백테스트 (실거래 전에 꼭)
```bash
cd /opt/gatebot && sudo -u gatebot python3 gate_backtest.py --grid
```
- `.env` 설정 그대로, 봇과 같은 신호 함수로 계산. 수수료·슬리피지 차감, 손절·익절은 봉의 고가·저가로 판정
- Gate.io 가 짧은 봉은 과거를 적게 줘서 15분봉은 약 3개월, 4시간봉은 약 4년치
- 마지막 '현재 설정 판정'에서 ✅ 가 아니면 실거래로 바꾸지 마세요

## 자주 쓰는 명령
| 할 일 | 명령어 |
|---|---|
| 로그 보기 | `journalctl -u gatebot -f` |
| 설정 바꾸기 | `nano ~/gatebot/.env` → `sudo systemctl restart gatebot` |
| 지금 리포트 | `cd /opt/gatebot && sudo -u gatebot python3 bot.py report` |
| 멈추기 | `sudo systemctl stop gatebot` |

텔레그램: `/report`(리포트), `/status`(상태). 매일 `REPORT_HOUR` 시에 리포트 자동 전송.

## API 키 권한
- Spot·Perpetual Futures 만 Read-Write, **출금(Withdrawal)·이체는 끄기**
- IP 화이트리스트에 서버 공인 IP 만 (설치 끝에 표시됨)
- 선물은 **단방향(One-way) 모드**여야 함 (기본값), 선물 계정으로 USDT 이체 필요

## 주의
- 봇은 자기가 연 포지션만 관리해요. 같은 종목을 손으로 거래하면 꼬일 수 있습니다
- 백테스트 결과도 미래 수익을 보장하지 않습니다. 잃어도 괜찮은 금액으로만 운용하세요
