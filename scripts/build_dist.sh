#!/usr/bin/env bash
# remotectl 배포물(wheel + sdist) 생성 — git 없이 설치·사용할 파일을 만든다.
# 사용: bash scripts/build_dist.sh  → dist/remotectl-*.whl, dist/remotectl-*.tar.gz
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[build_dist] python: $(python3 --version)"
python3 -m pip install --quiet --upgrade build
echo "[build_dist] building wheel + sdist ..."
python3 -m build

echo "[build_dist] done:"
ls -1 dist/*.whl dist/*.tar.gz 2>/dev/null || true
echo
echo "이 dist/*.whl 파일을 팀에 전달하면 git 없이 'pip install <whl>' 로 사용 가능합니다."
echo "설치/사용: docs/DISTRIBUTION.md 참고."
