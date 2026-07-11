#!/bin/bash
# 서버 감시 루프 (macOS): 서버가 어떤 이유로든 죽으면 2초 후 자동 재시작한다.
# 죽기 전의 활성 잡은 ~/.k-rail-macro/jobs.json 에서 서버가 다시 뜰 때 자동
# 복원되므로, 크래시가 나도 표잡기는 계속된다.
# "K-Rail 매크로 종료" 앱이 STOP_FLAG 파일을 만들면 루프를 멈춘다.
# 사용법: run_supervised.sh [ARCH_PREFIX]   (예: "arch -arm64")
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="${K_RAIL_LOG:-/tmp/k-rail-macro.log}"
STOP_FLAG="/tmp/k-rail-macro.stop"
ARCH_PREFIX="$1"

rm -f "$STOP_FLAG"
cd "$INSTALL_DIR"
while :; do
  $ARCH_PREFIX "$INSTALL_DIR/venv/bin/python" server.py >> "$LOG" 2>&1
  [ -f "$STOP_FLAG" ] && break
  echo "[$(date '+%F %T')] 서버 종료 감지 → 2초 후 자동 재시작" >> "$LOG"
  sleep 2
done
