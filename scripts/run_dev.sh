#!/usr/bin/env bash
# run_dev.sh — Mock 드라이버/센스로 로컬 개발 서버(FastAPI REST + 대시보드) 기동.
#
# 실 STB/detection-MCP 없이 학습→맵→목표실행 전 파이프라인이 완결되도록
# driver/sense 백엔드를 mock 으로 강제한다. 사내망 접속이 필요 없다.
#
# 사용:
#   ./scripts/run_dev.sh            # 기본 포트 8099
#   PORT=9000 ./scripts/run_dev.sh  # 포트 지정
#
# 실행권한: chmod +x scripts/run_dev.sh
set -euo pipefail

# 스크립트 위치 기준으로 저장소 루트로 이동(어디서 호출하든 동일 동작).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

PORT="${PORT:-8099}"
HOST="${HOST:-127.0.0.1}"
VENV="${VENV:-.venv}"

# venv 가 있으면 활성화(없으면 현재 인터프리터 사용 — CI/컨테이너 대비).
if [[ -f "${VENV}/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "${VENV}/bin/activate"
else
  echo "[run_dev] ${VENV} 가상환경이 없습니다. 'make install' 로 먼저 설치하세요." >&2
  echo "[run_dev] 임시로 시스템 인터프리터로 계속합니다(PYTHONPATH=src)." >&2
  export PYTHONPATH="${PYTHONPATH:-}:${REPO_ROOT}/src"
fi

# ── Mock 백엔드 강제(실 MCP 미접속) ─────────────────────────────
export REMOTECTL_DRIVER_BACKEND="${REMOTECTL_DRIVER_BACKEND:-mock}"
export REMOTECTL_SENSE_BACKEND="${REMOTECTL_SENSE_BACKEND:-mock}"
export REMOTECTL_MAP_STORE_PATH="${REMOTECTL_MAP_STORE_PATH:-./data/navmap.json}"

echo "[run_dev] driver=${REMOTECTL_DRIVER_BACKEND} sense=${REMOTECTL_SENSE_BACKEND}"
echo "[run_dev] map=${REMOTECTL_MAP_STORE_PATH}"
echo "[run_dev] http://${HOST}:${PORT}  (대시보드=/, REST=/health /map /goal /learn)"

# --reload 로 개발 중 코드 변경 자동 반영.
exec uvicorn remotectl.api.app:app --host "${HOST}" --port "${PORT}" --reload
