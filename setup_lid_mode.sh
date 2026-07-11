#!/bin/bash
# 뚜껑(리드)을 닫아도 표잡기가 계속 돌게 하는 1회 설정 (macOS, 관리자 비밀번호 필요).
#
# macOS에서 뚜껑 닫힘 절전을 막는 유일한 방법은 `pmset disablesleep`(root 전용)이다.
# 이 스크립트는 서버가 비밀번호 없이 pmset만 쓸 수 있는 sudoers 규칙을 설치한다.
# 서버는 '활성 잡이 있는 동안만' disablesleep을 켜고, 잡이 끝나면 자동으로 끈다
# (server.py의 lid guard) — 평소 배터리/발열에는 영향 없음.
#
# 해제: sudo rm /etc/sudoers.d/k-rail-pmset && sudo pmset -a disablesleep 0
set -e
if [ "$(uname)" != "Darwin" ]; then
  echo "macOS 전용입니다."; exit 1
fi
RULE="$(whoami) ALL=(root) NOPASSWD: /usr/bin/pmset"
echo "$RULE" | sudo tee /etc/sudoers.d/k-rail-pmset >/dev/null
sudo chmod 440 /etc/sudoers.d/k-rail-pmset
# 규칙 검증(문법 오류면 sudo 전체가 잠길 수 있으므로 visudo로 확인)
if ! sudo visudo -cf /etc/sudoers.d/k-rail-pmset >/dev/null; then
  sudo rm -f /etc/sudoers.d/k-rail-pmset
  echo "✗ sudoers 규칙 검증 실패 — 설치를 취소했습니다."; exit 1
fi
echo "✓ 설치 완료 — 잡이 도는 동안에는 뚜껑을 닫아도 맥이 잠들지 않습니다."
echo "  (잡이 없으면 평소처럼 잠듭니다. 서버 재시작 후 적용)"
echo "  ⚠ 잡 도는 중 뚜껑 닫은 채 가방에 넣으면 발열 주의"
