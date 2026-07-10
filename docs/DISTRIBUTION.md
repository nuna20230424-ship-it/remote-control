# remotectl 배포 — git 없이 사용하기

git 저장소 접근 없이 **배포된 파일만으로** 에이전트를 설치·실행하는 방법. 팀원에게는 아래 3개만
전달하면 된다(Confluence 첨부/사내 공유드라이브 등):

1. `remotectl-0.1.0-py3-none-any.whl` — 오프라인 설치용 패키지(대시보드 자산 포함)
2. `docs/CONFLUENCE.html` (또는 `.md`) — 현황/사용 개요
3. 이 `DISTRIBUTION.md`

> wheel 은 git 에 커밋하지 않는다(빌드 산출물). 아래 §3 으로 언제든 재생성해 배포한다.

---

## 1. 요구 사항

- Python **3.12+** (`python3 --version`)
- (실 STB 연동 시에만) 사내망 접근 + `ffmpeg`(capture 프레임 추출용). **mock 실행에는 불필요.**

## 2. 설치 & 실행 (mock — 실 하드웨어 불필요)

```bash
# 1) 가상환경 권장
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2) 배포된 wheel 설치 (인터넷 없이도 됨 — FastAPI 등 의존은 최초 1회 다운로드)
pip install remotectl-0.1.0-py3-none-any.whl

# 3-a) 대시보드 + REST 서버
remotectl serve                      # http://127.0.0.1:8099
#     학습 실행 → 맵 시각화 → 목표 입력/실행 을 브라우저에서

# 3-b) 또는 CLI 로 바로
remotectl learn                      # 탐색 학습(mock)
remotectl goal "넷플릭스 켜줘"        # 목표 실행
remotectl autolearn --target 0.9     # 목표 커버리지까지 자동 반복
remotectl keyscreen                  # 키이벤트↔화면 매핑 DB 조회
```

기본값이 mock 백엔드라 **사내망/실 STB 없이** 학습→맵→목표실행→자동반복 전 구간이 동작한다.
대시보드 정적 자산은 wheel 에 포함되어 별도 파일이 필요 없다.

## 3. wheel 재생성 (배포 담당자)

```bash
# 저장소에서 (담당자 1회)
pip install build
python -m build            # → dist/remotectl-0.1.0-py3-none-any.whl (+ .tar.gz)
#   또는
bash scripts/build_dist.sh
```

생성된 `dist/*.whl` 을 팀에 전달(Confluence 첨부/공유드라이브).

## 4. 실 STB 연동 전환 (env 만 채우면 코드 변경 0)

`.env` 를 만들거나 환경변수로:

```dotenv
REMOTECTL_DRIVER_BACKEND=mcp
REMOTECTL_SENSE_BACKEND=detection
IR_MCP_URL=http://172.16.3.x:8002
REMOTECTL_IR_CODESET=ref_remote       # 대상 단말 코드셋(GET /codesets 로 확인)
CAPTURE_MCP_URL=http://172.16.3.x:8001
DETECTION_MCP_URL=http://172.16.3.136:8103
```

실 연동 전제: 사내망 도달성 · `ffmpeg` 설치 · capture `file_path` 공유 볼륨 · 대상 코드셋 일치.

## 5. 다른 AI 에이전트가 도구로 사용 (MCP)

```bash
pip install "remotectl-0.1.0-py3-none-any.whl[mcp]"   # mcp 추가 의존
python -m remotectl.mcp_server                         # stdio MCP 서버
```

오케스트레이터 MCP 설정에 등록하면 `remote_learn / remote_run_goal / remote_autolearn /
remote_inspect_map / remote_find_path / remote_keyscreen` 6개 도구가 노출된다.

## 6. Docker (선택)

이미지로 배포하려면 저장소의 `Dockerfile` / `docker-compose.yml` 사용. 이미지를 사내 레지스트리에
올리면 팀원은 git 없이 `docker compose up` 만으로 서버를 띄울 수 있다.

## 7. 문제 해결

| 증상 | 원인/조치 |
|---|---|
| `remotectl: command not found` | venv 활성화 확인, 또는 `python -m remotectl.api.app <cmd>` |
| 대시보드가 비어 보임 | 정적 자산은 wheel 포함 — `pip show -f remotectl` 로 `api/static/*` 확인 |
| 실 연동 시 `DriverUnavailableError` | `IR_MCP_URL`/사내망 도달성 확인 |
| capture 실패 | `ffmpeg` 설치 및 `CAPTURE_MCP_URL`·공유 볼륨 확인 |
| detection 도달 불가 | `DETECTION_MCP_URL`(172.16.3.136:8103) 도달성 확인 |
