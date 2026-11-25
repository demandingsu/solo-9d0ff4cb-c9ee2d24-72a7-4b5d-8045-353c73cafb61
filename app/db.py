"""SQLite 持久化层。

三张表：

* ``evidence``：证据表，每条含证据号和判为同件的两个登记号，
  撤回只是置 ``withdrawn`` 标记，行保留以备追溯；
* ``handover``：交接申请及其 *已保存的结论*（供数/待补/暂停、
  两条链的登记号与证据号）。查询接口直接读这里，不临时重算；
* ``handover_evidence``：交接对当前依据所用证据的索引，
  撤回证据时凭它一次定位需要重核的交接对。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .graph import Edge, find_two_chains

STATUS_SUPPLIED = "supplied"  # 供数中：两条链齐备
STATUS_PENDING = "pending"  # 待补：申请时证据不足
STATUS_PAUSED = "paused"  # 暂停：撤回后无法再凑齐两条链

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    reg_a       TEXT NOT NULL,
    reg_b       TEXT NOT NULL,
    withdrawn   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    withdrawn_at TEXT
);

CREATE TABLE IF NOT EXISTS handover (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    reg_a      TEXT NOT NULL,
    reg_b      TEXT NOT NULL,
    status     TEXT NOT NULL,
    chain1_regs     TEXT,
    chain1_evidence TEXT,
    chain2_regs     TEXT,
    chain2_evidence TEXT,
    reason     TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(reg_a, reg_b)
);

CREATE TABLE IF NOT EXISTS handover_evidence (
    handover_id INTEGER NOT NULL,
    evidence_id TEXT NOT NULL,
    PRIMARY KEY (handover_id, evidence_id),
    FOREIGN KEY (handover_id) REFERENCES handover(id)
);

CREATE INDEX IF NOT EXISTS idx_he_evidence
    ON handover_evidence(evidence_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_pair(reg_a: str, reg_b: str) -> tuple[str, str]:
    """交接对规范化：按字典序排列，(A,B) 与 (B,A) 视为同一申请。"""
    return tuple(sorted((reg_a, reg_b)))  # type: ignore[return-value]


class Database:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # 证据
    # ------------------------------------------------------------------
    def insert_evidence(
        self, evidence_id: str, reg_a: str, reg_b: str
    ) -> dict:
        if reg_a == reg_b:
            raise ValueError("一条证据的两个登记号不能相同")
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO evidence(evidence_id, reg_a, reg_b, "
                    "created_at) VALUES (?, ?, ?, ?)",
                    (evidence_id, reg_a, reg_b, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateEvidence(evidence_id) from exc
        return self.get_evidence(evidence_id)  # type: ignore[return-value]

    def get_evidence(self, evidence_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        return _evidence_row_to_dict(row) if row else None

    def list_evidence(self, include_withdrawn: bool = False) -> list[dict]:
        sql = "SELECT * FROM evidence"
        if not include_withdrawn:
            sql += " WHERE withdrawn = 0"
        sql += " ORDER BY evidence_id"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [_evidence_row_to_dict(r) for r in rows]

    def _active_edges(self, conn: sqlite3.Connection) -> list[Edge]:
        rows = conn.execute(
            "SELECT evidence_id, reg_a, reg_b FROM evidence "
            "WHERE withdrawn = 0"
        ).fetchall()
        return [Edge(r["evidence_id"], r["reg_a"], r["reg_b"]) for r in rows]

    # ------------------------------------------------------------------
    # 交接申请与重核
    # ------------------------------------------------------------------
    def create_handover(self, reg_a: str, reg_b: str) -> dict:
        a, b = canonical_pair(reg_a, reg_b)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM handover WHERE reg_a = ? AND reg_b = ?",
                (a, b),
            ).fetchone()
            if existing:
                raise DuplicateHandover(existing["id"])
            now = _now()
            cur = conn.execute(
                "INSERT INTO handover(reg_a, reg_b, status, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?)",
                (a, b, STATUS_PENDING, now, now),
            )
            handover_id = cur.lastrowid
            self._recompute(conn, handover_id, a, b)
        return self.get_handover(handover_id)  # type: ignore[return-value]

    def get_handover(self, handover_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM handover WHERE id = ?", (handover_id,)
            ).fetchone()
        return _handover_row_to_dict(row) if row else None

    def list_handovers(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM handover ORDER BY id"
            ).fetchall()
        return [_handover_row_to_dict(r) for r in rows]

    def reverify_handover(self, handover_id: int) -> dict | None:
        """手工重核（例如补入新证据后，让待补/暂停的申请重新判定）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM handover WHERE id = ?", (handover_id,)
            ).fetchone()
            if row is None:
                return None
            self._recompute(conn, handover_id, row["reg_a"], row["reg_b"])
        return self.get_handover(handover_id)

    def handovers_using_evidence(self, evidence_id: str) -> list[int]:
        """当前依据中引用了某证据的交接对 id（撤回前/后均可查）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT handover_id FROM handover_evidence "
                "WHERE evidence_id = ? ORDER BY handover_id",
                (evidence_id,),
            ).fetchall()
        return [r["handover_id"] for r in rows]

    def withdraw_evidence(self, evidence_id: str) -> dict | None:
        """撤回证据并只重核用到它的交接对；撤回与重核在同一事务内生效。

        任一步骤失败则整体回滚，已保存的旧结论不受影响，查询仍可读。
        返回被撤回的证据；证据不存在或已撤回返回 ``None``。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            if row is None or row["withdrawn"]:
                return None

            affected = [
                r["handover_id"]
                for r in conn.execute(
                    "SELECT handover_id FROM handover_evidence "
                    "WHERE evidence_id = ? ORDER BY handover_id",
                    (evidence_id,),
                ).fetchall()
            ]
            conn.execute(
                "UPDATE evidence SET withdrawn = 1, withdrawn_at = ? "
                "WHERE evidence_id = ?",
                (_now(), evidence_id),
            )
            for handover_id in affected:
                hr = conn.execute(
                    "SELECT reg_a, reg_b FROM handover WHERE id = ?",
                    (handover_id,),
                ).fetchone()
                self._recompute(
                    conn, handover_id, hr["reg_a"], hr["reg_b"],
                    on_failure=STATUS_PAUSED,
                )
        return self.get_evidence(evidence_id)

    # ------------------------------------------------------------------
    def _recompute(
        self,
        conn: sqlite3.Connection,
        handover_id: int,
        a: str,
        b: str,
        on_failure: str = STATUS_PENDING,
    ) -> None:
        """基于当前全部有效证据重算一个交接对，并把结论落库。"""
        edges = self._active_edges(conn)
        chains = find_two_chains(a, b, edges)

        if chains is None:
            conn.execute(
                "UPDATE handover SET status = ?, chain1_regs = NULL, "
                "chain1_evidence = NULL, chain2_regs = NULL, "
                "chain2_evidence = NULL, reason = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    on_failure,
                    "无法在当前有效证据中找到两条登记号、证据均不共用"
                    "的同件证据链",
                    _now(),
                    handover_id,
                ),
            )
            conn.execute(
                "DELETE FROM handover_evidence WHERE handover_id = ?",
                (handover_id,),
            )
            return

        (c1_regs, c1_ev), (c2_regs, c2_ev) = (
            _chain_to_json(a, chains[0]),
            _chain_to_json(a, chains[1]),
        )
        conn.execute(
            "UPDATE handover SET status = ?, chain1_regs = ?, "
            "chain1_evidence = ?, chain2_regs = ?, chain2_evidence = ?, "
            "reason = NULL, updated_at = ? WHERE id = ?",
            (
                STATUS_SUPPLIED,
                json.dumps(c1_regs, ensure_ascii=False),
                json.dumps(c1_ev, ensure_ascii=False),
                json.dumps(c2_regs, ensure_ascii=False),
                json.dumps(c2_ev, ensure_ascii=False),
                _now(),
                handover_id,
            ),
        )
        conn.execute(
            "DELETE FROM handover_evidence WHERE handover_id = ?",
            (handover_id,),
        )
        conn.executemany(
            "INSERT INTO handover_evidence(handover_id, evidence_id) "
            "VALUES (?, ?)",
            [(handover_id, ev) for ev in c1_ev + c2_ev],
        )


def _chain_to_json(
    start: str, chain: list[tuple[str, str]]
) -> tuple[list[str], list[str]]:
    regs = [start]
    evidence: list[str] = []
    for reg, ev in chain:
        regs.append(reg)
        evidence.append(ev)
    return regs, evidence


def _evidence_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "evidence_id": row["evidence_id"],
        "reg_a": row["reg_a"],
        "reg_b": row["reg_b"],
        "withdrawn": bool(row["withdrawn"]),
        "created_at": row["created_at"],
        "withdrawn_at": row["withdrawn_at"],
    }


def _handover_row_to_dict(row: sqlite3.Row) -> dict:
    def loads(value: str | None) -> list[str] | None:
        return json.loads(value) if value is not None else None

    return {
        "id": row["id"],
        "reg_a": row["reg_a"],
        "reg_b": row["reg_b"],
        "status": row["status"],
        "chain1": {
            "registrations": loads(row["chain1_regs"]),
            "evidence": loads(row["chain1_evidence"]),
        },
        "chain2": {
            "registrations": loads(row["chain2_regs"]),
            "evidence": loads(row["chain2_evidence"]),
        },
        "reason": row["reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class DuplicateEvidence(Exception):
    def __init__(self, evidence_id: str) -> None:
        self.evidence_id = evidence_id
        super().__init__(f"证据号已存在：{evidence_id}")


class DuplicateHandover(Exception):
    def __init__(self, handover_id: int) -> None:
        self.handover_id = handover_id
        super().__init__(f"该登记号对的交接申请已存在：id={handover_id}")
