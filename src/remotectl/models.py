"""remotectl 핵심 데이터 모델 (Pydantic v2).

이 모듈은 학습 에이전트 전 구간에서 공유되는 "정본(canonical) 데이터 계약"을 정의한다.

설계 원칙
---------
- 순수 데이터 계층: 여기에는 I/O(HTTP·파일)·networkx·FastAPI 의존성을 두지 않는다.
  드라이버(RemoteDriver)·센스(ScreenSense)·엔진·API 레이어는 모두 이 모델만 주고받는다.
- 상태 정체성은 내용 주소화(content-addressable): ScreenState.id 는 signature 로부터
  결정론적으로 파생된다. 같은 화면 관찰은 같은 id 로 수렴하고, 맵 오염을 줄인다(R2 완화).
- 그래프(networkx)는 "런타임 뷰"일 뿐 정본이 아니다. 영속화의 정본은 NavMap 의
  states/transitions 리스트다. 저장/로드는 model_dump(mode="json") / model_validate 로
  왕복(round-trip)한다. 이로써 그래프 라이브러리 교체가 저장 포맷에 영향을 주지 않는다.

직렬화 전략 (맵 저장/로드)
--------------------------
- 저장:   NavMap.model_dump(mode="json")  ->  json.dumps  (또는 NavMap.dump_json()).
- 로드:   json.loads  ->  NavMap.model_validate(...)      (또는 NavMap.load_json()).
- Enum 은 값(문자열)으로, datetime 은 ISO-8601 문자열로 직렬화된다(mode="json").
- 스키마 진화 대비: NavMap.schema_version 을 두고, 알 수 없는 필드는 무시(ignore)한다.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------- #
# 공통 타입/유틸
# --------------------------------------------------------------------------- #

# 상태 정체성의 근거가 되는 화면 서명(signature). VLM 라벨/해시/OCR 등을 정규화한 문자열.
Signature = Annotated[str, Field(min_length=1, max_length=512)]

# 0.0~1.0 신뢰도.
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


def _utcnow() -> datetime:
    """timezone-aware UTC now. (naive datetime 혼입으로 인한 비교 버그 방지)"""
    return datetime.now(timezone.utc)


class _StrictModel(BaseModel):
    """프로젝트 공통 베이스.

    - extra="ignore": 저장 포맷 진화 시 구버전 코드가 신버전 파일을 로드해도 깨지지 않음.
    - validate_assignment=True: 런타임 필드 재할당도 검증(엔진이 상태를 갱신하므로 중요).
    - use_enum_values=False: 코드 안에서는 Enum 인스턴스로 다루고, 직렬화 때만 값으로 변환.
    - ser_json_timedelta / datetime 은 mode="json" 시 ISO 문자열.
    """

    model_config = ConfigDict(
        extra="ignore",
        validate_assignment=True,
        use_enum_values=False,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


# --------------------------------------------------------------------------- #
# 1. 버튼 / 키 입력
# --------------------------------------------------------------------------- #


class Button(str, Enum):
    """리모컨의 정규(canonical) 버튼 집합.

    사업자·단말별 물리 키코드 매핑은 RemoteDriver 어댑터 내부에서 흡수한다(코어는 이 심볼만 안다).
    앱 단축키처럼 열거 불가능한 키는 KeyPress.app_shortcut 로 표현한다(APP_SHORTCUT 참조).
    """

    # 내비게이션
    HOME = "HOME"
    BACK = "BACK"
    UP = "UP"
    DOWN = "DOWN"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    OK = "OK"  # 선택/확인(ENTER)
    MENU = "MENU"
    EXIT = "EXIT"

    # 미디어
    PLAY_PAUSE = "PLAY_PAUSE"
    STOP = "STOP"
    REWIND = "REWIND"
    FAST_FORWARD = "FAST_FORWARD"

    # 볼륨/채널
    VOL_UP = "VOL_UP"
    VOL_DOWN = "VOL_DOWN"
    MUTE = "MUTE"
    CH_UP = "CH_UP"
    CH_DOWN = "CH_DOWN"

    # 전원
    POWER = "POWER"

    # 숫자
    NUM_0 = "NUM_0"
    NUM_1 = "NUM_1"
    NUM_2 = "NUM_2"
    NUM_3 = "NUM_3"
    NUM_4 = "NUM_4"
    NUM_5 = "NUM_5"
    NUM_6 = "NUM_6"
    NUM_7 = "NUM_7"
    NUM_8 = "NUM_8"
    NUM_9 = "NUM_9"

    # 앱 단축키 "종류" 표식(구체 앱은 KeyPress.app_shortcut 로 지정).
    APP_SHORTCUT = "APP_SHORTCUT"


class KeyPress(_StrictModel):
    """실제로 누른 키 한 번.

    Transition/Observation/PlanStep 이 공통으로 참조하는 "행위" 단위.
    - button: 정규 버튼.
    - app_shortcut: button==APP_SHORTCUT 일 때만 유효한 앱 식별자(예: "netflix", "youtube").
    - repeat: 연속 입력 횟수(예: UP x3). 기본 1.
    """

    button: Button
    app_shortcut: Optional[str] = Field(
        default=None,
        max_length=64,
        description="button==APP_SHORTCUT 일 때 눌린 앱 단축키 식별자(소문자 권장).",
    )
    repeat: int = Field(default=1, ge=1, le=50, description="연속 반복 입력 횟수.")

    @model_validator(mode="after")
    def _check_app_shortcut(self) -> "KeyPress":
        if self.button is Button.APP_SHORTCUT and not self.app_shortcut:
            raise ValueError("APP_SHORTCUT 버튼은 app_shortcut 값이 필요합니다.")
        if self.button is not Button.APP_SHORTCUT and self.app_shortcut:
            raise ValueError("app_shortcut 은 button==APP_SHORTCUT 에서만 허용됩니다.")
        return self

    @property
    def token(self) -> str:
        """전이 키로 쓰는 안정적 문자열 표현. (그래프 엣지 라벨/사전 키 용도)

        예: "UP", "UP*3", "APP_SHORTCUT:netflix".
        """
        base = self.button.value
        if self.button is Button.APP_SHORTCUT:
            base = f"{base}:{self.app_shortcut}"
        return base if self.repeat == 1 else f"{base}*{self.repeat}"

    def __hash__(self) -> int:  # dict/set 키로 사용 가능하게
        return hash(self.token)


# --------------------------------------------------------------------------- #
# 2. 화면 상태
# --------------------------------------------------------------------------- #


class StateKind(str, Enum):
    """상태 종류 분류(정책·목표 해석 힌트)."""

    HOME = "home"
    APP = "app"
    MENU = "menu"
    SETTINGS = "settings"
    PLAYBACK = "playback"
    LIVE_TV = "live_tv"
    DIALOG = "dialog"
    LOADING = "loading"
    UNKNOWN = "unknown"


def compute_state_id(signature: str) -> str:
    """signature 로부터 결정론적 상태 id 파생.

    같은 서명 -> 같은 id. 상태 병합/식별의 기준(R2 완화). 짧고 안정적인 16자 hex.
    """
    digest = hashlib.sha1(signature.strip().encode("utf-8")).hexdigest()
    return f"st_{digest[:16]}"


class ScreenState(_StrictModel):
    """네비게이션 맵의 노드 = 하나의 식별된 화면 상태.

    - id: signature 파생(compute_state_id). 명시하지 않으면 자동 계산.
    - signature: 상태 정체성의 근거(정규화된 VLM 라벨/해시/OCR 조합 문자열).
    - label: 사람이 읽는 라벨(예: "홈 화면", "넷플릭스 앱"). VLM/규칙이 채운다.
    - kind: 상태 종류(홈/앱/메뉴/재생/미상 등) — 목표 해석·정책에서 활용.
    - app_id: 앱 상태일 때의 앱 식별자(예: "netflix"). 목표 매핑의 핵심 키.
    - screenshot_ref: 스크린샷 참조(파일경로/URL/blob id). 원본 이미지는 여기 두지 않음.
    - confidence: 이 상태 식별의 대표 신뢰도(저신뢰는 재관찰 대상).
    - visit_count: 학습 중 방문 횟수(커버리지·정책 통계용).
    """

    id: str = Field(default="", description="signature 파생 id. 비우면 자동 계산.")
    signature: Signature
    label: Optional[str] = Field(default=None, max_length=200)
    kind: StateKind = Field(default=StateKind.UNKNOWN)
    app_id: Optional[str] = Field(
        default=None, max_length=64, description="앱 상태일 때 앱 식별자(소문자)."
    )
    screenshot_ref: Optional[str] = Field(
        default=None, max_length=1024, description="스크린샷 참조(경로/URL/blob id)."
    )
    confidence: Confidence = 1.0
    visit_count: int = Field(default=0, ge=0)
    first_seen: datetime = Field(default_factory=_utcnow)
    last_seen: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _fill_id(self) -> "ScreenState":
        if not self.id:
            # validate_assignment 재검증 루프 방지를 위해 object.__setattr__ 대신 직접 대입
            self.__dict__["id"] = compute_state_id(self.signature)
        return self


# --------------------------------------------------------------------------- #
# 3. 관측(Observation) — 학습 기록 한 건
# --------------------------------------------------------------------------- #


class Observation(_StrictModel):
    """학습 루프에서 발생한 이벤트 한 건의 원장(ledger) 기록.

    "from_state 에서 key 를 눌러 settle 후 관찰하니 to_state 였다"는 인과 한 건.
    - 세션 최초 관찰(입력 없이 현재 화면 확인)은 key=None, from_state_id=None 로 기록.
    - sensed_signature/sensed_confidence 는 ScreenSense 원시 결과(정규화 전/후 추적용).
    """

    session_id: str = Field(min_length=1)
    seq: int = Field(ge=0, description="세션 내 순번(0부터).")
    from_state_id: Optional[str] = Field(
        default=None, description="입력 직전 상태 id. 세션 첫 관찰이면 None."
    )
    key: Optional[KeyPress] = Field(
        default=None, description="누른 키. 첫 관찰(입력 없음)이면 None."
    )
    to_state_id: str = Field(min_length=1, description="관찰로 확정된 결과 상태 id.")
    sensed_signature: Signature
    sensed_confidence: Confidence = 1.0
    settle_ms: int = Field(default=0, ge=0, description="입력 후 안정화 대기 시간(ms).")
    timestamp: datetime = Field(default_factory=_utcnow)
    note: Optional[str] = Field(default=None, max_length=500)


# --------------------------------------------------------------------------- #
# 4. 전이(Transition) — 맵의 엣지
# --------------------------------------------------------------------------- #


class Transition(_StrictModel):
    """방향 간선: (from_state, key) -> to_state.

    같은 (from, key) 가 여러 to 로 관측될 수 있으므로(비결정성, R3) 전이는
    (from_state_id, key.token, to_state_id) 3-튜플로 유일하다. 동일 3-튜플 반복 관측은
    observed_count 를 올리고 confidence 를 갱신한다.
    - observed_count: 이 전이가 관측된 횟수.
    - success_count: 계획 실행 중 이 전이가 예측대로 일어난 횟수(재계획 통계용).
    - confidence: observed 기반 신뢰도(엔진이 갱신; 동일 from,key 내 상대빈도로 해석 가능).
    """

    from_state_id: str = Field(min_length=1)
    key: KeyPress
    to_state_id: str = Field(min_length=1)
    observed_count: int = Field(default=1, ge=1)
    success_count: int = Field(default=0, ge=0)
    confidence: Confidence = 1.0
    first_observed: datetime = Field(default_factory=_utcnow)
    last_observed: datetime = Field(default_factory=_utcnow)

    @computed_field  # 직렬화에 포함되는 파생 키(디버깅/그래프 엣지 식별용)
    @property
    def edge_key(self) -> str:
        """전이 유일 키: "<from>|<token>|<to>"."""
        return f"{self.from_state_id}|{self.key.token}|{self.to_state_id}"


# --------------------------------------------------------------------------- #
# 5. 네비게이션 맵(NavMap) — 노드/엣지 컨테이너 (영속화 정본)
# --------------------------------------------------------------------------- #


class NavMap(_StrictModel):
    """상태 그래프의 정본(source of truth).

    networkx 그래프는 이 리스트로부터 재구성하는 런타임 뷰다(맵 모듈이 담당).
    직렬화는 아래 dump_json/load_json 또는 model_dump(mode="json") 으로 왕복한다.

    - states: state_id -> ScreenState.
    - transitions: 전이 목록(순서 보존; edge_key 로 유일).
    - root_state_id: 학습 시작(홈) 상태 힌트.
    - schema_version: 저장 포맷 버전(진화 대비).
    """

    schema_version: Literal[1] = 1
    map_id: str = Field(default="default", min_length=1, max_length=64)
    states: dict[str, ScreenState] = Field(default_factory=dict)
    transitions: list[Transition] = Field(default_factory=list)
    root_state_id: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("states", mode="after")
    @classmethod
    def _keys_match_ids(cls, v: dict[str, ScreenState]) -> dict[str, ScreenState]:
        for key, state in v.items():
            if key != state.id:
                raise ValueError(
                    f"states 딕셔너리 키({key})와 ScreenState.id({state.id})가 불일치합니다."
                )
        return v

    # --- 편의 접근자 (엔진/그래프 모듈이 사용; 순수 조회이므로 여기 둔다) ---

    def get_state(self, state_id: str) -> Optional[ScreenState]:
        return self.states.get(state_id)

    def outgoing(self, state_id: str) -> list[Transition]:
        """해당 상태에서 나가는 전이 목록."""
        return [t for t in self.transitions if t.from_state_id == state_id]

    def state_count(self) -> int:
        return len(self.states)

    def transition_count(self) -> int:
        return len(self.transitions)

    # --- 직렬화 정본 API ---

    def dump_json(self, *, indent: int | None = 2) -> str:
        """맵을 JSON 문자열로 직렬화(저장). Enum=값, datetime=ISO-8601."""
        return self.model_dump_json(indent=indent)

    @classmethod
    def load_json(cls, data: str | bytes) -> "NavMap":
        """JSON 문자열/바이트에서 맵 복원(로드). 알 수 없는 필드는 무시."""
        return cls.model_validate_json(data)


# --------------------------------------------------------------------------- #
# 6. 목표(Goal) / 계획 스텝(PlanStep) / 실행 결과(ExecutionResult)
# --------------------------------------------------------------------------- #


class GoalType(str, Enum):
    """목표 해석 결과의 종류."""

    OPEN_APP = "open_app"  # 특정 앱 실행 (예: 넷플릭스 켜줘)
    GOTO_STATE = "goto_state"  # 특정 상태 id 로 이동
    GOTO_KIND = "goto_kind"  # 특정 종류 상태(예: 홈)로 이동


class Goal(_StrictModel):
    """자연어에서 해석된 목표.

    1차는 규칙 기반 해석(별칭 사전). raw_text 는 원문, 나머지는 해석 산출물.
    - goal_type 에 따라 target_app_id / target_state_id / target_kind 중 하나가 채워진다.
    - resolved: 목표 상태를 실제 맵 상태로 확정했는지 여부(미학습이면 False).
    """

    raw_text: str = Field(min_length=1, max_length=500)
    goal_type: GoalType
    target_app_id: Optional[str] = Field(default=None, max_length=64)
    target_state_id: Optional[str] = None
    target_kind: Optional[StateKind] = None
    resolved: bool = False
    resolve_note: Optional[str] = Field(
        default=None, max_length=500, description="미해석/미학습 시 사유."
    )

    @model_validator(mode="after")
    def _check_target_present(self) -> "Goal":
        mapping = {
            GoalType.OPEN_APP: self.target_app_id,
            GoalType.GOTO_STATE: self.target_state_id,
            GoalType.GOTO_KIND: self.target_kind,
        }
        if mapping[self.goal_type] is None:
            raise ValueError(
                f"goal_type={self.goal_type.value} 에는 해당 target 필드가 필요합니다."
            )
        return self


class PlanStep(_StrictModel):
    """계획된(그리고 실행 후 채워지는) 스텝 한 개.

    계획 단계: from_state_id, key, expected_to_state_id 를 채운다.
    실행 단계: executed/actual_to_state_id/matched/observed_confidence 를 갱신한다.
    """

    index: int = Field(ge=0)
    from_state_id: str = Field(min_length=1)
    key: KeyPress
    expected_to_state_id: str = Field(min_length=1)

    # 실행 후 채워지는 필드
    executed: bool = False
    actual_to_state_id: Optional[str] = None
    matched: Optional[bool] = Field(
        default=None, description="actual==expected 여부. 미실행이면 None."
    )
    observed_confidence: Optional[Confidence] = None


class ExecutionStatus(str, Enum):
    """목표 실행 최종 상태."""

    SUCCESS = "success"  # 목표 상태 도달
    FAILED_UNREACHABLE = "failed_unreachable"  # 맵상 경로 없음(계획 불가)
    FAILED_UNRESOLVED = "failed_unresolved"  # 목표 해석/매핑 실패(미학습 포함)
    FAILED_BUDGET = "failed_budget"  # 재계획/스텝 예산 소진
    FAILED_DRIVER = "failed_driver"  # 드라이버/센스 오류


class ExecutionResult(_StrictModel):
    """UC-3 목표기반 실행의 결과 리포트.

    - status: 최종 상태.
    - goal: 실행한 목표(해석 결과 포함).
    - start_state_id / final_state_id: 실행 시작/종료 시 확정 상태.
    - steps: 실제 실행 궤적(재계획으로 계획이 갱신되면 최종 궤적 기준으로 append).
    - button_sequence: 실제로 누른 키 토큰 열(성공 경로/오버헤드 측정용, M6).
    - replans: 재계획 발생 횟수(견고성 지표, M3).
    - message: 사람이 읽는 요약/실패 사유.
    """

    status: ExecutionStatus
    goal: Goal
    start_state_id: Optional[str] = None
    final_state_id: Optional[str] = None
    steps: list[PlanStep] = Field(default_factory=list)
    replans: int = Field(default=0, ge=0)
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None
    message: Optional[str] = Field(default=None, max_length=1000)

    @computed_field
    @property
    def button_sequence(self) -> list[str]:
        """실제 실행된(executed=True) 스텝들의 키 토큰 열."""
        return [s.key.token for s in self.steps if s.executed]

    @computed_field
    @property
    def step_count(self) -> int:
        return sum(1 for s in self.steps if s.executed)

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.SUCCESS


# --------------------------------------------------------------------------- #
# 7. 학습 세션 요약 (UC-1 종료 집계)
# --------------------------------------------------------------------------- #


class LearningSummary(_StrictModel):
    """학습 세션 종료 시 집계(UC-1 성공 기준).

    엔진이 세션 종료 시 생성. 커버리지 지표(M4)의 근거.
    """

    session_id: str = Field(min_length=1)
    steps_taken: int = Field(ge=0)
    states_visited: int = Field(ge=0)
    transitions_recorded: int = Field(ge=0)
    unexplored_edges: int = Field(
        default=0, ge=0, description="후보 버튼 중 아직 시도하지 않은 (state,button) 수."
    )
    coverage_ratio: Optional[Confidence] = Field(
        default=None, description="도달가능 상태 대비 방문 비율(계산 가능 시)."
    )
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None
    stop_reason: Optional[str] = Field(default=None, max_length=200)


__all__ = [
    # 버튼/키
    "Button",
    "KeyPress",
    # 상태
    "StateKind",
    "ScreenState",
    "compute_state_id",
    # 관측/전이/맵
    "Observation",
    "Transition",
    "NavMap",
    # 목표/계획/실행
    "GoalType",
    "Goal",
    "PlanStep",
    "ExecutionStatus",
    "ExecutionResult",
    # 세션 집계
    "LearningSummary",
    # 공통 타입
    "Signature",
    "Confidence",
]
