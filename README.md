# remotectl — STB 리모컨 학습 에이전트

셋탑박스(STB)를 리모컨으로 조작하면서 화면 전이를 **관찰·학습**하고, 관찰을 **네비게이션 맵(상태 그래프)** 으로 구축한 뒤, 자연어 목표(예: "넷플릭스 켜줘")를 받아 **눌러야 할 버튼 시퀀스를 스스로 계획·실행**하는 목표기반 에이전트다. 실행 중 화면이 예상과 다르면 현재 상태 기준으로 **재계획**한다.

실 STB 제어는 `RemoteDriver` 추상 뒤에 개발용 `MockRemoteDriver` 와 사내망 HTTP 어댑터 `RemoteMcpClient` 를, 화면 판정은 `ScreenSense` 추상 뒤에 `MockScreenSense` 와 VLM 어댑터 `DetectionMcpScreenSense` 를 둔다. **1차 릴리스는 Mock 드라이버/센스만으로 학습→맵→목표실행 전 파이프라인이 엔드투엔드로 완결**된다. 실 remote-MCP / detection-MCP 엔드포인트는 미확정이며, 어댑터 안의 `[WIRE-*]` 표식 지점만 채우면 코어 엔진은 한 줄도 바꾸지 않고 실연동으로 전환된다.

관련 문서:
- [docs/PRD.md](docs/PRD.md) — 제품 요구사항(유스케이스 UC-1/2/3, 요구사항 R·마일스톤 M)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 아키텍처 정본(모듈/tier/데이터모델/API 계약)
- [docs/DESIGN.md](docs/DESIGN.md) — 운영자 대시보드 설계
- [docs/INTEGRATION.md](docs/INTEGRATION.md) — 통합/배선 노트

---

## 아키텍처 요약

핵심 루프는 세 단계다.

1. **학습(Explore, UC-1)** — `engine/learner.py`. 미탐색 간선 우선 BFS 정책으로 `press → settle → capture → observe → resolve_state → observe_transition` 루프를 돌며 상태·전이를 수집한다. 예산(step_budget) 또는 커버리지 목표 도달 시 종료하고 `LearningSummary` 를 반환한다.
2. **맵 구축(Build, UC-2)** — `navmap.py`. 관찰을 `NavMap`(영속화 정본: states/transitions)에 upsert 하고, 이로부터 `networkx.DiGraph` 런타임 뷰를 재구성한다. 상태 id 는 화면 signature 파생이라(`identify.py`) 같은 화면은 같은 id 로 결정론적으로 수렴한다.
3. **목표기반 실행(Plan & Act, UC-3)** — `goals.py`(자연어→`Goal` 규칙 해석) → `engine/planner.py`(맵 최단경로→`PlanStep` 시퀀스) → `engine/executor.py`(실행 + 스텝별 관찰 검증 + 불일치 시 재계획). 모든 실패는 예외를 던지지 않고 `ExecutionResult` 로 정규화된다.

### 두 개의 교체 가능한 추상

```
                 engine (learner / executor)  ← 구체 구현체를 절대 임포트하지 않음(M5)
                        │            │
              RemoteDriver        ScreenSense          ← 추상(abc.ABC)
              (전송 경계)          (판정 경계)
              ┌────┴─────┐       ┌────┴──────────┐
   MockRemoteDriver  RemoteMcpClient  MockScreenSense  DetectionMcpScreenSense
   (결정적 시뮬)     (사내망 HTTP)     (개발용)          (detection-MCP / VLM)
```

- **RemoteDriver** — `press(key)`(부작용만), `capture() -> RawScreen`(원재료 획득), `reset()`(HOME 복귀), `info() -> DriverInfo`. 화면 판정은 하지 않는다.
- **ScreenSense** — `observe(raw: RawScreen) -> SenseResult`. `RawScreen` 을 판정해 정규화 signature/상태 id 파생/신뢰도를 붙여 `ScreenState` 로 승격한다.
- 드라이버가 원재료(`RawScreen`)를 주면 센스가 판정해 결과(`SenseResult`)를 돌려주는, 계층 경계 전송 객체(dict 를 코어로 흘리지 않음) 구조다.

**핵심 불변식(M5):** `learner`/`executor` 는 어떤 구체 구현체도 임포트하지 않고, 유일한 wiring 지점인 `api/deps.py` 에서만 주입받는다. tier 의존 방향(tier3→tier2→tier1→tier0)은 논리적으로 강제되며, 이 덕분에 Mock↔실물 교체가 코어 변경 0줄로 이루어진다.

---

## 빠른 시작 (Mock 으로 즉시 실행)

환경변수 없이 기본값이 `driver_backend=mock`, `sense_backend=mock` 이라 설치 직후 바로 전 파이프라인이 돈다.

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# 1) 학습: 기본 시나리오(home/launcher_*/netflix·youtube·settings)를 탐색해 맵을 만든다
remotectl learn --steps 200

# 2) 맵 조회: 수집된 상태/전이/커버리지 요약
remotectl inspect

# 3) 목표 실행: 자연어 목표 → 버튼 시퀀스 계획·실행
remotectl goal "넷플릭스 켜줘"
#   → 예: RIGHT, OK 로 netflix 앱 도달 (status=SUCCESS)
```

또는 대시보드로 한 번에:

```bash
remotectl serve            # http://127.0.0.1:8099 접속 → 학습·맵·목표를 UI 로
```

Mock 드라이버는 결정적 상태머신이므로 STB 하드웨어 없이 재현 가능하며, `flaky_transitions` 로 전이 비결정성을 주입하면 executor 의 재계획 견고성(R3/M3)도 검증할 수 있다.

---

## 설치 / 실행 / 테스트

`buildRunDeploy` 명세 기준. Makefile 타깃(`make install|run|test|lint`)도 동일 동작을 감싼다.

### 설치

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

### 실행

```bash
# REST + 대시보드 (editable 설치 시)
uvicorn remotectl.api.app:app --port 8099
# 또는 src 레이아웃을 직접 지정
PYTHONPATH=src uvicorn remotectl.api.app:app --port 8099

# CLI (editable 설치 시 remotectl 명령 사용, 아니면 PYTHONPATH=src python -m ...)
remotectl serve --host 127.0.0.1 --port 8099   # REST + 대시보드 서빙
remotectl learn --steps 200 --coverage 0.9     # UC-1 탐색 학습
remotectl inspect                               # UC-2 저장된 맵 요약
remotectl goal "넷플릭스 켜줘"                    # UC-3 자연어 목표 실행
```

### 테스트

```bash
pytest -q
# 또는
PYTHONPATH=src pytest -q
```

계약 테스트(드라이버/센스), Mock 엔드투엔드(학습→맵→식별→플래너 최단경로→실행/재계획), API 스모크(`/health·/learn·/map·/map/path·/goal·/`), 그리고 아키텍처 임포트 검사(엔진이 구체 구현체를 임포트하지 않는지 AST 로 검증, M5)를 포함한다.

### REST API 엔드포인트

| 메서드 | 경로 | 용도 |
|--------|------|------|
| `GET`  | `/health`   | 드라이버/센스 진단(`DriverInfo`·backend_name 근거) |
| `POST` | `/learn`    | UC-1 학습 세션 실행 → `LearningSummary` |
| `GET`  | `/map`      | UC-2 맵 상태/전이 조회 |
| `GET`  | `/map/path` | from→to 최단경로를 `PlanStep` 열로 반환(도달 가능성 정규화) |
| `POST` | `/goal`     | UC-3 자연어 목표 실행 → `ExecutionResult` |
| `GET`  | `/`         | 자족 대시보드(인라인 자산, 외부 CDN 0) |

---

## 다른 AI 에이전트에서 도구로 호출 (MCP)

상위 오케스트레이터 에이전트(예: `stb-ai-tc-automation`)가 remotectl을 **도구**로 쓰도록
MCP 서버를 제공한다. 엔진을 인프로세스로 재사용하며 백엔드/맵경로는 전부 환경변수로 결정된다.

```bash
pip install -e ".[mcp]"        # 선택 의존성(mcp) 설치
python -m remotectl.mcp_server # stdio 트랜스포트로 기동 (또는 remotectl-mcp)
```

노출 도구: `remote_learn`(UC-1) · `remote_inspect_map`(UC-2) · `remote_find_path` · `remote_run_goal`(UC-3).

오케스트레이터 MCP 설정 등록 예:

```jsonc
{ "mcpServers": {
  "remotectl": {
    "command": "python", "args": ["-m", "remotectl.mcp_server"],
    "env": {
      "REMOTECTL_DRIVER_BACKEND": "mcp",            // 실 STB. 개발은 "mock"
      "REMOTE_MCP_URL": "http://172.16.x.x:PORT",   // mcp_client.py TODO 배선 후
      "REMOTECTL_SENSE_BACKEND": "detection",       // 개발은 "mock"
      "REMOTECTL_MAP_STORE_PATH": "/shared/navmap.json"  // learn/goal 이 공유(볼륨 마운트)
    }}}}
```

> **주의**: (1) `learn`과 `goal`은 같은 `REMOTECTL_MAP_STORE_PATH`를 공유해야 한다(별도
> 프로세스면 공유 볼륨). (2) 물리 STB는 공유 자원이므로 오케스트레이터에서 호출을 직렬화하라.
> (3) 미학습 상태의 `goal`은 `failed_unresolved`/`failed_unreachable`로 정규화되니, 그 신호를
> 보고 먼저 `remote_learn`을 호출하도록 설계하면 견고하다. REST(`/learn`·`/goal`)로도 동일하게 가능.

---

## 실 STB 배선 (ir-mcp · capture-mcp · detection-mcp)

어댑터는 사내 STB 스택의 **실물 계약**에 맞춰 배선돼 있다(2026-07 확정). 제어·캡처·판정이
**세 서비스**로 분리돼 있고, 코어 엔진 변경은 0줄이다(M5).

### 1. 백엔드 선택 스위치

`.env.example` 을 `.env` 로 복사해 채운다(`.env` 는 커밋 금지). 구현체 선택은 `config.Settings` 가 읽는다.

```dotenv
# 개발: mock (기본) / 실연동: mcp·detection 으로 전환
REMOTECTL_DRIVER_BACKEND=mcp        # mock | mcp
REMOTECTL_SENSE_BACKEND=detection   # mock | detection

# 리모컨 제어(ir-mcp): POST /send {codeset, key}
IR_MCP_URL=http://172.16.3.136:8002
REMOTECTL_IR_CODESET=ref_remote     # 대상 단말/리모컨에 맞는 코드셋
REMOTECTL_RESET_HOME_PRESSES=1      # reset 엔드포인트 없음 → HOME 반복 폴백

# 화면 캡처(capture-mcp): POST /capture → MP4 (ffmpeg 로 프레임 추출)
CAPTURE_MCP_URL=http://172.16.3.136:8001
REMOTECTL_CAPTURE_TARGET=dut
REMOTECTL_CAPTURE_DURATION_SEC=1

# 화면 판정(detection-mcp): POST /check/screen → verdict + description
DETECTION_MCP_URL=http://172.16.3.136:8103
DETECTION_MCP_MODEL=qwen2.5vl:7b
```

백엔드를 `mcp`/`detection` 으로 바꾸면 `api/deps.py` 가 `RemoteMcpClient.from_env()` /
`DetectionMcpScreenSense.from_env()` 로 어댑터를 생성한다. URL 미설정 시
`DriverUnavailableError`/`SenseUnavailableError` 로 명확히 실패한다.

### 2. 리모컨 제어 — ir-mcp (`drivers/mcp_client.py`)

- `press` → `POST /send {codeset, key}`. `DEFAULT_KEYMAP` 이 canonical `Button` 을 ir-mcp
  키명으로 매핑(방향키 `DPAD_*`, 음량 `VOLUME_*`, 숫자 `"0"~"9"`, 앱단축 대문자). repeat 는
  ir-mcp 가 지원하지 않아 **N회 호출**로 처리.
- `reset` → ir-mcp 에 reset 이 없어 **HOME 키 반복**(`REMOTECTL_RESET_HOME_PRESSES`)으로 폴백.
- `info` → `GET /health` 의 `status`/`backend` 파싱.
- **확인 필요**: `REMOTECTL_IR_CODESET` 이 대상 단말과 일치해야 실제 키가 송신된다
  (`GET /codesets`, `GET /codesets/{codeset}` 로 사용 가능 키 확인).

### 3. 화면 캡처 — capture-mcp (`drivers/mcp_client.py`)

- `capture` → `POST /capture {target, duration_sec}` 로 짧은 MP4 를 녹화한 뒤 **ffmpeg 로
  마지막 프레임을 PNG 로 추출**해 `RawScreen.image_bytes` 에 싣는다.
- **전제**: (1) 로컬에 `ffmpeg` 설치. (2) capture-mcp 가 반환하는 `file_path` 를 이 프로세스가
  읽을 수 있어야 함(**공유 볼륨** 마운트). `CAPTURE_MCP_URL` 미설정 시 캡처 미가용.

### 4. 화면 판정 — detection-mcp (`sense/detection_mcp.py`)

- `observe` → `POST /check/screen {scenario, image_base64, prefer_vision}`.
- detection-mcp 는 상태 분류기가 아니라 **QA 판정기**이므로, 응답 `description`(VLM 서술)을
  **로컬에서 합성**해 상태를 만든다: 키워드 규칙으로 `kind`/`app_id` 판별, 의미 토큰으로 안정적
  `signature` 구성.
- **튜닝 레버**([TUNE]): `_KIND_KEYWORDS`/`_APP_KEYWORDS` 어휘와 **detection-mcp 서버측 vision
  프롬프트**(사내 리포). 서명 안정성/정확도는 여기서 올린다.

> 남은 확인/후속: ir-mcp 코드셋 실측 매칭, capture 공유 볼륨/ffmpeg 배치, detection 상태 서명
> 안정성 튜닝. 상태 분류 전용 baseline 이 마련되면 `description` 합성 대신 그 경로로 승격 가능.

---

## 커버리지의 의미 (무엇을 "학습 완료"로 보는가)

커버리지 = **발견된 각 화면에서 각 리모컨 키를 최소 한 번 눌러 그 효과(전이)를 관측한 비율**이다.
(리모컨 IR 코드 자체를 캡처·복제하는 것이 아니라 — 그건 ir-mcp `/learn` 의 몫 — 키의 UI 효과를 관측한다.)

- **커버리지 키집합 = 실 리모컨 키 전체.** 학습기는 `driver.available_keys()`(실 드라이버는
  ir-mcp `GET /codesets/{codeset}`)로 대상 단말의 전체 키를 받아 커버리지 분모로 삼는다. 고정 7키가
  아니다. 드라이버가 미보고(mock)면 기본 탐색셋으로 폴백하며, `LearningSummary.key_set_source`
  (`driver`|`default`|`explicit`)로 어느 쪽인지 드러난다. `POWER` 등 위험 키는 자동 탐색에서 제외.
- **100% 는 "발견된 상태" 기준이다.** 아직 도달 못 한 화면은 분모에 없다 — 미발견 영역까지의
  완전성은 보장하지 않는다(오라클/완전성 한계).
- **미달은 숨기지 않는다.** `LearningSummary.key_coverage`(키별 비율)와 `uncovered_key_tokens`
  (모든 발견 상태에서 시도되지 않은 키), `NavGraph.coverage_report()`(상태별 미시도 키)로 노출한다.
- **전제**: 커버리지 수치가 신뢰되려면 화면 상태 식별(§4 detection)이 안정적이어야 한다. 서명이
  흔들리면 그래프가 파편화돼 분모가 부풀고 커버리지가 무의미해진다.

---

## 대시보드 사용법

`remotectl serve` 후 브라우저로 `http://127.0.0.1:8099/` 접속. 외부 CDN 의존이 없는 자족 SPA(바닐라 JS + `fetch`)이며 사내망/오프라인에서도 뜬다.

- **Health** — 현재 드라이버/센스 백엔드와 타깃/엔드포인트 상태(`/health` 근거)를 보여준다. mock 인지 실연동인지 한눈에 확인.
- **Learn** — 스텝 예산/커버리지 목표를 주고 학습 세션을 실행(`POST /learn`). 종료 후 방문 상태 수·전이 수·커버리지·종료 사유를 요약.
- **Map** — 수집된 상태 노드와 전이 간선을 조회(`GET /map`). 상태 kind/label/신뢰도/방문 횟수 확인.
- **Path** — 두 상태 사이 최단경로를 눌러야 할 버튼 시퀀스로 표시(`GET /map/path`).
- **Goal** — 자연어 목표를 입력하면(`POST /goal`) 계획된 버튼 시퀀스, 실행 결과(SUCCESS / FAILED_*), 재계획 횟수를 반환한다.

상세 디자인/컴포넌트/API 소비 계약은 [docs/DESIGN.md](docs/DESIGN.md) 참조.

---

## 디렉토리 구조

```
remote-control/
├─ README.md
├─ pyproject.toml            # 패키지·의존성·CLI 진입점(remotectl → remotectl.api.app:main)
├─ Makefile                  # install / run / test / lint
├─ .env.example              # 환경변수 예시(.env 로 복사)
├─ data/                     # 맵 영속화(navmap.json) 기본 저장 위치
├─ docs/
│  ├─ PRD.md                 # 제품 요구사항
│  ├─ ARCHITECTURE.md        # 아키텍처 정본
│  ├─ DESIGN.md              # 대시보드 설계
│  └─ INTEGRATION.md         # 통합/배선 노트
├─ src/remotectl/
│  ├─ models.py              # [tier0] 전 계층 공유 데이터 정본(Pydantic v2)
│  ├─ config.py              # Settings / load_settings
│  ├─ navmap.py              # [tier1] NavGraph(networkx 런타임 뷰): upsert/최단경로/커버리지
│  ├─ identify.py            # [tier1] SenseResult → 안정 상태 id 매칭/해석
│  ├─ goals.py               # [tier2] 자연어 → Goal 규칙 해석
│  ├─ drivers/
│  │  ├─ base.py             # [tier0] RemoteDriver 추상 + 전송 dataclass + 예외 계층
│  │  ├─ mock.py             # [tier0] MockRemoteDriver(결정적 시뮬)
│  │  └─ mcp_client.py       # [tier0] RemoteMcpClient(사내망 HTTP, [WIRE-*])
│  ├─ sense/
│  │  ├─ base.py             # [tier0] ScreenSense 추상 + normalize_signature + 예외
│  │  ├─ mock.py             # [tier0] MockScreenSense(개발용)
│  │  └─ detection_mcp.py    # [tier0] DetectionMcpScreenSense(VLM 스텁, [WIRE-*])
│  ├─ engine/
│  │  ├─ learner.py          # [tier1] UC-1 탐색 학습 루프
│  │  ├─ planner.py          # [tier2] UC-3 최단경로 → PlanStep
│  │  └─ executor.py         # [tier2] UC-3 실행·검증·재계획
│  └─ api/
│     ├─ app.py              # [tier3] FastAPI 조립·REST·대시보드 서빙 + CLI(main)
│     ├─ deps.py             # [tier3] 유일한 wiring 지점(구현체 주입, M5)
│     └─ static/             # 자족 대시보드 정적 자산(index.html 등)
└─ tests/                    # 계약 / 엔드투엔드 / API 스모크 / 아키텍처 임포트 검사
```

---

## 제약 / 다음 단계

**현재 제약 (1차 릴리스)**

- 실 remote-MCP / detection-MCP 엔드포인트가 **미인증·미확정**이다. `mcp`/`detection` 백엔드는 `[WIRE-*]` 지점이 가정 계약으로 채워진 스텁이며, 실연동은 후속 작업이다. **엔드투엔드로 완결되는 것은 Mock 백엔드 경로뿐**이다.
- 자연어 목표 해석은 **규칙 기반**이다(별칭 사전 + 라벨 부분매칭, 1차 LLM 아님). `OPEN_APP | GOTO_STATE | GOTO_KIND` 목표 유형을 다루며, 미학습/미매핑은 `resolved=False` 사유로 정규화된다.
- 학습 정책은 미탐색 간선 우선 BFS로, 대규모 UI 트리에서의 최적 탐색 전략은 향후 개선 대상이다.

**다음 단계**

1. **실 remote-MCP 배선** — 사내망 엔드포인트/키맵/요청·응답 스키마 확정 후 `mcp_client.py` 의 `[WIRE-*]` 교체 및 실 STB 대상 스모크.
2. **detection-MCP / VLM 배선 + 프롬프트 튜닝** — `CALIBRATION_PROMPT`(`[WIRE-PROMPT]`)가 판정 정확도의 최대 레버이므로 실측 기반으로 우선 튜닝.
3. **판정 신뢰도/저신뢰 처리 강화** — `low_confidence` 상태의 재관찰·보정 정책.
4. **목표 해석 LLM 확장** — 규칙 해석을 폴백으로 두고 LLM 기반 목표 해석을 선택적으로 결합.
5. **맵 진화 내성** — 앱/펌웨어 업데이트로 화면이 바뀔 때의 재학습·맵 병합 전략.
