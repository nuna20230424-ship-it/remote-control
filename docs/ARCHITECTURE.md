# remotectl 아키텍처 (확정 명세)

STB 리모컨 학습 에이전트. 이 문서는 세 설계 트랙(아키텍처/데이터/통합)을 종합해 리드 아키텍트가
확정한 **단일 구현 계약**이다. 이미 디스크에 존재하는 코드(`src/remotectl/models.py`,
`drivers/`, `sense/`)의 실제 시그니처를 정본으로 삼고, 아직 미작성인 엔진/API 계층의 계약을
확정한다. 여기 명시된 경로·시그니처·의존 방향은 각 담당 에이전트가 지켜야 할 계약이다.

---

## 0. 확정된 전역 규약 (트랙 간 충돌 해소 결과)

세 트랙 사이의 불일치를 다음과 같이 확정한다. **아래 결정이 최종이며, 트랙별 원안보다 우선한다.**

- **레이아웃**: `src/` 레이아웃 + 기능 폴더(`drivers/`, `sense/`)를 채택한다. 아키텍처 원안의
  `remotectl/tierN/` 물리 디렉터리는 **채택하지 않는다**. 이미 데이터/통합 트랙이 `src/remotectl/`에
  동작하는 코드와 통과 테스트를 올려두었기 때문이다. tier(계층)는 **논리적 개념**으로만 유지하며,
  의존 방향 강제에 사용한다.
- **패키지 임포트**: 절대 임포트만 사용(`from remotectl.models import ...`). 실행은 `PYTHONPATH=src`
  또는 editable 설치(`pip install -e .`)로 `remotectl`이 임포트되게 한다.
- **`__init__.py` 재노출 허용**: 아키텍처 원안의 "`__init__.py`는 빈 파일" 규칙은 **폐기**한다.
  `drivers/__init__.py`·`sense/__init__.py`는 공개 심볼을 재노출하며 이미 테스트가 이에 의존한다.
  단, 재노출은 순수 심볼 재노출만 하고 부수효과(네트워크/파일 I/O)를 두지 않는다.
- **ScreenSense.observe 는 인자를 받는다**: `observe(self, raw: RawScreen) -> SenseResult`.
  아키텍처 원안의 무인자 `observe()`는 폐기. 캡처(드라이버)와 판정(센스)이 분리되므로 엔진이
  `raw = driver.capture()` 후 `sense.observe(raw)`를 호출한다. 이것이 M5(전송/판정 독립 교체)의 근거다.
- **드라이버/센스는 추상 클래스(abc.ABC)**: 원안의 Protocol 대신 `abc.ABC` 상속을 정본으로 한다
  (이미 구현체가 상속 중). 코어 엔진은 `RemoteDriver`/`ScreenSense` 추상 타입만 임포트한다.
- **데이터 모델 정본**: `remotectl/models.py`의 Pydantic v2 모델이 전 계층 공유 계약이다.
  dict를 계층 경계로 흘리지 않는다(드라이버 원재료 `RawScreen`/`DriverInfo`/`SenseResult`는
  dataclass 전송 객체이며 판정 후 `ScreenState` 모델로 승격된다).
- **버튼 어휘**: `models.Button`(str, Enum) 하나로 통일. 문자열 리터럴 금지. 앱 단축키는
  `KeyPress(button=Button.APP_SHORTCUT, app_shortcut="netflix")`로 표현하고, 전이 키는
  `KeyPress.token`("HOME", "RIGHT*3", "APP_SHORTCUT:netflix")을 정본으로 쓴다.
- **상태 정체성**: `ScreenState.id = compute_state_id(normalize_signature(sig))`. 같은 화면→같은 id로
  결정론적 수렴(R2 완화). 센스가 signature를 정규화하고 id를 파생한다.
- **맵 영속화 정본**: `NavMap`(Pydantic) 의 `states`/`transitions`가 저장 정본. networkx 그래프는
  이로부터 재구성하는 런타임 뷰다(그래프 라이브러리 교체가 저장 포맷에 영향 없음). 저장/로드는
  `NavMap.dump_json()`/`NavMap.load_json()`.
- **에러 정규화**: 드라이버 오류는 `RemoteDriverError`(및 하위), 센스 오류는 `ScreenSenseError`(및
  하위)로만 코어에 올라온다. 실행 엔진은 이 둘을 잡아 `ExecutionStatus.FAILED_DRIVER`로 매핑한다.

---

## 1. 계층(tier)과 의존 방향

논리 tier는 하향 단방향으로만 의존한다: **tier3 → tier2 → tier1 → tier0**. 하위가 상위를 임포트하면
아키텍처 위반(테스트로 강제 권장). **핵심 불변식**: `engine.learner`·`engine.executor`는 구체 구현체
(`MockRemoteDriver`/`RemoteMcpClient`/`DetectionMcpScreenSense`)를 **임포트하지 않는다**. 구현체 선택·
주입은 오직 tier3의 조립 지점(`api.deps`)에서만 일어난다 → M5(교체 시 코어 코드 0줄 변경) 성립.

```
tier0  models.py            (순수 데이터. 내부 의존 없음)
       drivers/base.py      (RemoteDriver 추상 + RawScreen/DriverInfo/예외)  ← models
       drivers/mock.py      (MockRemoteDriver)                              ← base, models
       drivers/mcp_client.py(RemoteMcpClient httpx 스텁)                     ← base, models
       sense/base.py        (ScreenSense 추상 + SenseResult/normalize)       ← base, models
       sense/mock.py        (MockScreenSense)                               ← sense.base, base, models
       sense/detection_mcp.py(DetectionMcpScreenSense VLM 스텁)             ← sense.base, base, models
tier1  navmap.py            (NavMap networkx 뷰 + 경로/커버리지)             ← models
       identify.py          (관찰→상태 식별/매칭)                            ← models, navmap
       engine/learner.py    (탐색 학습 세션)          ← RemoteDriver, ScreenSense(추상), navmap, identify, models
tier2  goals.py             (자연어→Goal 규칙 해석)                          ← models, navmap
       engine/planner.py    (최단경로→PlanStep)                             ← models, navmap
       engine/executor.py   (실행·검증·재계획)  ← RemoteDriver, ScreenSense(추상), navmap, planner, goals, identify, models
tier3  api/deps.py          (설정→구현체 선택·조립. 유일 wiring)  ← 모든 구현체, config
       api/app.py           (FastAPI REST + 대시보드 + CLI 엔트리)  ← deps, learner, planner, executor, goals
       config.py            (Settings. tier0에 두되 deps만 사용)
```

주의: `config.py`는 물리적으로 tier0(순수)지만 논리적으로는 tier3(조립)에서만 소비된다.

---

## 2. 이미 확정된 계약 (디스크 정본 — 수정 금지)

아래는 데이터/통합 트랙이 이미 작성·검증한 파일이다. 엔진/API 담당은 **이 시그니처에 맞춰** 소비만 한다.

### 2.1 `remotectl/models.py` (데이터 정본)
- `Button(str, Enum)`: HOME/BACK/UP/DOWN/LEFT/RIGHT/OK/MENU/EXIT, 미디어(PLAY_PAUSE/STOP/REWIND/
  FAST_FORWARD), VOL_*/CH_*/MUTE, POWER, NUM_0~9, APP_SHORTCUT.
- `KeyPress(button, app_shortcut=None, repeat=1)`: `.token` 프로퍼티, `__hash__`. APP_SHORTCUT 검증.
- `StateKind`: home/app/menu/settings/playback/live_tv/dialog/loading/unknown.
- `compute_state_id(signature) -> "st_"+sha1[:16]`.
- `ScreenState(id="", signature, label=None, kind=UNKNOWN, app_id=None, screenshot_ref=None,
  confidence=1.0, visit_count=0, first_seen, last_seen)`: id 자동 파생.
- `Observation(session_id, seq, from_state_id=None, key=None, to_state_id, sensed_signature,
  sensed_confidence=1.0, settle_ms=0, timestamp, note=None)`.
- `Transition(from_state_id, key, to_state_id, observed_count=1, success_count=0, confidence=1.0,
  first_observed, last_observed)`: `.edge_key` computed.
- `NavMap(schema_version=1, map_id="default", states: dict[str,ScreenState], transitions: list,
  root_state_id=None, created_at, updated_at)`: `get_state`/`outgoing`/`state_count`/
  `transition_count`/`dump_json(indent=2)`/`load_json(data)`. states 키=id 일치 검증.
- `GoalType`: open_app/goto_state/goto_kind. `Goal(raw_text, goal_type, target_app_id=None,
  target_state_id=None, target_kind=None, resolved=False, resolve_note=None)`.
- `PlanStep(index, from_state_id, key, expected_to_state_id, executed=False, actual_to_state_id=None,
  matched=None, observed_confidence=None)`.
- `ExecutionStatus`: success/failed_unreachable/failed_unresolved/failed_budget/failed_driver.
- `ExecutionResult(status, goal, start_state_id, final_state_id, steps, replans=0, started_at,
  finished_at=None, message=None)`: `.button_sequence`/`.step_count` computed, `.succeeded`.
- `LearningSummary(session_id, steps_taken, states_visited, transitions_recorded, unexplored_edges=0,
  coverage_ratio=None, started_at, finished_at=None, stop_reason=None)`.

### 2.2 `remotectl/drivers/base.py` (전송 추상)
- `RemoteDriver(abc.ABC)`: 추상 `press(key: KeyPress) -> None`, `capture() -> RawScreen`,
  `reset() -> None`, `info() -> DriverInfo`. 기본제공 `settle(ms)`, `press_and_capture(key, settle_ms=0)`,
  `close()`, 컨텍스트 매니저.
- `RawScreen(image_ref, image_bytes, image_mime, text_hint, meta, captured_at_ms)` dataclass +
  `has_pixels()`. `DriverInfo(name, target, endpoint, supports_capture, ready)` dataclass.
- 예외: `RemoteDriverError ⊃ {DriverUnavailableError, PressError, CaptureError}`. `HOME_KEY` 상수.

### 2.3 `remotectl/drivers/mock.py`, `mcp_client.py`
- `MockRemoteDriver(scenario=None, name="mock")`: 결정적 상태머신. `current_screen_key`/`goto()`
  (테스트용, 계약 밖). `default_stb_scenario()`가 home/launcher_*/app_(netflix|youtube|settings) 맵 제공.
  `MockScenario(screens, transitions, start_screen, home_screen, flaky_transitions={})` — flaky로 R3 주입.
- `RemoteMcpClient(base_url, ...)` + `from_env(**overrides)`(REMOTE_MCP_URL/TOKEN/TIMEOUT), `DEFAULT_KEYMAP`.
  httpx 스텁, `[WIRE-*]` 표식. 도달불가→`DriverUnavailableError`.

### 2.4 `remotectl/sense/base.py`, `mock.py`, `detection_mcp.py`
- `ScreenSense(abc.ABC)`: 추상 `observe(raw: RawScreen) -> SenseResult`, `backend_name` 프로퍼티.
  `confidence_threshold=0.5`. 헬퍼 `_build_result(...)`. `normalize_signature(text)`.
- `SenseResult(state: ScreenState, raw_signature: str, low_confidence: bool=False)` dataclass.
- 예외: `ScreenSenseError ⊃ SenseUnavailableError`.
- `MockScreenSense(confidence=1.0)`: RawScreen.text_hint→signature, meta→label/kind/app_id.
- `DetectionMcpScreenSense(...)` + `from_env(**overrides)`, `CALIBRATION_PROMPT`. VLM 스텁.

---

## 3. 확정할 미작성 계약 (엔진/맵/목표/API 담당이 구현)

### 3.1 `remotectl/navmap.py` (tier1)
networkx.DiGraph를 NavMap 위에 두는 런타임 그래프 뷰. 학습이 채우고 계획이 소비하고 REST가 조회.

```python
class NavGraph:
    """NavMap(정본) 위의 networkx.DiGraph 런타임 뷰. 노드=state_id, 엣지=Transition.
    저장 정본은 self.navmap(NavMap)이며, 그래프는 여기서 재구성한 캐시다."""
    def __init__(self, navmap: NavMap | None = None) -> None: ...
    @property
    def navmap(self) -> NavMap: ...
    def observe_transition(self, from_state: ScreenState, key: KeyPress, to_state: ScreenState) -> Transition:
        """상태 upsert(visit_count 증가) + (from,key,to) 전이 기록/누적(observed_count) 후 Transition 반환."""
    def upsert_state(self, state: ScreenState) -> ScreenState:
        """상태를 맵에 추가/갱신(visit_count 증가, last_seen 갱신). root 미설정이면 첫 상태를 root로."""
    def outgoing(self, state_id: str) -> list[Transition]:
        """해당 상태에서 나가는 전이 목록."""
    def shortest_path(self, from_id: str, to_id: str) -> list[Transition] | None:
        """networkx 최단경로 → Transition 리스트. 도달 불가면 None(엔진이 UNREACHABLE 매핑)."""
    def unexplored(self, from_id: str, button_set: list[KeyPress]) -> list[KeyPress]:
        """해당 상태에서 아직 관측되지 않은 후보 키 목록(미탐색 간선 우선 정책의 근거)."""
    def coverage(self, button_set: list[KeyPress]) -> float:
        """관측 전이 / (상태수 × 후보키수) 비율(M4). 상태 0이면 0.0."""
    def save(self, path: str) -> None:
        """NavMap.dump_json()으로 파일 저장(디렉터리 자동 생성)."""
    @classmethod
    def load(cls, path: str) -> "NavGraph":
        """파일에서 NavMap.load_json()으로 복원. 파일 없으면 빈 그래프."""
```

### 3.2 `remotectl/identify.py` (tier1)
관찰(SenseResult) → 안정 state id 부여 및 기존 상태 매칭(R2). 순수 함수 계층.

```python
def match_state(result: SenseResult, navmap: NavMap) -> str | None:
    """SenseResult.state.id가 맵에 이미 있으면 그 id, 없으면 None(신규). id는 signature 파생이므로
    같은 화면은 자동 매칭된다(내용 주소화). 저신뢰(low_confidence)는 매칭하되 호출자가 재관찰 판단."""
def resolve_state(result: SenseResult, graph: "NavGraph") -> ScreenState:
    """SenseResult를 맵에 반영(upsert)하고 확정된 ScreenState를 반환. 신규면 등록, 기존이면 갱신."""
```

### 3.3 `remotectl/engine/learner.py` (tier1)
UC-1. 미탐색 간선 우선 BFS 탐색. **추상 타입만 의존**(구체 드라이버/센스 임포트 금지).

```python
class Learner:
    """탐색 학습 엔진. RemoteDriver/ScreenSense 추상에만 의존(M5).
    정책: 현재 상태의 미탐색 후보 키를 우선 선택, 없으면 미탐색 간선 보유 상태로 BFS 이동."""
    def __init__(self, driver: RemoteDriver, sense: ScreenSense, graph: NavGraph,
                 button_set: list[KeyPress] | None = None, settle_ms: int = 400) -> None:
        """button_set 미지정 시 DEFAULT_BUTTON_SET(방향/OK/BACK/HOME + 앱단축키) 사용."""
    def learn(self, step_budget: int = 200, coverage_target: float = 0.9,
              session_id: str | None = None) -> LearningSummary:
        """reset→초기 관찰→루프[후보키 선택→press→settle→capture→observe→resolve_state→
        observe_transition→종료조건]. 예산/커버리지 도달 시 종료. LearningSummary 반환.
        드라이버/센스 오류는 잡아 stop_reason에 기록하고 조기 종료(부분 맵 보존)."""
    def _select_key(self, current_state_id: str) -> KeyPress | None:
        """미탐색 간선 우선 BFS 정책으로 다음 키 선택. 전 후보 탐색 완료면 None(세션 종료)."""

DEFAULT_BUTTON_SET: list[KeyPress]
"""각 상태에서 시도할 기본 후보 키(탐색 순서 포함): RIGHT/LEFT/UP/DOWN/OK/BACK/HOME + 앱 단축키."""
```

### 3.4 `remotectl/goals.py` (tier2)
UC-3의 자연어→Goal 규칙 해석(R4, LLM 아님).

```python
GOAL_ALIASES: dict[str, str]
"""키워드/별칭 → app_id 사전(예: "넷플릭스"/"netflix"→"netflix", "유튜브"→"youtube", "설정"→"settings")."""
def resolve_goal(text: str, navmap: NavMap) -> Goal:
    """자연어를 Goal로 해석. 1) 별칭사전으로 app_id 추출→OPEN_APP, 2) '홈으로'류→GOTO_KIND(home),
    3) 맵 상태 라벨 부분매칭→GOTO_STATE. 매핑 성공 시 목표 상태를 맵에서 찾아 resolved 설정,
    미학습/미매핑이면 resolved=False + resolve_note에 사유(엔진이 FAILED_UNRESOLVED 매핑)."""
def find_goal_state_id(goal: Goal, navmap: NavMap) -> str | None:
    """해석된 Goal에 해당하는 맵 상태 id 반환(OPEN_APP=app_id 일치, GOTO_KIND=kind 일치 첫 상태,
    GOTO_STATE=target_state_id). 맵에 없으면 None."""
```

### 3.5 `remotectl/engine/planner.py` (tier2)
UC-3 계획. NavGraph 최단경로를 PlanStep 시퀀스로.

```python
class PlanningError(RuntimeError):
    """도달 불가/미학습 영역 등 계획 실패(R5). 사유 메시지 포함."""
class Planner:
    """맵 위 경로 계획기. NavGraph.shortest_path를 PlanStep 리스트로 변환."""
    def __init__(self, graph: NavGraph) -> None: ...
    def plan(self, from_state_id: str, goal_state_id: str) -> list[PlanStep]:
        """현재→목표 최단 전이열을 PlanStep(index/from/key/expected_to)로 생성. from==goal이면 빈 리스트.
        도달 불가면 PlanningError."""
```

### 3.6 `remotectl/engine/executor.py` (tier2)
UC-3 실행. 계획 실행 + 스텝별 관찰 검증 + 불일치 시 재계획(M2/M3). **추상 타입만 의존**.

```python
class Executor:
    """목표기반 실행기. driver/sense 추상 + graph + planner + goals 조합."""
    def __init__(self, driver: RemoteDriver, sense: ScreenSense, graph: NavGraph,
                 planner: Planner, settle_ms: int = 400, replan_budget: int = 5) -> None: ...
    def run_goal(self, goal_text: str) -> ExecutionResult:
        """resolve_goal→목표상태 확정(없으면 FAILED_UNRESOLVED)→현재상태 관찰 확정→plan→
        스텝별 [press→settle→capture→observe→actual 확정→matched 검증]. 불일치 시
        observe_transition(mismatch 반영) 후 현재상태 기준 재계획(replans++). 목표 도달=SUCCESS,
        재계획 예산 소진=FAILED_BUDGET, 도달불가=FAILED_UNREACHABLE, 드라이버/센스 오류=FAILED_DRIVER.
        모든 실패는 예외를 잡아 ExecutionResult로 정규화(예외를 밖으로 던지지 않음)."""
```

### 3.7 `remotectl/config.py` (tier0, deps만 소비)
```python
class Settings(BaseModel):
    """환경변수(REMOTECTL_*) 기반 런타임 설정. deps가 이 값으로 구현체를 고른다."""
    driver_backend: str = "mock"        # "mock" | "mcp"
    sense_backend: str = "mock"         # "mock" | "detection"
    remote_mcp_url: str = ""
    detection_mcp_url: str = ""
    map_store_path: str = "./data/navmap.json"
    settle_ms: int = 400
    learn_step_budget: int = 200
    exec_replan_budget: int = 5
def load_settings() -> Settings:
    """환경변수에서 Settings 로드. 미설정 항목은 기본값."""
```

### 3.8 `remotectl/api/deps.py` (tier3, 유일 wiring)
```python
def make_driver(s: Settings) -> RemoteDriver:
    """s.driver_backend에 따라 MockRemoteDriver 또는 RemoteMcpClient.from_env(base_url=...) 반환.
    여기가 구체 드라이버를 임포트하는 유일한 지점(M5)."""
def make_sense(s: Settings) -> ScreenSense:
    """s.sense_backend에 따라 MockScreenSense 또는 DetectionMcpScreenSense.from_env(...) 반환."""
def load_graph(s: Settings) -> NavGraph:
    """s.map_store_path에서 NavGraph.load."""
```

### 3.9 `remotectl/api/app.py` (tier3, FastAPI + 대시보드 + CLI)
FastAPI 앱 팩토리 + REST 라우트 + 자족 대시보드 서빙(R7: 외부 CDN 0, 인라인 자산) + CLI 엔트리.

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    """앱 팩토리. deps로 driver/sense/graph/planner/executor 조립하여 app.state에 보관하고 라우트 등록.
    라우트:
      GET  /health   드라이버/센스 가용성(DriverInfo/backend_name)
      POST /learn    UC-1 학습 세션 실행 → LearningSummary (요청: step_budget, coverage_target)
      GET  /map      UC-2 맵 조회(states/transitions/coverage)
      GET  /map/path?from=&to=  두 상태 간 최단경로(PlanStep 열)
      POST /goal     UC-3 목표 실행 → ExecutionResult (요청: text)
      GET  /         대시보드 HTML(인라인 CSS/JS, /map·/goal·/learn fetch)"""
app = create_app()  # uvicorn remotectl.api.app:app 진입점
def main(argv: list[str] | None = None) -> int:
    """CLI. 서브커맨드: learn --steps N / inspect / goal "<텍스트>" / serve --port P.
    pyproject [project.scripts] remotectl = "remotectl.api.app:main"."""
```

---

## 4. 엔드투엔드 흐름 (UC-1→2→3, Mock)

```
[UC-1 학습]  deps.make_driver(mock)+make_sense(mock)  →  Learner.learn(budget)
             reset → capture → observe → resolve_state(root 등록)
             루프: _select_key(미탐색 우선 BFS) → press → settle → capture → observe
                   → resolve_state → observe_transition  → 커버리지/예산 체크
             → LearningSummary + graph.save(map_store_path)
[UC-2 조회]  GET /map → NavMap(states/transitions) + coverage; 대시보드 SVG 렌더
[UC-3 실행]  POST /goal {"text":"넷플릭스 켜줘"}
             resolve_goal → Goal(OPEN_APP, app_id=netflix) → find_goal_state_id
             현재상태 관찰 확정 → Planner.plan → 스텝 실행·검증
             (flaky 전이면) 불일치 감지 → 재계획(replans++) → 목표 도달
             → ExecutionResult(SUCCESS, button_sequence, steps)
```

---

## 5. 파일별 담당 분배

- **데이터(완료)**: `models.py`
- **통합(완료)**: `drivers/base.py|mock.py|mcp_client.py`, `sense/base.py|mock.py|detection_mcp.py`,
  각 `__init__.py`, `tests/test_drivers_sense_contract.py`, `docs/INTEGRATION.md`
- **맵/식별 담당**: `navmap.py`, `identify.py`
- **엔진 담당**: `engine/__init__.py`, `engine/learner.py`, `engine/planner.py`, `engine/executor.py`, `goals.py`
- **API/CLI 담당**: `config.py`, `api/__init__.py`, `api/deps.py`, `api/app.py`, `api/static/index.html`
- **테스트 담당**: `tests/` (계약 테스트 + Mock 엔드투엔드 학습→맵→목표실행 + 아키텍처 임포트 검사)
- **패키징 담당**: `pyproject.toml`, 루트 `src/remotectl/__init__.py`(버전만; 무거운 재노출 금지), `README.md`

각 담당은 §2의 확정 시그니처를 소비만 하고 수정하지 않으며, §3의 계약대로 신규 파일을 작성한다.

---

## 6. 빌드/실행/테스트

- 설치: `python3.12 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`
  (pyproject 미비 시 임시: `pip install fastapi uvicorn "pydantic>=2" networkx httpx pytest`)
- 실행(API+대시보드): `uvicorn remotectl.api.app:app --port 8099` (editable 설치 시) 또는
  `PYTHONPATH=src uvicorn remotectl.api.app:app --port 8099`. CLI: `remotectl serve|learn|inspect|goal`.
- 테스트: `PYTHONPATH=src pytest -q` (또는 editable 설치 후 `pytest -q`).
