# STB 리모컨 학습 에이전트 (remotectl) — 진행 현황

> 팀 공유용 상태 문서. 기획 · 설계 · 구현 · 운영을 한 곳에 정리. (Confluence 붙여넣기/HTML import 겸용)

| | |
|---|---|
| **제품** | 셋탑박스를 리모컨으로 조작하며 버튼→화면 전이를 학습 → 네비게이션 맵 구축 → 목표기반 에이전트가 버튼 시퀀스를 스스로 계획·실행 |
| **저장소** | `nuna20230424-ship-it/remote-control` (독립) · 패키지 `remotectl` |
| **스택** | Python 3.12 · FastAPI · Pydantic v2 · networkx · httpx · SQLite |
| **상태** | Mock 백엔드로 전 파이프라인 E2E 완결 · pytest 81 · ruff clean · PR #1~#5 머지 |
| **미완(실기기)** | detection 서명 안정성 튜닝 · 실 STB 스모크(코드셋/capture 볼륨·ffmpeg) — 사내망 필요 |

---

## 1. 기획

**목표**: 사업자·단말에 종속되지 않고, STB UI를 스스로 탐색·학습해 목표(자연어)를 달성하는 리모컨 제어 에이전트.

**핵심 유스케이스**
- **UC-1 학습**: 리모컨 키를 눌러가며 화면 전이를 관찰, 네비게이션 맵을 구축한다.
- **UC-2 맵 조회**: 학습된 상태/전이/커버리지를 조회한다.
- **UC-3 목표 실행**: "넷플릭스 켜줘" 같은 목표를 받아 최단 버튼 경로를 계획·실행하고, 어긋나면 재계획한다.

**스코프**
- (In) mock 백엔드만으로 학습→맵→실행 전 구간 완결 / 실 STB 어댑터(교체식) / REST·CLI·MCP 인터페이스.
- (Out, 후속) IR 코드 자체 캡처(그건 ir-mcp `/learn` 영역) / 사업자별 화면 라벨 온톨로지 표준화.

---

## 2. 설계

**파이프라인**: `학습(Learner) → 맵(NavGraph) → 에이전트(Planner+Executor)`

**교체 가능한 어댑터 (코어 엔진 변경 0줄, M5 경계)**
- `RemoteDriver` (제어): `MockRemoteDriver`(개발) / `RemoteMcpClient`(실 — ir-mcp + capture-mcp)
- `ScreenSense` (판정): `MockScreenSense`(개발) / `DetectionMcpScreenSense`(실 — detection-mcp/VLM)

**실 STB 스택 = 3 서비스** (사내망 실물 계약 확정)

| 서비스 | 엔드포인트 | remotectl 처리 |
|---|---|---|
| ir-mcp `:8002` | `POST /send {codeset, key}` | 키맵(DPAD_*/VOLUME_*/숫자/앱), repeat→N회, reset→HOME 폴백, `/codesets`→실 키집합 |
| capture-mcp `:8001` | `POST /capture` → MP4 | ffmpeg로 마지막 프레임 PNG 추출 |
| detection-mcp `:8103` | `POST /check/screen` → verdict+description | QA 판정기 → description(VLM 서술)을 로컬 합성해 상태 식별 |

**데이터/영속화**
- 네비게이션 맵: JSON (`navmap`, 상태=노드/전이=엣지, networkx).
- 키이벤트↔화면 매핑: **별도 SQLite DB**(`keyscreen`, navmap과 분리).

**모듈(tier)**: tier0 설정/모델/드라이버·센스 추상+mock+실어댑터 → tier1 Learner/NavGraph/식별 → tier2 Planner/Executor → tier3 FastAPI app. 부가: `store`(DB)·`reconcile`(자가치유)·`autolearn`(자동반복)·`mcp_server`.

---

## 3. 구현 현황

**완료 (PR #1~#5, main 반영)**

| PR | 내용 |
|---|---|
| 초기 | 6단계 전문가 파이프라인으로 맨바닥 빌드(기획→설계→디자인→구현→검증→배포) |
| #1 | MCP 서버 — 다른 AI 에이전트가 도구로 호출 |
| #2 | 실 STB 스택(ir-mcp/capture-mcp/detection-mcp) 배선 |
| #3 | 커버리지 키집합을 **실 리모컨 키 전체**로 재정렬 + 미커버 리포트 |
| #4 | **키↔화면 동시 학습 + 별도 DB + 오류 자가치유 + 목표 커버리지 자동 반복** |
| #5 | 구현 기능 개요 페이지(`docs/features.html`) |

**추가 기능(#4) 상세**
- **동시 학습**: 스텝마다 `(from,key)→to` 매핑과 화면 LLM 분석을 함께 별도 DB에 기록.
- **오류 자가치유**: 전송 실패→재전송, 판정 실패/저신뢰/비결정 전이→detection-mcp VLM 재판정(REOBSERVE)으로 정확 재매핑, 소진 시 건너뜀(무한 재시도 방지).
- **자동 반복**: 목표 커버리지 도달 / K라운드 무진전 / 상한으로 종료하고, 미커버 키·상태를 리포트로 노출.

**품질**: pytest **81건 통과**, ruff clean, GitHub Actions CI(ruff+pytest, py3.12).

**커버리지의 정의(중요)**: 커버리지 = "발견된 각 화면에서 각 실 리모컨 키의 효과(전이)를 관측한 비율". 리모컨 IR 코드 캡처가 아니다. **100%는 미발견 상태·도달불가 키 때문에 항상 보장되지 않으므로** 목표 커버리지를 파라미터로 받고 미달을 숨기지 않는다.

---

## 4. 운영

**호출 인터페이스 (3종)**

| 인터페이스 | 학습 | 목표 실행 | 자동 반복 | DB/맵 조회 |
|---|---|---|---|---|
| REST(:8099) | `POST /learn` | `POST /goal` | `POST /autolearn` | `GET /map` · `GET /keyscreen` |
| CLI | `remotectl learn` | `remotectl goal "…"` | `remotectl autolearn --target …` | `remotectl inspect` · `keyscreen` |
| MCP(stdio) | `remote_learn` | `remote_run_goal` | `remote_autolearn` | `remote_inspect_map` · `remote_keyscreen` |

**즉시 실행 (mock, 실 하드웨어 불필요)**
```bash
pip install -e ".[dev]"        # 또는 배포 wheel(§ 배포) 오프라인 설치
remotectl serve                # http://127.0.0.1:8099 대시보드
remotectl autolearn --target 0.9
```

**실 STB 배선 (env 스위치)**
```dotenv
REMOTECTL_DRIVER_BACKEND=mcp        # mock | mcp
REMOTECTL_SENSE_BACKEND=detection   # mock | detection
IR_MCP_URL=http://172.16.3.x:8002
REMOTECTL_IR_CODESET=ref_remote     # 대상 단말 코드셋
CAPTURE_MCP_URL=http://172.16.3.x:8001
DETECTION_MCP_URL=http://172.16.3.136:8103
REMOTECTL_KEYSCREEN_DB=./data/keyscreen.db
```

**배포**: `Dockerfile` · `docker-compose.yml`(사내망 env) · `scripts/run_dev.sh`·`run_learning.sh` · CI.

**MCP 통합(다른 에이전트가 도구로)**: `python -m remotectl.mcp_server`(stdio). 주의 — learn/goal은 같은 `REMOTECTL_MAP_STORE_PATH` 공유, 물리 STB 호출은 직렬화.

---

## 5. 리스크 / 남은 일 (로드맵)

1. **detection 서명 안정성 튜닝** — 화면 상태 서명이 흔들리면 그래프 파편화로 커버리지 수치가 무의미해짐. detection-mcp 서버측 vision 프롬프트/키워드 어휘가 최대 레버.
2. **실 STB 스모크** — `REMOTECTL_IR_CODESET` 실측 매칭(`GET /codesets`), capture `file_path` 공유 볼륨 + ffmpeg 배치. (사내망 실기기 필요)
3. **커버리지 완전성** — 100%는 "발견 상태 기준". 상태 분류 전용 baseline이 마련되면 description 합성 → 그 경로로 승격.

---

## 6. 배포물 (git 없이 사용)

git 접근 없이도 에이전트를 실행할 수 있도록 **오프라인 설치용 wheel**을 제공한다. (설치·사용 상세는 `docs/DISTRIBUTION.md`)

```bash
pip install remotectl-0.1.0-py3-none-any.whl   # 배포된 파일만으로 설치
remotectl serve                                 # mock 으로 즉시 실행
```

- 대시보드 정적 자산이 wheel 에 포함되어 별도 파일 없이 대시보드가 뜬다.
- mock 백엔드가 기본이라 사내망/실 STB 없이도 학습→맵→목표실행→자동반복 전 구간이 동작한다.
- 실연동은 위 env 를 채우면 코드 변경 없이 전환된다.
