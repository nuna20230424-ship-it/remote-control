"""KeyScreenStore — 키이벤트↔화면 매핑을 저장하는 별도 SQLite DB(StepObserver 구현).

navmap(JSON, 그래프 정본)과 **분리된** 학습 원장이다. 학습 스텝마다 LLM(VLM)이 분석한
화면 정보와 (from_state, key)→to_state 매핑, 그리고 오류·자가치유 이력을 누적한다.

테이블
------
- screens        : 화면 상태별 최신 LLM 분석(서명·kind·app·서술·신뢰도·관측수).
- key_screen_map : (from_state_id, key_token) → to_state + 분석 + 신뢰도 + 관측수 + reconciled.
- error_log      : 스텝 오류 + 복구 결정 이력.

self-contained(파일 1개, 서버리스). 경로는 REMOTECTL_KEYSCREEN_DB(기본 ./data/keyscreen.db),
테스트는 ":memory:" 사용.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from remotectl.engine.hooks import ErrorContext, RemediationDecision, StepObserver, StepRecord
from remotectl.models import _utcnow

__all__ = ["KeyScreenStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screens (
    state_id   TEXT PRIMARY KEY,
    signature  TEXT NOT NULL,
    kind       TEXT,
    app_id     TEXT,
    analysis   TEXT,
    confidence REAL,
    observed   INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT,
    last_seen  TEXT
);
CREATE TABLE IF NOT EXISTS key_screen_map (
    from_state_id TEXT NOT NULL,
    key_token     TEXT NOT NULL,
    to_state_id   TEXT NOT NULL,
    to_signature  TEXT,
    to_kind       TEXT,
    to_app_id     TEXT,
    analysis      TEXT,
    confidence    REAL,
    observed      INTEGER NOT NULL DEFAULT 0,
    reconciled    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'ok',
    first_seen    TEXT,
    last_seen     TEXT,
    PRIMARY KEY (from_state_id, key_token)
);
CREATE TABLE IF NOT EXISTS error_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT,
    phase         TEXT,
    from_state_id TEXT,
    key_token     TEXT,
    message       TEXT,
    action        TEXT,
    note          TEXT
);
"""


class KeyScreenStore(StepObserver):
    """키이벤트↔화면 매핑 SQLite 원장(StepObserver 로서 학습 루프에 주입)."""

    def __init__(self, path: str = "./data/keyscreen.db"):
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        # 학습은 단일 스레드지만, API 서버 컨텍스트 대비 same-thread 체크는 완화.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @classmethod
    def from_env(cls, **overrides) -> "KeyScreenStore":
        """REMOTECTL_KEYSCREEN_DB(기본 ./data/keyscreen.db)로 구성."""
        path = overrides.pop("path", None) or os.environ.get(
            "REMOTECTL_KEYSCREEN_DB", "./data/keyscreen.db"
        )
        return cls(path=path)

    # --- StepObserver 계약 ------------------------------------------------ #

    def on_step(self, record: StepRecord) -> None:
        """정상 스텝: 화면 + (from,key)→to 매핑 upsert(관측수 증가)."""
        now = _utcnow().isoformat()
        cur = self._conn.cursor()
        # 화면(도착 상태) 최신 분석 upsert.
        cur.execute(
            """
            INSERT INTO screens (state_id, signature, kind, app_id, analysis,
                                 confidence, observed, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(state_id) DO UPDATE SET
                signature=excluded.signature, kind=excluded.kind, app_id=excluded.app_id,
                analysis=excluded.analysis, confidence=excluded.confidence,
                observed=screens.observed+1, last_seen=excluded.last_seen
            """,
            (record.to_state_id, record.to_signature, record.to_kind, record.to_app_id,
             record.analysis, record.confidence, now, now),
        )
        # 키이벤트 매핑 upsert.
        cur.execute(
            """
            INSERT INTO key_screen_map (from_state_id, key_token, to_state_id, to_signature,
                                        to_kind, to_app_id, analysis, confidence, observed,
                                        reconciled, status, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'ok', ?, ?)
            ON CONFLICT(from_state_id, key_token) DO UPDATE SET
                to_state_id=excluded.to_state_id, to_signature=excluded.to_signature,
                to_kind=excluded.to_kind, to_app_id=excluded.to_app_id,
                analysis=excluded.analysis, confidence=excluded.confidence,
                observed=key_screen_map.observed+1,
                reconciled=MAX(key_screen_map.reconciled, excluded.reconciled),
                status='ok', last_seen=excluded.last_seen
            """,
            (record.from_state_id, record.key_token, record.to_state_id, record.to_signature,
             record.to_kind, record.to_app_id, record.analysis, record.confidence,
             1 if record.reconciled else 0, now, now),
        )
        self._conn.commit()

    def on_error(self, context: ErrorContext, decision: RemediationDecision) -> None:
        """오류 + 복구 결정 로깅. 매핑이 있으면 status 를 error 로 표시."""
        now = _utcnow().isoformat()
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO error_log (ts, phase, from_state_id, key_token, message, action, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now, context.phase.value, context.from_state_id, context.key_token,
             context.message, decision.action.value, decision.note),
        )
        if context.from_state_id and context.key_token:
            cur.execute(
                "UPDATE key_screen_map SET status='error', last_seen=? "
                "WHERE from_state_id=? AND key_token=?",
                (now, context.from_state_id, context.key_token),
            )
        self._conn.commit()

    # --- 조회 ------------------------------------------------------------- #

    def stats(self) -> dict:
        """요약 통계(대시보드/리포트용)."""
        cur = self._conn.cursor()
        screens = cur.execute("SELECT COUNT(*) c FROM screens").fetchone()["c"]
        mappings = cur.execute("SELECT COUNT(*) c FROM key_screen_map").fetchone()["c"]
        errors = cur.execute("SELECT COUNT(*) c FROM error_log").fetchone()["c"]
        reconciled = cur.execute(
            "SELECT COUNT(*) c FROM key_screen_map WHERE reconciled=1"
        ).fetchone()["c"]
        bad = cur.execute(
            "SELECT COUNT(*) c FROM key_screen_map WHERE status='error'"
        ).fetchone()["c"]
        return {
            "screens": screens, "mappings": mappings, "errors": errors,
            "reconciled": reconciled, "error_mappings": bad, "db_path": self.path,
        }

    def mappings(self) -> list[dict]:
        """전체 키이벤트↔화면 매핑."""
        rows = self._conn.execute(
            "SELECT * FROM key_screen_map ORDER BY from_state_id, key_token"
        ).fetchall()
        return [dict(r) for r in rows]

    def screens(self) -> list[dict]:
        """수집된 화면(LLM 분석 포함)."""
        rows = self._conn.execute(
            "SELECT * FROM screens ORDER BY state_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "KeyScreenStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
