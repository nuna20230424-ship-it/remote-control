# remotectl — STB 리모컨 학습 에이전트 컨테이너 이미지
# python:3.12-slim 기반, src 레이아웃을 editable(-e) 설치하고 uvicorn 으로 기동한다.
# 로컬 개발 / 사내망 Mac mini(M4 Pro) 런타임 실행을 가정한다.
FROM python:3.12-slim

# 파이썬 런타임 위생 설정
#  - PYc 캐시 파일 미생성, 표준출력 버퍼링 해제(로그 즉시 노출)
#  - pip 캐시/버전체크 비활성으로 이미지 슬림화
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 컨테이너 기본 구현체는 Mock — 사내망 미연동 상태에서도 엔드투엔드 완결(G4/M1).
    # 실 MCP 연동 시 docker-compose 의 env 로 mcp/detection 으로 덮어쓴다.
    REMOTECTL_DRIVER_BACKEND=mock \
    REMOTECTL_SENSE_BACKEND=mock \
    REMOTECTL_MAP_STORE_PATH=/app/data/navmap.json \
    REMOTECTL_SETTLE_MS=400

WORKDIR /app

# 의존성 레이어 캐시 최적화: 먼저 메타데이터/소스만 복사해 설치.
# pyproject.toml 만으로 editable 설치가 성립하도록 소스 트리를 함께 넣는다.
COPY pyproject.toml README.md ./
COPY src/ ./src/

# editable 설치(-e): src 레이아웃을 그대로 사용, package-data(대시보드 정적자산) 포함.
# 실 배포에서는 dev extra 를 제외해 런타임 의존성만 설치한다.
RUN pip install --upgrade pip \
    && pip install -e "."

# 런타임 산출물 디렉터리(맵 JSON 등). 볼륨 마운트로 영속화 가능.
RUN mkdir -p /app/data

# FastAPI REST + 자족 대시보드 서빙 포트
EXPOSE 8099

# 컨테이너 헬스체크 — /health 엔드포인트(드라이버/센스 준비상태 근거)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=4).status==200 else 1)"

# uvicorn 기동. editable 설치라 PYTHONPATH 불필요.
# 0.0.0.0 바인딩으로 컨테이너 외부(사내망)에서 접근 가능.
CMD ["uvicorn", "remotectl.api.app:app", "--host", "0.0.0.0", "--port", "8099"]
