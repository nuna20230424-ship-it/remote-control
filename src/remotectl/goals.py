"""UC-3 자연어 -> Goal 규칙 기반 해석 (R4).

이 모듈은 사용자가 말한 자연어 명령(예: "넷플릭스 켜줘", "홈으로 가")을
규칙(별칭 사전 + 라벨 부분매칭)으로 :class:`~remotectl.models.Goal` 로 해석한다.
1차 릴리스는 LLM 을 쓰지 않는다 — 결정론적이고 감사 가능한 규칙만 사용한다.

책임 범위
---------
- resolve_goal: 자연어 텍스트를 Goal 로 해석. 별칭 사전으로 앱/종류를 우선 매핑하고,
  실패하면 맵(NavMap)의 상태 라벨/앱 id 부분매칭으로 보강한다. 최종적으로도 매핑에
  실패하면 Goal.resolved=False 와 함께 명확한 사유(resolve_note)를 채워 반환한다
  (예외를 던지지 않는다 — 실행 계층이 결과 상태로 정규화하도록).
- find_goal_state_id: 해석된 Goal 을 실제 맵 상태 id 로 조회한다. 미학습(맵에 해당
  상태 없음)이면 None 을 돌려주고, 이는 "미학습" 사유로 이어진다.

설계 원칙
---------
- 순수 규칙/조회 계층: HTTP·networkx·FastAPI 의존성 없음. 오직 models 와 NavMap 만 안다.
- "미학습(맵에 상태 없음)"과 "미매핑(문장을 목표로 해석 못함)"을 사유로 구분해 전달한다(R4).
- 앱 식별자(app_id)는 소문자 정규화를 정본으로 삼는다(ScreenState.app_id 규약과 일치).
"""

from __future__ import annotations

import re
from typing import Optional

from .models import Goal, GoalType, NavMap, ScreenState, StateKind

# --------------------------------------------------------------------------- #
# 별칭 사전
# --------------------------------------------------------------------------- #
#
# 자연어에 등장하는 표현(한/영, 표기 변형 포함) -> 정규화 앱 식별자(app_id, 소문자).
# 여기에 없는 앱도 라벨/app_id 부분매칭으로 보강되므로, 이 사전은 "흔한 별칭"의
# 빠른 경로일 뿐 폐쇄 목록이 아니다. 키는 모두 소문자로 저장하고, 매칭 시 입력도
# 소문자로 낮춰 비교한다.
GOAL_ALIASES: dict[str, str] = {
    # Netflix
    "netflix": "netflix",
    "넷플릭스": "netflix",
    "넷플": "netflix",
    "nflx": "netflix",
    # YouTube
    "youtube": "youtube",
    "유튜브": "youtube",
    "유투브": "youtube",
    "yt": "youtube",
    # Disney+
    "disney": "disney",
    "disney+": "disney",
    "디즈니": "disney",
    "디즈니플러스": "disney",
    # Watcha
    "watcha": "watcha",
    "왓챠": "watcha",
    # Wavve
    "wavve": "wavve",
    "웨이브": "wavve",
    # Tving
    "tving": "tving",
    "티빙": "tving",
    # Coupang Play
    "coupang": "coupangplay",
    "coupangplay": "coupangplay",
    "쿠팡": "coupangplay",
    "쿠팡플레이": "coupangplay",
    # Amazon Prime Video
    "prime": "primevideo",
    "primevideo": "primevideo",
    "아마존": "primevideo",
    "프라임": "primevideo",
    "프라임비디오": "primevideo",
    # Apple TV
    "appletv": "appletv",
    "apple tv": "appletv",
    "애플티비": "appletv",
}

# --------------------------------------------------------------------------- #
# 종류(kind) 별칭 — GOTO_KIND 목표(예: "홈으로", "설정 열어")
# --------------------------------------------------------------------------- #
#
# 자연어 표현 -> StateKind. 앱 별칭보다 우선순위가 낮다(앱 이름이 먼저 걸리면 앱 실행이
# 사용자 의도에 더 가깝다고 본다). "홈"은 예외적으로 강한 신호이므로 별도 처리한다.
_KIND_ALIASES: dict[str, StateKind] = {
    "home": StateKind.HOME,
    "홈": StateKind.HOME,
    "홈화면": StateKind.HOME,
    "메인": StateKind.HOME,
    "메인화면": StateKind.HOME,
    "setting": StateKind.SETTINGS,
    "settings": StateKind.SETTINGS,
    "설정": StateKind.SETTINGS,
    "환경설정": StateKind.SETTINGS,
    "menu": StateKind.MENU,
    "메뉴": StateKind.MENU,
    "livetv": StateKind.LIVE_TV,
    "live tv": StateKind.LIVE_TV,
    "라이브": StateKind.LIVE_TV,
    "실시간": StateKind.LIVE_TV,
    "실시간tv": StateKind.LIVE_TV,
    "tv": StateKind.LIVE_TV,
    "방송": StateKind.LIVE_TV,
    "playback": StateKind.PLAYBACK,
    "재생": StateKind.PLAYBACK,
}

# "특정 상태 id 로 이동" 목표를 직접 지정하는 접두어(진단/테스트/자동화 편의).
# 예: "goto st_0123456789abcdef" 또는 "state:st_0123456789abcdef".
_STATE_ID_PATTERN = re.compile(
    r"(?:goto\s+|state[:=]\s*)?(st_[0-9a-f]{16})", re.IGNORECASE
)

# 앱 실행 의도를 나타내는 동사/조사 힌트(부분매칭 보강 시 오탐 완화용).
_OPEN_HINTS: tuple[str, ...] = (
    "켜",
    "열",
    "실행",
    "틀어",
    "봐",
    "보여",
    "open",
    "launch",
    "start",
    "play",
    "run",
)


# --------------------------------------------------------------------------- #
# 내부 유틸
# --------------------------------------------------------------------------- #


def _normalize_text(text: str) -> str:
    """비교용 정규화: 소문자화 + 앞뒤 공백 제거.

    한글은 대소문자 개념이 없으므로 그대로 유지되고, 영문/기호 표기 변형만 흡수한다.
    """
    return text.strip().lower()


def _tokenize(normalized: str) -> list[str]:
    """정규화 문자열을 토큰으로 분해.

    한글/영문/숫자 및 '+'(disney+ 대응)를 단어로 취급하고, 나머지 기호/공백은 경계로 본다.
    별칭 사전의 정확 일치(exact match) 매칭에 사용한다.
    """
    return re.findall(r"[0-9a-z가-힣]+\+?|\+", normalized)


def _match_alias_app(normalized: str) -> Optional[str]:
    """별칭 사전으로 앱 식별자 매핑 시도.

    1) 토큰 단위 정확 일치 우선(짧은 별칭의 오탐 방지).
    2) 실패 시, 여러 단어로 된 별칭(예: "apple tv")을 부분 문자열로 탐색.
    매칭되면 정규화 app_id(소문자)를, 아니면 None 을 반환한다.
    """
    tokens = set(_tokenize(normalized))
    best_app: Optional[str] = None
    best_len = 0
    for alias, app_id in GOAL_ALIASES.items():
        if _is_ascii(alias):
            # ASCII: 멀티워드는 부분 문자열, 단일어는 토큰 정확 일치(짧은 별칭 오탐 방지).
            matched = alias in normalized if " " in alias else alias in tokens
        else:
            # 한글: 조사 흡수 위해 부분 문자열 매칭.
            matched = alias in normalized
        if matched and len(alias) > best_len:
            best_app, best_len = app_id, len(alias)
    return best_app


def _is_ascii(s: str) -> bool:
    """문자열이 ASCII(영문/숫자 등)만으로 이뤄졌는지."""
    return s.isascii()


def _match_alias_kind(normalized: str) -> Optional[StateKind]:
    """종류(kind) 별칭 매핑 시도.

    한글은 조사가 붙어(예: "홈으로") 토큰 경계가 별칭과 어긋나므로 부분 문자열로 본다.
    ASCII 별칭(예: "tv")은 짧아서 다른 단어에 우연히 포함될 수 있으므로 토큰 정확 일치만
    허용해 오탐을 막는다. 더 긴(구체적인) 별칭을 우선한다.
    """
    tokens = set(_tokenize(normalized))
    best_kind: Optional[StateKind] = None
    best_len = 0
    for alias, kind in _KIND_ALIASES.items():
        matched = False
        if _is_ascii(alias):
            # ASCII: 멀티워드는 부분 문자열, 단일어는 토큰 정확 일치.
            matched = alias in normalized if " " in alias else alias in tokens
        else:
            # 한글: 부분 문자열 매칭(조사 흡수).
            matched = alias in normalized
        if matched and len(alias) > best_len:
            best_kind, best_len = kind, len(alias)
    return best_kind


def _has_open_hint(normalized: str) -> bool:
    """문장에 '앱을 켠다'류 실행 의도 힌트가 있는지."""
    return any(hint in normalized for hint in _OPEN_HINTS)


def _iter_states(navmap: NavMap) -> list[ScreenState]:
    """맵의 상태 목록(방어적으로 리스트화)."""
    return list(navmap.states.values())


def _match_state_by_app_label(normalized: str, navmap: NavMap) -> Optional[str]:
    """맵의 상태 라벨/앱 id 부분매칭으로 app_id 를 유추.

    별칭 사전에 없는 앱(맵에는 학습돼 있음)을 구제하는 보강 경로다.
    - app_id 가 문장 토큰에 정확히 포함되면 그 app_id.
    - label 이 문장에 부분 포함되면 해당 상태의 app_id(있으면).
    가장 긴(구체적인) 후보를 우선한다.
    """
    tokens = set(_tokenize(normalized))
    best_app: Optional[str] = None
    best_len = 0
    for state in _iter_states(navmap):
        app_id = (state.app_id or "").strip().lower()
        if app_id and app_id in tokens and len(app_id) > best_len:
            best_app, best_len = app_id, len(app_id)
        label = (state.label or "").strip().lower()
        if app_id and label and label in normalized and len(label) > best_len:
            best_app, best_len = app_id, len(label)
    return best_app


# --------------------------------------------------------------------------- #
# 공개 API
# --------------------------------------------------------------------------- #


def resolve_goal(text: str, navmap: NavMap) -> Goal:
    """자연어 명령을 규칙 기반으로 :class:`Goal` 로 해석한다(R4).

    해석 우선순위
    -------------
    1. 명시적 상태 id 지정("goto st_...", "state:st_...") -> GOTO_STATE.
    2. 앱 별칭 사전 매칭 -> OPEN_APP.
    3. 종류(kind) 별칭 매칭("홈으로", "설정") -> GOTO_KIND.
    4. 맵 라벨/app_id 부분매칭(실행 힌트가 있을 때) -> OPEN_APP (미학습 앱 구제).

    반환되는 Goal 의 ``resolved`` 는 "목표를 실제 맵 상태로 확정했는지"를 뜻한다.
    - 목표 자체를 해석하지 못하면(위 4단계 모두 실패) GOTO_KIND(HOME) 로 폴백하지 않고,
      명확한 사유(resolve_note)를 담아 GOTO_STATE 형태로 만들 수 없으므로,
      해석 실패는 OPEN_APP 도 GOTO_KIND 도 아닌 상태로 표현할 수 없다.
      따라서 "미매핑"은 목표 종류를 특정할 수 없는 경우로 정의하고, 이때는
      가장 안전한 형태(GOTO_KIND=HOME, resolved=False, 사유 기재)로 정규화한다.
      (Goal 모델은 target 필드 중 하나가 반드시 필요하므로 표현 가능한 최소 목표를 채운다.)
    - 목표 종류는 해석됐으나 맵에 대상 상태가 없으면 resolved=False + "미학습" 사유.

    Args:
        text: 사용자 자연어 명령. 공백만 있으면 미매핑으로 처리한다.
        navmap: 조회 대상 네비게이션 맵(정본). 라벨/앱 매칭과 학습 여부 판정에 쓴다.

    Returns:
        해석된 Goal. 예외를 던지지 않으며, 실패는 resolved=False + resolve_note 로 전달한다.

    Raises:
        TypeError: text 가 문자열이 아닐 때(방어적).
    """
    if not isinstance(text, str):  # 방어: 잘못된 호출 조기 발견
        raise TypeError(f"text 는 str 이어야 합니다. got={type(text).__name__}")

    normalized = _normalize_text(text)

    if not normalized:
        # 빈 입력: 표현 가능한 최소 목표(HOME)로 정규화하되 미매핑 사유를 남긴다.
        return Goal(
            # raw_text 는 min_length=1 이고 모델이 공백을 strip 하므로 원문이 공백뿐이면
            # 사유를 알 수 있는 플레이스홀더를 둔다.
            raw_text="(빈 명령)",
            goal_type=GoalType.GOTO_KIND,
            target_kind=StateKind.HOME,
            resolved=False,
            resolve_note="빈 명령입니다. 목표를 인식하지 못했습니다.",
        )

    # 1) 명시적 상태 id 지정.
    m = _STATE_ID_PATTERN.search(normalized)
    if m:
        state_id = m.group(1).lower()
        exists = navmap.get_state(state_id) is not None
        return Goal(
            raw_text=text,
            goal_type=GoalType.GOTO_STATE,
            target_state_id=state_id,
            resolved=exists,
            resolve_note=None
            if exists
            else f"맵에 상태 id '{state_id}' 가 없습니다(미학습).",
        )

    # 2) 앱 별칭 사전 매칭 -> OPEN_APP.
    app_id = _match_alias_app(normalized)
    source_note: Optional[str] = None

    # 4) 별칭 실패 시, 실행 힌트가 있으면 맵 라벨/app_id 부분매칭으로 보강.
    if app_id is None and _has_open_hint(normalized):
        app_id = _match_state_by_app_label(normalized, navmap)
        if app_id is not None:
            source_note = "맵 라벨/앱 매칭으로 해석했습니다."

    if app_id is not None:
        goal = Goal(
            raw_text=text,
            goal_type=GoalType.OPEN_APP,
            target_app_id=app_id,
            resolved=False,
        )
        state_id = find_goal_state_id(goal, navmap)
        if state_id is not None:
            goal.resolved = True
            goal.resolve_note = source_note
        else:
            goal.resolve_note = (
                f"앱 '{app_id}' 에 해당하는 화면 상태가 맵에 없습니다(미학습)."
            )
        return goal

    # 3) 종류(kind) 별칭 매칭 -> GOTO_KIND.
    kind = _match_alias_kind(normalized)
    if kind is not None:
        goal = Goal(
            raw_text=text,
            goal_type=GoalType.GOTO_KIND,
            target_kind=kind,
            resolved=False,
        )
        state_id = find_goal_state_id(goal, navmap)
        if state_id is not None:
            goal.resolved = True
        else:
            goal.resolve_note = (
                f"종류 '{kind.value}' 에 해당하는 화면 상태가 맵에 없습니다(미학습)."
            )
        return goal

    # 미매핑: 목표 종류를 특정하지 못함. 표현 가능한 최소 목표(HOME)로 정규화 + 사유.
    return Goal(
        raw_text=text,
        goal_type=GoalType.GOTO_KIND,
        target_kind=StateKind.HOME,
        resolved=False,
        resolve_note=(
            f"명령 '{text.strip()}' 을(를) 알려진 앱/화면 목표로 해석하지 못했습니다(미매핑)."
        ),
    )


def find_goal_state_id(goal: Goal, navmap: NavMap) -> Optional[str]:
    """해석된 목표에 대응하는 맵 상태 id 를 조회한다.

    goal_type 별 조회 규칙
    ----------------------
    - OPEN_APP: app_id 가 target_app_id 와 일치하는 상태 중 대표 하나. 후보가 여럿이면
      (신뢰도, 방문 횟수) 가 높은 상태를 우선한다(가장 안정적으로 관측된 앱 화면).
    - GOTO_STATE: target_state_id 가 맵에 존재하면 그 id, 없으면 None.
    - GOTO_KIND: kind 가 일치하는 상태 중 대표 하나. HOME 은 root_state_id 를 최우선한다.
      나머지는 (신뢰도, 방문 횟수) 우선.

    Args:
        goal: 해석된 목표(resolve_goal 산출물).
        navmap: 조회 대상 네비게이션 맵.

    Returns:
        대응 상태 id. 맵에 대상이 없으면(미학습) None.
    """
    if goal.goal_type is GoalType.GOTO_STATE:
        sid = goal.target_state_id
        if sid and navmap.get_state(sid) is not None:
            return sid
        return None

    if goal.goal_type is GoalType.OPEN_APP:
        target = (goal.target_app_id or "").strip().lower()
        if not target:
            return None
        candidates = [
            s
            for s in navmap.states.values()
            if (s.app_id or "").strip().lower() == target
        ]
        best = _pick_representative(candidates)
        return best.id if best is not None else None

    if goal.goal_type is GoalType.GOTO_KIND:
        target_kind = goal.target_kind
        if target_kind is None:
            return None
        # HOME 은 학습 시작점(root)이 정본 후보.
        if target_kind is StateKind.HOME and navmap.root_state_id:
            root = navmap.get_state(navmap.root_state_id)
            if root is not None and root.kind is StateKind.HOME:
                return root.id
        candidates = [s for s in navmap.states.values() if s.kind is target_kind]
        best = _pick_representative(candidates)
        return best.id if best is not None else None

    # 알 수 없는 goal_type(스키마 진화 대비): 안전하게 미해석.
    return None


def _pick_representative(candidates: list[ScreenState]) -> Optional[ScreenState]:
    """상태 후보 중 대표 하나를 결정론적으로 고른다.

    우선순위: (신뢰도 desc, 방문 횟수 desc, id asc). id 를 최종 타이브레이커로 두어
    동일 통계일 때도 결과가 안정적(결정론적)이도록 한다.
    """
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda s: (-s.confidence, -s.visit_count, s.id),
    )[0]
