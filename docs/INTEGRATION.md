# 통합/인터페이스 계약 — RemoteDriver & ScreenSense (remotectl)

- 문서 버전: v1.0 (2026-07-07)
- 담당: 통합/인터페이스 계층
- 대상 파일:
  - `src/remotectl/drivers/` — `base.py`(RemoteDriver), `mock.py`(MockRemoteDriver), `mcp_client.py`(RemoteMcpClient)
  - `src/remotectl/sense/` — `base.py`(ScreenSense), `mock.py`(MockScreenSense), `detection_mcp.py`(DetectionMcpScreenSense)
  - `tests/test_drivers_sense_contract.py`

---

## 1. 설계 요지 — 왜 두 인터페이스로 쪼갰나

코어 엔진(학습/계획/실행)이 결합되면 안 되는 두 가지 축을 분리한다.

| 축 | 인터페이스 | 책임 | 모르는 것 |
|----|-----------|------|----------|
| 전송(무엇을 누르고, 화면을 어떻게 가져오나) | `RemoteDriver` | press / capture / reset | "화면이 어떤 상태인가" |
| 판정(화면이 어떤 상태인가) | `ScreenSense` | RawScreen → ScreenState | "그 화면을 어떻게 가져왔나" |

이 분리로 **Mock 드라이버 + 실제 VLM**, **실제 MCP 드라이버 + Mock 센스** 같은 조합이 자유롭다.
코어는 두 추상 타입만 임포트하므로 구현체 교체 시 코어 코드 변경 0줄(PRD M5).

데이터 흐름:

```
RemoteDriver.press(KeyPress)          # 부작용(리모컨 입력)
RemoteDriver.capture() -> RawScreen   # 화면 원재료(픽셀/텍스트/메타)
ScreenSense.observe(RawScreen) -> SenseResult(ScreenState)   # 정규화된 상태 판정
```

- `RawScreen`(drivers/base.py): 판정 전 원재료. `image_ref` / `image_bytes` / `text_hint` / `meta`.
- `SenseResult`(sense/base.py): `state`(ScreenState, id는 signature 파생) + `raw_signature` + `low_confidence`.
- 상태 정체성: `ScreenState.id = compute_state_id(normalize_signature(sig))`. 같은 화면 → 같은 id (PRD R2).

---

## 2. RemoteDriver 계약 (전송)

필수 추상 메서드(구현체는 이것만 채우면 코어 호환):

| 메서드 | 시그니처 | 계약 |
|--------|----------|------|
| `press` | `press(key: KeyPress) -> None` | 키 1회(`key.repeat` 반복) 입력. 판정 안 함. 실패 시 `PressError`(도달불가면 `DriverUnavailableError`). |
| `capture` | `capture() -> RawScreen` | 현재 화면 원재료 획득. 판정 안 함. 실패 시 `CaptureError` / `DriverUnavailableError`. |
| `reset` | `reset() -> None` | 알려진 시작점(HOME)으로 복귀. 세션 재현성. |
| `info` | `info() -> DriverInfo` | 자기소개(진단/대시보드). 부작용 없이 값싸게. |

기본 제공(오버라이드 선택): `settle(ms)`(입력 후 안정화 대기, PRD R3), `press_and_capture(...)`, `close()`, 컨텍스트 매니저.

예외 계층: `RemoteDriverError` ⊃ `DriverUnavailableError` / `PressError` / `CaptureError`.
엔진은 `RemoteDriverError` 만 잡아 `ExecutionStatus.FAILED_DRIVER` 로 매핑하면 된다(구체 예외에 결합 X).

### 구현체

- **MockRemoteDriver** — 결정적 가짜 STB. `default_stb_scenario()` = 홈↔런처(좌우)↔앱(netflix/youtube/settings) + 앱 단축키 지름길. 미정의 키 = self-loop. `flaky_transitions` 로 전이 비결정성 주입(재계획 테스트, PRD M3).
- **RemoteMcpClient** — 사내망 remote-MCP HTTP 어댑터(**스텁**). 아래 4절 참고.

---

## 3. ScreenSense 계약 (판정)

| 멤버 | 시그니처 | 계약 |
|------|----------|------|
| `observe` | `observe(raw: RawScreen) -> SenseResult` | 부작용 없이 판정. 백엔드 다운이면 `SenseUnavailableError`. |
| `backend_name` | `-> str` (property) | 백엔드 이름(진단). 예: `"mock"`, `"detection-mcp:qwen2.5vl:7b"`. |
| `confidence_threshold` | float | 이 값 미만 confidence → `low_confidence=True`(엔진이 재관찰 판단, PRD R2/R3). |

공통 헬퍼 `_build_result(...)` 가 정규화 → ScreenState 조립 → 임계값 처리를 한곳에서 하여 구현체 간 편차를 막는다.

### 구현체

- **MockScreenSense** — `RawScreen.text_hint`를 signature로, `meta`(label/kind/app_id)를 활용. 항상 고신뢰(1.0). MockRemoteDriver와 짝을 이루면 "같은 화면→같은 state id"가 결정론적으로 성립.
- **DetectionMcpScreenSense** — detection-mcp/VLM HTTP 어댑터(**스텁**). 아래 5절 참고.

---

## 4. 가정한 remote-MCP 계약 (RemoteMcpClient)

> **엔드포인트 미확정(PRD R1).** 아래는 *가정*이며, 실물 확정 시 코드의 `[WIRE-*]` 표식 지점만 채운다.

- 전송: HTTP/JSON, base URL = 환경변수 `REMOTE_MCP_URL`.
- 추정 대역: detection-mcp가 `172.16.3.136:8103` 이므로 remote-MCP도 `172.16.3.x` 로 추정.

| op | method + path | 요청 | 응답(가정) |
|----|---------------|------|-----------|
| press | `POST /press` | `{"key":"<CANONICAL>","repeat":N,"app":"<app|null>"}` | `{"ok":true}` |
| capture | `GET /capture` | — | `{"image_ref","image_b64","mime","text","meta"}` |
| reset | `POST /reset` | — | `{"ok":true}` |
| health | `GET /health` | — | `{"status":"ok","target":"<stb>"}` |

### 실물 배선 시 채워야 할 지점 (RemoteMcpClient)

| 표식 | 위치 | 채울 내용 |
|------|------|----------|
| `[WIRE-URL]` | `from_env()` | `REMOTE_MCP_URL` 실제 host:port/경로 프리픽스 |
| `[WIRE-PATHS]` | `press`/`capture`/`reset`/`info` | 각 op의 실제 method + path |
| `[WIRE-KEYMAP]` | `DEFAULT_KEYMAP`, `_map_key` | canonical `Button` → 실제 키 문자열(예: `KEY_HOME`, `0x0A`) |
| `[WIRE-PRESS]` | `press` | 요청 바디 스키마(단발 vs repeat 파라미터 vs N회 호출) / 앱 실행 방식 |
| `[WIRE-CAPTURE]` | `capture` | 응답 파싱(이미지 URL vs base64, 필드명) |
| `[WIRE-RESET]` | `reset` | 전용 op인가, HOME 다중 press 폴백인가 |
| `[WIRE-AUTH]` | `__init__` | 인증 헤더/스킴(`REMOTE_MCP_TOKEN` 예약, 현재 `Bearer` 가정) |
| `[WIRE-ERRORS]` | `_raise_if_not_ok` | 실패 표현(HTTP status vs `{"ok":false}`) → 예외 매핑 |

환경변수: `REMOTE_MCP_URL`(필수), `REMOTE_MCP_TOKEN`(옵션), `REMOTE_MCP_TIMEOUT`(옵션, 기본 5s).

---

## 5. 가정한 detection-mcp/VLM 계약 (DetectionMcpScreenSense)

> **실 판정 실연동은 후속(PRD 아웃오브스코프).** 아래는 *가정*이며 `[WIRE-*]` 만 채우면 배선 완료.

메모리 근거: detection-mcp `172.16.3.136:8103`; 로컬 VLM `qwen2.5vl:7b + 보정 프롬프트 = 실측 96%`;
최대 레버는 **프롬프트**(모델 크기 아님); `num_ctx 16k` 필수; 32b는 대상 Mac 비현실적.

- 전송: HTTP/JSON, base URL = 환경변수 `DETECTION_MCP_URL`.

| op | method + path | 요청 | 응답(가정) |
|----|---------------|------|-----------|
| classify | `POST /classify` | `{"image_ref","image_b64","text_hint","prompt","model"}` | `{"signature","label","kind","app_id","confidence"}` |

`kind` 라벨: `home|app|menu|settings|playback|live_tv|dialog|loading|unknown` (StateKind와 일치).

### 실물 배선 시 채워야 할 지점 (DetectionMcpScreenSense)

| 표식 | 위치 | 채울 내용 |
|------|------|----------|
| `[WIRE-URL]` | `from_env()` | `DETECTION_MCP_URL` host:port/경로 |
| `[WIRE-PATHS]` | `observe` | classify op의 method + path |
| `[WIRE-REQ]` | `observe` | 이미지 전달 방식(URL vs base64), 프롬프트/모델 파라미터명 |
| `[WIRE-RES]` | `observe`/`_map_kind`/`_coerce_confidence` | 응답 필드명, kind 라벨 매핑, confidence 스케일(0~1 vs 0~100) |
| `[WIRE-PROMPT]` | `CALIBRATION_PROMPT` | 보정 프롬프트 본문(정확도 최대 레버) |
| `[WIRE-AUTH]` | `__init__` | 인증(`DETECTION_MCP_TOKEN` 예약) |

환경변수: `DETECTION_MCP_URL`(필수), `DETECTION_MCP_MODEL`(옵션, 기본 `qwen2.5vl:7b`), `DETECTION_MCP_TOKEN`(옵션), `DETECTION_MCP_TIMEOUT`(옵션, 기본 30s).

---

## 6. 조립 예시 (엔진/조립 지점 담당자 참고)

```python
from remotectl.drivers import MockRemoteDriver, RemoteMcpClient
from remotectl.sense import MockScreenSense, DetectionMcpScreenSense

# 개발/테스트(실물 없이 완결, PRD M1)
driver = MockRemoteDriver()
sense = MockScreenSense()

# 실물(배선 후) — 코어 코드는 그대로, 조립만 교체(M5)
# driver = RemoteMcpClient.from_env()
# sense = DetectionMcpScreenSense.from_env()

driver.reset()
raw = driver.press_and_capture(key, settle_ms=800)   # press -> settle -> capture
result = sense.observe(raw)                            # -> SenseResult(state=ScreenState)
```

엔진은 `RemoteDriver`/`ScreenSense` 추상 타입만 파라미터로 받는다(구현체 선택은 조립 지점).

---

## 7. 테스트

`tests/test_drivers_sense_contract.py` (19 케이스, 모두 통과):
- 계약: 두 인터페이스의 추상 메서드 구현/타입.
- Mock 정합: MockDriver+MockSense가 "같은 화면→같은 id" 결정론 보장, 앱 단축키/repeat/self-loop/flaky.
- HTTP 어댑터: `httpx.MockTransport` 로 가정한 MCP 계약 JSON 파싱, 도달불가→`*UnavailableError` 승격, `from_env` URL 필수.

> 실행에는 `pydantic>=2`, `httpx`, `pytest` 필요(패키지 의존성은 별도 담당). `PYTHONPATH=src pytest tests/`.
