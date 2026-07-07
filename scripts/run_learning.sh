#!/usr/bin/env bash
# run_learning.sh — Mock STB 로 UC-1 학습 세션을 돌리고 맵을 저장·요약하는 CLI 예시.
#
# 흐름: (1) learn 으로 버튼→화면 전이를 탐색·학습해 navmap.json 저장
#       (2) inspect 로 학습된 맵(states/transitions/coverage) 요약
#       (3) goal 로 목표기반 실행이 학습된 맵 위에서 되는지 확인
#
# 사용:
#   ./scripts/run_learning.sh                       # 기본: 200스텝, coverage 0.9
#   STEPS=50 COVERAGE=0.8 ./scripts/run_learning.sh # 예산 지정
#   GOAL="유튜브 켜줘" ./scripts/run_learning.sh     # 실행 목표 지정
#
# 실행권한: chmod +x scripts/run_learning.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

VENV="${VENV:-.venv}"
if [[ -f "${VENV}/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "${VENV}/bin/activate"
else
  echo "[run_learning] ${VENV} 가상환경이 없습니다. 'make install' 로 먼저 설치하세요." >&2
  export PYTHONPATH="${PYTHONPATH:-}:${REPO_ROOT}/src"
fi

# ── Mock 백엔드 강제(실 STB 없이 결정적 상태머신으로 학습) ──────
export REMOTECTL_DRIVER_BACKEND="${REMOTECTL_DRIVER_BACKEND:-mock}"
export REMOTECTL_SENSE_BACKEND="${REMOTECTL_SENSE_BACKEND:-mock}"
export REMOTECTL_MAP_STORE_PATH="${REMOTECTL_MAP_STORE_PATH:-./data/navmap.json}"

STEPS="${STEPS:-200}"
COVERAGE="${COVERAGE:-0.9}"
GOAL="${GOAL:-넷플릭스 켜줘}"

# CLI 진입점: editable 설치 시 remotectl, 아니면 python -m 로 폴백.
if command -v remotectl >/dev/null 2>&1; then
  CLI=(remotectl)
else
  CLI=(python -m remotectl.api.app)
fi

echo "======================================================================"
echo "[1/3] UC-1 학습 세션 (steps=${STEPS}, coverage_target=${COVERAGE})"
echo "======================================================================"
"${CLI[@]}" learn --steps "${STEPS}" --coverage "${COVERAGE}"

echo
echo "======================================================================"
echo "[2/3] UC-2 맵 요약 조회 (map=${REMOTECTL_MAP_STORE_PATH})"
echo "======================================================================"
"${CLI[@]}" inspect

echo
echo "======================================================================"
echo "[3/3] UC-3 목표 실행: \"${GOAL}\""
echo "======================================================================"
# goal 성공 시 종료코드 0, 실패 시 2 — 스크립트가 통째로 죽지 않도록 포착.
"${CLI[@]}" goal "${GOAL}" || echo "[run_learning] 목표 실행 실패(미학습/도달불가일 수 있음)"
