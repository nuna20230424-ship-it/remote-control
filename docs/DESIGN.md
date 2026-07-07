# remotectl 운영자 대시보드 설계 (DESIGN.md)

STB 리모컨 학습 에이전트의 운영자용 단일 페이지 웹 대시보드 설계 정본.
FastAPI가 `GET /`로 서빙하는 자족(self-contained) SPA이며, 외부 CDN 의존 0(R7),
바닐라 JS + `fetch`로 REST를 호출한다. 이 문서는 디자인 시스템과 페이지/컴포넌트,
그리고 각 상호작용이 소비하는 API 계약(§ARCHITECTURE 3.9)을 확정한다.

> 산출 정적 파일(구현 대상, 절대경로):
> - `/Users/keonhee/dev/remote-control/src/remotectl/api/static/index.html`
> - `/Users/keonhee/dev/remote-control/src/remotectl/api/static/styles.css`
> - `/Users/keonhee/dev/remote-control/src/remotectl/api/static/app.js`
>
> ARCHITECTURE.md §3.9는 대시보드 진입 파일을 `api/static/index.html`로 확정했다.
> 자산은 위 3파일로 분리하되, R7(외부 CDN 0)을 지키기 위해 `styles.css`/`app.js`는
> 같은 static 마운트에서 **동일 출처 상대경로**(`./styles.css`, `./app.js`)로만 로드한다.
> 폰트 CDN·아이콘 폰트·차트 라이브러리를 일절 링크하지 않는다(그래프는 자체 Canvas/SVG로 그린다).

---

## 0. 설계 관점 — 이건 문서가 아니라 계기판이다

운영자는 이 화면을 위에서 아래로 "읽지" 않는다. 학습을 돌리고, 맵이 자라는 걸 지켜보고,
목표를 던지고, 실행이 어디서 어긋났는지 훑는다. 그래서 타이포그래피보다 **정보 설계**가 우선이다:

- **요약을 먼저, 상세는 나중에.** 상단 상태 바(백엔드 가용성·상태수·전이수·커버리지)가
  한눈에 "지금 시스템이 어떤 상태인지"를 답한다.
- **상태를 숫자만이 아니라 형태로 인코딩한다.** pill/chip/신뢰도 막대/전이 성공률 스트라이프로,
  주의가 필요한 것(저신뢰 상태, 미탐색 간선, 재계획, 드라이버 도달불가)이 한눈에 읽히게 한다.
- **의미색은 액센트색과 분리한다.** good/warn/critical(성공·경고·실패)은 액센트 신호색과 별개다.
- **상호작용 요소는 상호작용처럼 보인다.** 버튼·입력·노드는 hover/active/focus 상태를 명시한다.

이 제품의 세계는 **방송 엔지니어링 콘솔**이다 — 사내망 셋탑박스에 신호를 쏘고, 화면 전이를
관측해 신호 경로 지도를 그린다. 그래서 AI 기본값(크림+세리프, 보라→파랑 그라디언트 히어로)을
피하고, **어두운 계기판 위의 인광(phosphor) 신호** 정체성을 택한다. 그리드는 오실로스코프의
미세 격자, 액센트는 신호가 통과하는 청록(신호 통과)과 상태 전이를 나타내는 호박(hold/변화)이다.

---

## 1. 디자인 시스템

### 1.1 색 (Color)

계기판 그라운드는 순수 검정이 아니라 **청록으로 살짝 치우친 딥 슬레이트**다(선택된 중립).
액센트는 한 곳에 집중한다: 신호 청록(signal). 상태 의미색은 이와 분리한다.

| 토큰 | HEX | 용도 |
|---|---|---|
| `--bg` | `#0B1013` | 앱 그라운드(청록 편향 near-black). 순수 #000 아님 — 선택된 중립 |
| `--panel` | `#111A1F` | 패널/카드 표면 |
| `--panel-2` | `#16232A` | 패널 내부 융기(입력, 노드, 로그 행 hover) |
| `--line` | `#22333B` | 1px 경계선·오실로스코프 격자선 |
| `--ink` | `#DCE7EA` | 본문 텍스트(청록 편향 오프화이트) |
| `--ink-dim` | `#7C929B` | 보조 텍스트·라벨·캡션 |
| `--signal` | `#39E0C4` | **단일 액센트**: 신호 청록. 활성 노드·주요 CTA·진행·엣지 강조 |
| `--signal-soft` | `#1C4A46` | 액센트의 저채도 배경(칩 배경·진행 트랙) |
| `--good` | `#5BD97B` | 의미색: 성공·matched 전이·resolved 목표 |
| `--warn` | `#E7B24A` | 의미색: 저신뢰 상태·미탐색 간선·재계획 발생(hold/호박) |
| `--crit` | `#F0605C` | 의미색: 실패·드라이버 도달불가·mismatch |

원칙: 액센트(`--signal`)는 "지금 신호가 흐르는 곳" 한 곳에만 쓴다. 액센트가 그라운드와
싸우면 채도를 낮춘 `--signal-soft`로 물러선다. good/warn/crit은 액센트가 아니라 상태 전달용이다.

### 1.2 타이포그래피 (Type)

폰트 CDN을 링크하지 않는다(CSP·R7). 시스템 폰트 스택으로 안전하게 떨어지되, 역할을 분리한다.
계기판 제품이므로 **데이터·토큰·상태 id는 반드시 등폭(mono)**으로 — 리모컨 토큰(`RIGHT*3`,
`APP_SHORTCUT:netflix`)과 상태 id(`st_…`)가 정렬되어 읽히는 것이 정보 설계의 핵심이다.

- **UI/본문 (sans):** `system-ui, -apple-system, "Segoe UI", "Pretendard", "Apple SD Gothic Neo", sans-serif`
  — 한글(운영자 라벨·안내)까지 자연스럽게 커버.
- **데이터/토큰/코드 (mono):** `"SF Mono", "JetBrains Mono", "Cascadia Code", ui-monospace, Menlo, Consolas, monospace`
  — 키 토큰, 상태 id, 신뢰도 수치, 타임스탬프.
- **타입 스케일(1.25 비율):** 12 / 13 / 15 / 18 / 22 / 28 px. 본문 15, 캡션 12–13.
- 대문자 라벨(패널 헤더·칼럼 헤더)은 `letter-spacing: .08em`, `--ink-dim`, 12px.
- 숫자가 열을 이루는 곳(로그 seq, 신뢰도, 스텝 index)은 `font-variant-numeric: tabular-nums`.
- 제목은 `text-wrap: balance`.

### 1.3 레이아웃 (Layout)

**고정 상단 상태 바 + 2열 워크벤치.** 상단은 시스템 요약(백엔드 상태·맵 규모·커버리지)을
항상 보이게 고정. 그 아래는 좌측 **조작 레일**(학습 제어·목표 입력, 폭 고정 ~360px),
우측 **관측 캔버스**(맵 그래프가 지배적, 그 아래 실행 결과·관측 로그). 1024px 미만에서는
1열로 스택되고, 좌측 레일이 위로 올라온다.

- 8px 베이스 그리드. 패널 간 `gap: 16px`, 패널 내부 패딩 16–20px.
- 그라운드에 **오실로스코프 미세 격자**(4% 불투명 `--line`, 24px 반복)를 CSS `background`로
  깔아 계기판 질감을 준다 — 장식이지만 절제한다.
- 넓은 콘텐츠(맵 그래프, 관측 로그 테이블)는 각자 `overflow: auto` 컨테이너 안에서만 스크롤.
  페이지 본문은 절대 가로 스크롤하지 않는다.
- 모서리는 절제된 4–8px. `rounded-lg` 남발(AI 기본값) 회피.

### 1.4 모션 (Motion)

계기판이므로 모션은 **신호 피드백**에만 쓴다. 과하면 AI스러워진다.

- 학습 진행 중 상단 상태 바의 신호 청록 인디케이터가 은은히 맥동(2s ease).
- 새 노드/엣지가 맵에 추가되면 200ms 페이드+스케일 인.
- 실행 스텝이 매칭되면 해당 스텝 행에 good 스트라이프가 좌→우로 스윕(180ms).
- `@media (prefers-reduced-motion: reduce)`: 모든 전환 0ms, 맥동 정지.

### 1.5 상태 인코딩 규약 (형태로 읽히는 상태)

| 개념 | 형태 인코딩 |
|---|---|
| 백엔드 가용(ready) | 좌측 점: `--good` / 도달불가: `--crit` / 미확인: `--ink-dim` |
| 상태 종류(StateKind) | 노드 색 힌트 칩: home=signal, app=good, menu/settings=ink-dim, playback=warn, dialog/loading=warn, unknown=line |
| 신뢰도(confidence) | 노드 하단 4px 막대(0–1 폭), <0.5는 `--warn` 테두리 링 |
| 전이 성공률 | 엣지 두께(observed_count)와 색(success/observed 비율: 높음=good, 낮음=warn) |
| 미탐색 간선 | 상단 배지 `미탐색 N`(`--warn`), 맵에서 해당 노드에 점선 스텁 |
| 커버리지(coverage_ratio) | 상단 진행 링/막대(signal), 수치 mono |
| 실행 스텝 matched | 행 좌측 스트라이프: matched=good, mismatch=crit, 미실행=line |
| 재계획(replans) | 결과 헤더 배지 `재계획 N`(N>0이면 `--warn`) |
| 실행 status | pill: success=good, failed_*=crit, unresolved/budget=warn |

---

## 2. 페이지 / 컴포넌트

단일 페이지(SPA). 논리적으로 4개 작업 영역 — (A) 시스템 요약 바, (B) 조작 레일(학습+목표),
(C) 맵 캔버스, (D) 실행·관측 결과. 각 영역이 소비하는 API를 함께 명시한다.

### A. 상단 시스템 상태 바 (`#statusbar`)
항상 고정. 폴링/수동 새로고침으로 갱신.
- 제품 마크 `remotectl` + 부제 "STB 리모컨 학습 에이전트".
- **백엔드 칩 2개**: 드라이버(`DriverInfo.name/target/ready`)·센스(`backend_name`) — good/crit 점.
- **맵 요약 카운터**: 상태 `state_count`, 전이 `transition_count`, 커버리지 `coverage`(진행 막대+mono %).
- **미탐색 배지**: `unexplored_edges`(warn, 0이면 회색).
- 우측 학습 라이브 인디케이터(맥동 점 + `LEARNING…` / `IDLE`).
- API: `GET /health`(백엔드), `GET /map`(카운터·커버리지).

### B-1. 학습 제어 카드 (`#learn-panel`)
UC-1. 좌측 레일 상단.
- 입력: `step_budget`(number, 기본 200), `coverage_target`(0–1 slider+수치, 기본 0.9).
- 주 버튼 **학습 시작**(signal, primary). 실행 중이면 스피너+**중지**(secondary, crit 테두리).
- 진행 영역: 스텝 진행 막대, "관측 N · 상태 N · 전이 N" 라이브 텍스트.
- 종료 시 `LearningSummary` 요약 카드(steps_taken/states_visited/transitions_recorded/
  unexplored_edges/coverage_ratio/stop_reason). coverage_ratio는 링으로.
- API: `POST /learn {step_budget, coverage_target}` → `LearningSummary`. 완료 후 자동 `GET /map` 리프레시.
- 주: `/learn`은 동기 블로킹일 수 있으므로 UI는 "요청 중" 상태로 잠그고, 완료 응답으로 요약을 채운다.
  (서버가 후속에 진행 스트리밍/SSE를 제공하면 진행 막대를 라이브 연결로 승격 — 그 전까진 낙관적 표시.)

### B-2. 목표 실행 카드 (`#goal-panel`)
UC-3. 좌측 레일 하단.
- 텍스트 입력 `goal-text`(placeholder "예: 넷플릭스 켜줘"), 빠른 칩(넷플릭스/유튜브/설정/홈으로) → 입력 채움.
- 버튼 **실행**(signal). 실행 중 잠금.
- **계획 미리보기**: 응답의 `steps`(PlanStep 열)를 실행 전/후 모두 스텝 리스트로 표시
  (index · from → key → expected_to). from==goal이면 "이미 목표 상태" 안내.
  ※ 현 계약상 `POST /goal`은 실행까지 수행해 `ExecutionResult`를 돌려준다(계획+실행 통합).
  미리보기는 `GET /map/path?from=&to=`로도 별도 확인 가능(선택적 "경로만 보기" 링크).
- **결과 요약**: status pill, `button_sequence`(mono 토큰 칩 열), `step_count`, `replans` 배지,
  start→final 상태, message. 실패 사유(unresolved: `Goal.resolve_note`)를 crit 박스로.
- API: `POST /goal {text}` → `ExecutionResult`; (선택) `GET /map/path` → `PlanStep[]`.

### C. 네비게이션 맵 캔버스 (`#map-canvas`)
UC-2. 우측 지배 영역. **자체 렌더링**(라이브러리 없음): SVG(엣지=path, 노드=group) 또는 Canvas.
- 레이아웃: 간단한 힘-기반/계층(root 위쪽) 배치. root_state_id는 상단 고정·특별 표시.
- **노드 = 화면 상태(ScreenState)**: 라벨(없으면 kind), StateKind 색 칩, 신뢰도 막대,
  visit_count 배지, 저신뢰(<0.5)면 warn 링. 클릭 시 상세 팝오버(id·signature·app_id·first/last_seen).
- **엣지 = 전이(Transition)**: 화살표, 키 토큰 라벨(`key.token`), 두께=observed_count,
  색=success/observed 비율. 현재 실행 경로/계획 경로는 signal로 하이라이트(펄스).
- 도구: 확대/축소·리셋 뷰·라벨 토글·저신뢰만 보기 필터·검색(라벨/앱). 빈 맵이면 "아직 학습된 화면이
  없습니다. 좌측에서 학습을 시작하세요." 엠프티 스테이트.
- API: `GET /map` → `NavMap`(states/transitions/root_state_id) + coverage. 학습/목표 후 자동 리프레시.
- 성능: 노드 수백 규모까지 SVG로 충분. 그 이상이면 Canvas 폴백(문서상 명시, 1차는 SVG).

### D-1. 실행 스텝 트레이스 (`#exec-trace`)
맵 아래. 최근 목표 실행의 스텝별 검증 결과.
- 행 = PlanStep: index · from → `key.token` → expected → actual · matched 스트라이프(good/crit/line)
  · observed_confidence(mono 막대). 재계획으로 갱신된 궤적을 순서대로.
- 헤더에 status pill·replans 배지·소요(finished-started). 비었으면 "실행 이력 없음".
- 데이터원: 직전 `POST /goal` 응답(`ExecutionResult.steps`). 클라이언트 상태로 보관.

### D-2. 관측 로그 (`#obs-log`)
학습·실행 중 발생한 이벤트의 append-only 원장 뷰.
- 테이블: 시각(mono) · 종류(LEARN/EXEC) · seq · from → key → to · signature(말줄임) · 신뢰도(mono, 저신뢰 warn).
- 필터(LEARN/EXEC/저신뢰만), 자동 스크롤 토글, 최대 N행 링 버퍼(오래된 행 폐기), "지우기".
- 데이터원: 1차는 클라이언트가 `/learn`·`/goal` 응답에서 파생해 채운다(Observation 모델 형태).
  서버가 관측 스트림 엔드포인트를 제공하면 그때 라이브 연결로 승격(문서상 확장점 명시).
- `overflow-y: auto` 자체 스크롤. 페이지 본문 가로 스크롤 금지 → 테이블은 `overflow-x: auto` 래퍼 안.

### 공통 컴포넌트 (재사용)
- `Panel`(제목 대문자 라벨 + 본문), `Pill`(status/kind), `Chip`(토큰·앱), `MeterBar`(신뢰도/커버리지),
  `Ring`(coverage_ratio), `Button`(primary/secondary/ghost + disabled/loading),
  `Toast`(작업 결과: "학습 완료 · 상태 6 · 전이 11" / 오류), `EmptyState`, `KeyToken`(mono 칩).
- 모든 상호작용 요소 `:focus-visible` 링(signal, 2px). 키보드 조작 가능.

---

## 3. 접근성·견고성 체크

- 색만으로 상태를 전달하지 않는다: pill/스트라이프에 텍스트 라벨 병기(success/실패/저신뢰).
- 명도 대비: `--ink`/`--bg` ≈ 13:1, `--ink-dim`/`--bg` ≈ 5:1(라벨 최소 통과).
- `prefers-reduced-motion` 존중(맥동·스윕 정지).
- 모든 `fetch` 실패는 Toast로 사람이 읽는 오류를 표시("드라이버 도달불가: MCP 미응답" 등),
  콘솔에만 삼키지 않는다. 네트워크 오류 시 해당 카드에 재시도 버튼.
- 자산은 동일 출처 상대경로만(외부 CDN 0, R7). 이미지 필요 시 data URI 인라인.

---

## 4. API 소비 요약 (§ARCHITECTURE 3.9 계약)

| UI 영역 | 메서드/경로 | 요청 | 응답(모델) |
|---|---|---|---|
| 상태 바(백엔드) | `GET /health` | — | 드라이버/센스 가용성(DriverInfo/backend_name) |
| 상태 바(맵 요약)·맵 캔버스 | `GET /map` | — | NavMap(states/transitions/root)+coverage |
| 학습 카드 | `POST /learn` | `{step_budget, coverage_target}` | LearningSummary |
| 목표 카드(경로 미리보기) | `GET /map/path` | `?from=&to=` | PlanStep[] |
| 목표 카드(실행) | `POST /goal` | `{text}` | ExecutionResult |

모든 응답 모델의 필드는 `models.py` 정본을 그대로 사용한다(클라이언트에서 임의 필드 신설 금지).
`ExecutionResult.button_sequence`/`step_count`는 서버 computed 필드이므로 클라이언트는 재계산하지 않고 표시만 한다.
