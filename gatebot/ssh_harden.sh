#!/usr/bin/env bash
# SSH 무차별 대입(Bruteforce) 대응: bash ssh_harden.sh
#  1) 점검: 실패한 로그인 수, 공격 IP 상위, '성공한' 로그인 목록(침입 여부 확인), 로그인 가능한 계정, 등록된 SSH 키
#  2) fail2ban 강화: 5번 틀리면 1시간 차단, 다시 오면 2배씩 늘려 최대 1주, 상습범은 1주 차단
#  3) (선택) 비밀번호 로그인 막고 SSH 키로만 접속 — 키가 등록돼 있을 때만
#  4) (선택) SSH 포트 변경 — 두 단계(새 포트 추가 → 접속 확인 후 22 닫기)라 갇힐 일 없음
# 중간에 묻는 질문은 Enter 만 누르면 안전한 기본값으로 진행합니다.
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "root 로 실행하세요 (sudo bash ssh_harden.sh)"; exit 1; }

ask() {  # ask "질문" 기본값(y/n)
  local ans
  read -r -p "$1 [$( [ "$2" = y ] && echo Y/n || echo y/N )] " ans || true
  ans="${ans:-$2}"
  [[ "$ans" =~ ^[Yyㅛ] ]]
}
UNIT=ssh; systemctl cat ssh.service >/dev/null 2>&1 || UNIT=sshd
logs() {  # 최근 7일 SSH 로그 (journald + auth.log)
  journalctl -u ssh -u sshd --since "-7 days" --no-pager -o short 2>/dev/null
  [ -f /var/log/auth.log ] && grep -h sshd /var/log/auth.log 2>/dev/null
}
apply_sshd() {  # 설정 검사 → 적용 (Ubuntu 24.04 의 ssh.socket 방식도 처리). 실패하면 1
  sshd -t || return 1
  if systemctl is-active --quiet ssh.socket 2>/dev/null; then
    systemctl daemon-reload && systemctl restart ssh.socket
  fi
  systemctl restart "$UNIT"
}
ports() { sshd -T 2>/dev/null | awk '$1=="port"{print $2}' | sort -un | paste -sd, -; }

echo "━━ 1. SSH 보안 점검 (최근 7일) ━━"
L="$(logs)"
echo "  로그인 실패      : $(grep -cE 'Failed password|Invalid user|authentication failure' <<<"$L") 건"
echo "  공격 IP 상위 5   :"
grep -E 'Failed password|Invalid user' <<<"$L" | grep -oE 'from [0-9a-f.:]+' | awk '{print $2}' \
  | sort | uniq -c | sort -rn | head -5 | sed 's/^/     /'
echo "  ✅ 성공한 로그인 (본인 접속이 맞는지 꼭 확인!):"
grep -E 'Accepted (password|publickey)' <<<"$L" \
  | sed -E 's/.*Accepted (password|publickey) for ([^ ]+) from ([^ ]+).*/\2 \1 \3/' \
  | sort | uniq -c | sort -rn | head -10 | awk '{printf "     %s회  계정 %s · 방식 %s · IP %s\n", $1, $2, $3, $4}'
echo "     (모르는 IP 가 있으면 침입 가능성 — 즉시 알려주세요)"
echo "  로그인 가능한 계정: $(awk -F: '$7 !~ /(nologin|false|sync|shutdown|halt)$/ {print $1}' /etc/passwd | paste -sd' ' -)"
if [ -s /root/.ssh/authorized_keys ]; then
  echo "  등록된 SSH 키    :"; ssh-keygen -lf /root/.ssh/authorized_keys 2>/dev/null | sed 's/^/     /'
else
  echo "  등록된 SSH 키    : 없음 (비밀번호로만 접속 중)"
fi
PA="$(sshd -T 2>/dev/null | awk '$1=="passwordauthentication"{print $2}')"
echo "  SSH 포트 ${UNIT}  : $(ports) / 비밀번호 로그인: ${PA:-?}"

echo
echo "━━ 2. fail2ban 강화 ━━"
if ask "  5번 틀리면 1시간 차단, 재범은 2배씩 늘려 최대 1주 차단으로 설정할까요?" y; then
  command -v fail2ban-client >/dev/null || apt-get install -y -qq fail2ban python3-systemd >/dev/null
  F=/etc/fail2ban/jail.d/zz-ssh-harden.local
  cat > "$F" <<EOF
[DEFAULT]
bantime.increment = true
bantime.factor = 2
bantime.maxtime = 1w

[sshd]
enabled = true
backend = systemd
port = $(ports)
maxretry = 5
findtime = 10m
bantime = 1h

[recidive]
enabled = true
bantime = 1w
findtime = 1d
maxretry = 3
EOF
  if fail2ban-client -t >/dev/null 2>&1 && systemctl restart fail2ban && sleep 2 && fail2ban-client status sshd >/dev/null 2>&1; then
    echo "  ✅ 적용됨 — 지금 차단 중인 IP: $(fail2ban-client status sshd | awk -F: '/Currently banned/{gsub(/ /,"",$2); print $2}')개"
  else
    rm -f "$F"; systemctl restart fail2ban
    echo "  ❌ 설정 검사 실패로 되돌렸습니다 (이전 fail2ban 설정은 그대로)"
  fi
fi

echo
echo "━━ 3. 비밀번호 로그인 차단 (SSH 키로만 접속) — 가장 확실한 방어 ━━"
if [ ! -s /root/.ssh/authorized_keys ]; then
  cat <<'EOF'
  ⏭  등록된 SSH 키가 없어 건너뜁니다. 키 만드는 법 (Termius):
     ① Termius → Keychain → + → Generate Key (종류 ED25519) → 저장
     ② 만든 키를 길게 눌러 'Export to host' → 이 서버 선택 (서버에 키가 등록됨)
     ③ 서버 설정(Host)에서 Key 를 그 키로 지정 → 새 창으로 접속되는지 확인
     ④ 이 스크립트를 다시 실행하면 비밀번호 로그인을 막을 수 있어요
  그 전까지는 root 비밀번호를 길고 복잡하게(영문·숫자·특수문자 12자 이상): passwd
EOF
elif ask "  지금 SSH 키로 접속돼 있나요? 비밀번호 로그인을 막을까요? (키 접속 확인 전엔 n)" n; then
  C=/etc/ssh/sshd_config.d/01-harden.conf
  printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\nMaxAuthTries 3\nLoginGraceTime 30\n' > "$C"
  if apply_sshd; then
    echo "  ✅ 비밀번호 로그인 차단. ⚠️ 이 창을 닫기 전에 Termius 새 창으로 접속되는지 꼭 확인!"
    echo "     안 되면 이 창에서 되돌리기: rm $C && systemctl restart $UNIT"
  else
    rm -f "$C"; echo "  ❌ 설정 검사 실패로 되돌렸습니다"
  fi
fi

echo
echo "━━ 4. SSH 포트 변경 (자동 공격 대부분이 22번만 두드림) ━━"
P=/etc/ssh/sshd_config.d/02-port.conf
if [ -f "$P" ] && [ "$(grep -c '^Port' "$P")" -ge 2 ]; then
  NEW="$(grep '^Port' "$P" | awk '$2!=22{print $2}' | head -1)"
  echo "  지금 22 와 새 포트 ${NEW} 를 함께 열어 둔 상태입니다."
  if ask "  Termius 에서 포트 ${NEW} 로 접속되는 것을 확인했나요? 22번을 닫을까요?" n; then
    printf 'Port %s\n' "$NEW" > "$P"
    if apply_sshd; then
      sed -i -E "s/^port = .*/port = ${NEW}/" /etc/fail2ban/jail.d/zz-ssh-harden.local 2>/dev/null && systemctl restart fail2ban
      command -v ufw >/dev/null && ufw status | grep -q active && ufw delete allow 22/tcp >/dev/null 2>&1
      echo "  ✅ 22번 닫음. 이제 포트 ${NEW} 로만 접속. 네이버 ACG 의 22 규칙도 지워도 됩니다"
    else
      printf 'Port 22\nPort %s\n' "$NEW" > "$P"; apply_sshd; echo "  ❌ 실패로 되돌렸습니다"
    fi
  fi
elif ask "  SSH 포트를 바꿀까요? (네이버 클라우드 콘솔 ACG 에서 새 포트를 직접 열어야 함)" n; then
  DEF=$(( (RANDOM % 30000) + 20000 ))
  read -r -p "  새 포트 번호 [${DEF}]: " NEW; NEW="${NEW:-$DEF}"
  if ! [[ "$NEW" =~ ^[0-9]+$ ]] || [ "$NEW" -lt 1024 ] || [ "$NEW" -gt 65535 ] || ss -tln | grep -q ":${NEW} "; then
    echo "  ❌ 쓸 수 없는 포트입니다 (1024~65535, 사용 중이 아닌 번호)"
  else
    printf 'Port 22\nPort %s\n' "$NEW" > "$P"   # 1단계: 22 는 그대로 두고 새 포트 추가
    if apply_sshd && sleep 1 && ss -tln | grep -q ":${NEW} "; then
      command -v ufw >/dev/null && ufw status | grep -q active && ufw allow "${NEW}/tcp" >/dev/null
      sed -i -E "s/^port = .*/port = 22,${NEW}/" /etc/fail2ban/jail.d/zz-ssh-harden.local 2>/dev/null && systemctl restart fail2ban
      cat <<EOF
  ✅ 포트 ${NEW} 추가 (22 도 아직 열려 있음). 다음 순서로:
     ① 네이버 클라우드 콘솔 → Server → ACG → 이 서버의 ACG → Inbound 규칙 추가
        프로토콜 TCP / 접근 소스 0.0.0.0/0 / 허용 포트 ${NEW}
     ② Termius 에서 이 서버의 Port 를 ${NEW} 로 바꾼 새 연결로 접속 확인
     ③ 되면 이 스크립트를 다시 실행 → 22번 닫기
EOF
    else
      rm -f "$P"; apply_sshd; echo "  ❌ 새 포트 적용 실패로 되돌렸습니다"
    fi
  fi
fi

echo
echo "━━ 결과 ━━"
echo "  SSH 포트: $(ports) · 비밀번호 로그인: $(sshd -T 2>/dev/null | awk '$1=="passwordauthentication"{print $2}')"
echo "  fail2ban: $(systemctl is-active fail2ban) · 차단 중: $(fail2ban-client status sshd 2>/dev/null | awk -F: '/Currently banned/{gsub(/ /,"",$2); print $2}')개 · 누적 차단: $(fail2ban-client status sshd 2>/dev/null | awk -F: '/Total banned/{gsub(/ /,"",$2); print $2}')개"
echo "  차단 목록 보기: fail2ban-client status sshd"
