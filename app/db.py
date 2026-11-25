"""SQLite 持久化层。

表：

* ``evidence``：证据表，每条含证据号和判为同件的两个登记号，
  撤回只是置 ``withdrawn`` 标记，行保留以备追溯；
* ``handover``：交接申请及其 *已保存的结论*（供数/待补/暂停、
  两条链的登记号与证据号）。查询接口直接读这里，不临时重算；
  另存上次判定时的 *可达范围*（``reachable`` JSON）和该结论是否
  还能被手工重核直接复用的标记（``reuse_eligible``）；
* ``handover_evidence``：交接对当前依据所用证据的索引，
  撤回证据时凭它一次定位需要重核的交接对；
* ``handover_scope``：每个交接对上次判定时可达范围内的登记号，
  证据增撤提交时凭索引一次定位“受影响、应失去复用资格”的结论。

复用规则（见 ``reverify_handover_ex``）：上次判定后，若没有任何证据
增撤，或每次增撤的证据两端都在保存的可达范围之外，则旧结论和依据
仍然有效，手工重核直接复用，跳过证据全表读取和双链求解；只要有一次
增撤涉及可达范围内的登记号，``reuse_eligible`` 就会在该证据事务提交
时被置 0，下一次手工重核重新判定（待补结论同理）。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .graph import Edge, find_two_chains

logger = logging.getLogger("handover.reverify")

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
    reachable  TEXT,
    reuse_eligible INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS handover_scope (
    handover_id INTEGER NOT NULL,
    register    TEXT NOT NULL,
    PRIMARY KEY (handover_id, register),
    FOREIGN KEY (handover_id) REFERENCES handover(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_hs_register
    ON handover_scope(register);
"""


def _now() -> str:
    # 微秒精度：保证快速连续的重新判定也能得到严格更新的 updated_at
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_pair(reg_a: str, reg_b: str) -> tuple[str, str]:
    """交接对规范化：按字典序排列，(A,B) 与 (B,A) 视为同一申请。"""
    return tuple(sorted((reg_a, reg_b)))  # type: ignore[return-value]


@dataclass(frozen=True)
class ReverifyOutcome:
    """一次手工重核的结果：结论字典 + 是否复用了已保存结论。"""

    handover: dict
    reused: bool


def _reachable_scope(a: str, b: str, edges: list[Edge]) -> set[str]:
    """上次判定时，从交接两端沿当前有效证据可达的全部登记号。

    证据视为无向边，因此结果是 ``a``、``b`` 各自所在连通片的并集；
    两个端点本身始终计入（没有任何证据时范围也恰为 ``{a, b}``）。
    """
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.u, set()).add(edge.v)
        adjacency.setdefault(edge.v, set()).add(edge.u)

    scope = {a, b}
    for start in (a, b):
        if start not in adjacency:
            continue
        stack = [start]
        while stack:
            node = stack.pop()
            for nxt in adjacency[node]:
                if nxt not in scope:
                    scope.add(nxt)
                    stack.append(nxt)
    return scope


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
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """旧版本库平滑加列；旧结论默认不可复用，首次手工重核即刷新。"""
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(handover)").fetchall()
        }
        if "reachable" not in cols:
            conn.execute("ALTER TABLE handover ADD COLUMN reachable TEXT")
        if "reuse_eligible" not in cols:
            conn.execute(
                "ALTER TABLE handover "
                "ADD COLUMN reuse_eligible INTEGER NOT NULL DEFAULT 0"
            )

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
            # 与证据提交同事务：可达范围含新证据任一端点的旧结论，
            # 立即失去复用资格；两端都在所有范围之外的结论不受影响。
            revoked = self._revoke_reuse(conn, reg_a, reg_b)
            if revoked:
                logger.info(
                    "evidence added id=%s endpoints=(%s,%s); "
                    "revoked reuse eligibility of %d handover(s)",
                    evidence_id, reg_a, reg_b, revoked,
                )
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
        """手工重核；返回已保存/重判的结论，交接不存在返回 ``None``。

        保留原有的 ``dict | None`` 返回约定；是否走了复用快路见
        :meth:`reverify_handover_ex`。
        """
        outcome = self.reverify_handover_ex(handover_id)
        return None if outcome is None else outcome.handover

    def reverify_handover_ex(self, handover_id: int) -> ReverifyOutcome | None:
        """手工重核的完整结果，附带本次是否 *复用* 了旧结论。

        - ``reuse_eligible=1``：上次判定后没有任何涉及可达范围的证据
          增撤（提交时已用 :meth:`_revoke_reuse` 精准撤销资格），直接
          读已保存行返回，不读证据全表、不跑双链求解、也不写入；
        - 否则在同一事务内按当前有效证据重新判定并保存新结论与范围。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM handover WHERE id = ?", (handover_id,)
            ).fetchone()
            if row is None:
                return None

            if row["reuse_eligible"]:
                logger.info(
                    "reverify handover_id=%s -> reused saved conclusion "
                    "(status=%s); full scan and chain solve skipped",
                    handover_id, row["status"],
                )
                return ReverifyOutcome(
                    handover=_handover_row_to_dict(row), reused=True
                )

            logger.info(
                "reverify handover_id=%s -> recomputing from active evidence",
                handover_id,
            )
            # 先拿写锁再读全表：结论/范围与提交时的有效证据一致，
            # 也不会覆盖另一证据提交刚刚写入的复用失效标记。
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            self._recompute(
                conn, handover_id, row["reg_a"], row["reg_b"]
            )
            fresh = conn.execute(
                "SELECT * FROM handover WHERE id = ?", (handover_id,)
            ).fetchone()
            return ReverifyOutcome(
                handover=_handover_row_to_dict(fresh), reused=False  # type: ignore[arg-type]
            )

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
            # 可达范围含被撤证据任一端点的旧结论立即失去复用资格。
            # 必须在下方 _recompute 刷新 handover_scope 之前执行；
            # 被自动重核的交接对会在 _recompute 末尾恢复资格。
            revoked = self._revoke_reuse(
                conn, row["reg_a"], row["reg_b"]
            )
            conn.execute(
                "UPDATE evidence SET withdrawn = 1, withdrawn_at = ? "
                "WHERE evidence_id = ?",
                (_now(), evidence_id),
            )
            if revoked:
                logger.info(
                    "evidence withdrawn id=%s endpoints=(%s,%s); "
                    "revoked reuse eligibility of %d handover(s)",
                    evidence_id, row["reg_a"], row["reg_b"], revoked,
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
    def _revoke_reuse(
        self,
        conn: sqlite3.Connection,
        reg_a: str,
        reg_b: str,
    ) -> int:
        """证据增撤提交时调用：可达范围含任一端点的结论置为不可复用。

        命中 ``handover_scope.register`` 索引；两端都在范围外的交接对
        不会被更新。返回被撤销资格的交接对数量。必须在证据增撤所在的
        同一事务内调用，写入失败随事务整体回滚。
        """
        cur = conn.execute(
            """
            UPDATE handover
               SET reuse_eligible = 0
             WHERE reuse_eligible = 1
               AND id IN (
                    SELECT handover_id
                      FROM handover_scope
                     WHERE register = ? OR register = ?
               )
            """,
            (reg_a, reg_b),
        )
        return cur.rowcount

    def _save_scope(
        self,
        conn: sqlite3.Connection,
        handover_id: int,
        scope: set[str],
    ) -> None:
        """以本次提交时的有效证据为准，重建可达范围索引与 JSON 快照。"""
        ordered = sorted(scope)
        conn.execute(
            "UPDATE handover SET reachable = ? WHERE id = ?",
            (json.dumps(ordered, ensure_ascii=False), handover_id),
        )
        conn.execute(
            "DELETE FROM handover_scope WHERE handover_id = ?",
            (handover_id,),
        )
        conn.executemany(
            "INSERT INTO handover_scope(handover_id, register) "
            "VALUES (?, ?)",
            [(handover_id, reg) for reg in ordered],
        )

    def _recompute(
        self,
        conn: sqlite3.Connection,
        handover_id: int,
        a: str,
        b: str,
        on_failure: str = STATUS_PENDING,
    ) -> None:
        """基于当前全部有效证据重算一个交接对，并把结论落库。

        无论找到双链与否，都同时保存本次判定时的可达范围，并把
        ``reuse_eligible`` 置 1：这份新结论与当前已提交的有效证据一致，
        此后在没有范围相关增撤的前提下可被手工重核直接复用。
        """
        edges = self._active_edges(conn)
        chains = find_two_chains(a, b, edges)
        scope = _reachable_scope(a, b, edges)

        if chains is None:
            conn.execute(
                "UPDATE handover SET status = ?, chain1_regs = NULL, "
                "chain1_evidence = NULL, chain2_regs = NULL, "
                "chain2_evidence = NULL, reason = ?, "
                "reuse_eligible = 1, updated_at = ? WHERE id = ?",
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
            self._save_scope(conn, handover_id, scope)
            return

        (c1_regs, c1_ev), (c2_regs, c2_ev) = (
            _chain_to_json(a, chains[0]),
            _chain_to_json(a, chains[1]),
        )
        conn.execute(
            "UPDATE handover SET status = ?, chain1_regs = ?, "
            "chain1_evidence = ?, chain2_regs = ?, chain2_evidence = ?, "
            "reason = NULL, reuse_eligible = 1, updated_at = ? "
            "WHERE id = ?",
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
        self._save_scope(conn, handover_id, scope)


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
