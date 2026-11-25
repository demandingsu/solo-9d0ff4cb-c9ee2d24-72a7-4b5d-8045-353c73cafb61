"""手工重核“可达范围 + 复用资格”优化的端到端测试。

运行：/tmp/venv/bin/python -I tests/test_reverify_cache.py

覆盖任务要求的四类实测：
* 无关补证（增撤证据两端都在可达范围外）-> 复用，跳过全表读取/求解；
* 片区接通（范围内登记号接入新片区）-> 重新判定；
* 片区切断（撤回涉及范围内登记号的证据，含不在当前依据里的边）-> 重判；
* 写入失败回滚 -> 上次结论与复用资格都仍可查/可用。
另含待补交接、提交时失效、二次重核复用与旧库迁移。
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, "/workspace")

from fastapi.testclient import TestClient  # noqa: E402


class ReverifyCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        os.environ["HANDOVER_DB"] = self.db_path
        for name in list(sys.modules):
            if name.startswith("app."):
                del sys.modules[name]
        from app.main import app  # noqa: E402
        from app import db as db_module  # noqa: E402

        self.app = app
        self.dbm = db_module
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------
    def add_evidence(self, eid, a, b):
        r = self.client.post(
            "/evidence", json={"evidence_id": eid, "reg_a": a, "reg_b": b}
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def create_handover(self, a="A", b="B"):
        r = self.client.post("/handovers", json={"reg_a": a, "reg_b": b})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def reverify(self, hid):
        r = self.client.post(f"/handovers/{hid}/reverify")
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def scope_rows(self, hid):
        conn = sqlite3.connect(self.db_path)
        try:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT register FROM handover_scope "
                    "WHERE handover_id=? ORDER BY register",
                    (hid,),
                )
            }
        finally:
            conn.close()

    def eligible_in_db(self, hid):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT reuse_eligible FROM handover WHERE id=?", (hid,)
            ).fetchone()[0]
        finally:
            conn.close()

    def install_spies(self):
        """统计全表读取 _active_edges 与双链求解 find_two_chains 的调用。"""
        dbm = self.dbm
        orig_scan = dbm.Database._active_edges
        orig_solve = dbm.find_two_chains
        counts = {"scan": 0, "solve": 0}

        def scan_spy(self, conn):
            counts["scan"] += 1
            return orig_scan(self, conn)

        def solve_spy(*args, **kwargs):
            counts["solve"] += 1
            return orig_solve(*args, **kwargs)

        p1 = patch.object(dbm.Database, "_active_edges", scan_spy)
        p2 = patch.object(dbm, "find_two_chains", solve_spy)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        return counts

    # -- tests ---------------------------------------------------------
    def test_scope_saved_for_pending_including_endpoints(self):
        # 只有一条链 A-M-B：pending，但可达范围仍含 A/M/B
        self.add_evidence("E1", "A", "M")
        self.add_evidence("E2", "M", "B")
        h = self.create_handover()
        self.assertEqual(h["status"], "pending")
        self.assertEqual(self.scope_rows(h["id"]), {"A", "M", "B"})
        self.assertEqual(self.eligible_in_db(h["id"]), 1)

    def test_no_change_reverification_reuses_and_skips_work(self):
        for eid, x, y in [
            ("E1", "A", "M1"), ("E2", "M1", "B"),
            ("E3", "A", "M2"), ("E4", "M2", "B"),
        ]:
            self.add_evidence(eid, x, y)
        h = self.create_handover()
        self.assertEqual(h["status"], "supplied")

        counts = self.install_spies()
        r = self.reverify(h["id"])
        self.assertEqual(r.headers["x-reverify-decision"], "reused")
        self.assertEqual(counts, {"scan": 0, "solve": 0})
        # 原响应：响应体与 GET 已保存结论完全一致，updated_at 不变
        self.assertEqual(r.json(),
                         self.client.get(f"/handovers/{h['id']}").json())
        self.assertEqual(r.json()["updated_at"], h["updated_at"])

        # 馆员反复手工重核：始终复用
        for _ in range(3):
            r = self.reverify(h["id"])
            self.assertEqual(r.headers["x-reverify-decision"], "reused")
        self.assertEqual(counts, {"scan": 0, "solve": 0})

    def test_unrelated_add_and_withdraw_reuse_for_pending_and_supplied(self):
        self.add_evidence("E1", "A", "M")
        self.add_evidence("E2", "M", "B")
        h = self.create_handover()
        self.assertEqual(h["status"], "pending")
        ua = h["updated_at"]

        # 无关片区 U-V：两端都在 {A,M,B} 外
        self.add_evidence("U1", "U", "V")
        counts = self.install_spies()
        r = self.reverify(h["id"])
        self.assertEqual(r.headers["x-reverify-decision"], "reused")
        self.assertEqual(r.json()["status"], "pending")
        self.assertEqual(r.json()["updated_at"], ua)
        self.assertEqual(counts, {"scan": 0, "solve": 0})

        # 无关证据撤回同样复用
        self.client.post("/evidence/U1/withdraw")
        r = self.reverify(h["id"])
        self.assertEqual(r.headers["x-reverify-decision"], "reused")
        self.assertEqual(counts, {"scan": 0, "solve": 0})

        # 补出第二条独立链 -> 涉及 A/B，提交时资格已撤销 -> 重判 supplied
        self.add_evidence("E3", "A", "N")
        self.add_evidence("E4", "N", "B")
        self.assertEqual(self.eligible_in_db(h["id"]), 0)
        r = self.reverify(h["id"])
        self.assertEqual(r.headers["x-reverify-decision"], "recomputed")
        self.assertEqual(r.json()["status"], "supplied")
        self.assertEqual(counts["solve"], 1)
        self.assertGreaterEqual(counts["scan"], 1)

        # 重判后范围扩大，再次重核立即恢复复用
        self.assertIn("N", self.scope_rows(h["id"]))
        r = self.reverify(h["id"])
        self.assertEqual(r.headers["x-reverify-decision"], "reused")
        self.assertEqual(counts["solve"], 1)

        # supplied 状态下，再来一条无关补证仍复用，依据不变
        basis_before = r.json()["chain1"], r.json()["chain2"]
        self.add_evidence("W1", "W", "Z")
        r = self.reverify(h["id"])
        self.assertEqual(r.headers["x-reverify-decision"], "reused")
        self.assertEqual((r.json()["chain1"], r.json()["chain2"]),
                         basis_before)

    def test_touching_middle_register_forces_recompute(self):
        # 范围 {A,M,B}；新证据 M-X 不碰申请端点 A/B，但碰中间登记号 M
        self.add_evidence("E1", "A", "M")
        self.add_evidence("E2", "M", "B")
        h = self.create_handover()
        self.add_evidence("EX", "M", "X")
        self.assertEqual(self.eligible_in_db(h["id"]), 0)

        counts = self.install_spies()
        r = self.reverify(h["id"])
        self.assertEqual(r.headers["x-reverify-decision"], "recomputed")
        self.assertEqual(counts["solve"], 1)
        # 这条枝杈不会造出双链，结论仍是 pending，但范围已把 X 纳入
        self.assertEqual(r.json()["status"], "pending")
        self.assertIn("X", self.scope_rows(h["id"]))

    def test_area_connect_then_cut(self):
        # 两条独立链
        for eid, x, y in [
            ("E1", "A", "M1"), ("E2", "M1", "B"),
            ("E3", "A", "M2"), ("E4", "M2", "B"),
        ]:
            self.add_evidence(eid, x, y)
        h = self.create_handover()
        self.assertEqual(h["status"], "supplied")
        hid = h["id"]

        # 片区接通：从范围内 M1 接到新片区节点 C，再由 C 接回 A，
        # 形成一条新的 A-C-M1... 路径（至少必须触发重判与范围扩张）
        self.add_evidence("C1", "M1", "C")
        r = self.reverify(hid)
        self.assertEqual(r.headers["x-reverify-decision"], "recomputed")
        self.assertIn("C", self.scope_rows(hid))
        self.assertEqual(r.json()["status"], "supplied")

        # 片区切断：撤回这条不在双链依据里的枝杈 C1。
        # 撤回接口的“依据索引”不含它 -> affected_handovers 为空，
        # 但可达范围含 M1 -> 旧结论在提交时已失去复用资格。
        wr = self.client.post("/evidence/C1/withdraw").json()
        self.assertEqual(wr["affected_handovers"], [])
        self.assertEqual(self.eligible_in_db(hid), 0)
        counts = self.install_spies()
        r = self.reverify(hid)
        self.assertEqual(r.headers["x-reverify-decision"], "recomputed")
        self.assertEqual(counts["solve"], 1)
        self.assertEqual(r.json()["status"], "supplied")

    def test_withdraw_basis_auto_recompute_then_manual_reuse(self):
        for eid, x, y in [
            ("E1", "A", "M1"), ("E2", "M1", "B"),
            ("E3", "A", "M2"), ("E4", "M2", "B"),
        ]:
            self.add_evidence(eid, x, y)
        h = self.create_handover()
        hid = h["id"]

        # 撤回整条依据链上的 E1 -> 同事务自动重核：只剩一条链 -> paused
        wr = self.client.post("/evidence/E1/withdraw")
        self.assertEqual(wr.status_code, 200)
        self.assertEqual(wr.json()["affected_handovers"], [hid])
        paused = self.client.get(f"/handovers/{hid}").json()
        self.assertEqual(paused["status"], "paused")
        # 自动重核保存的新结论同样具备复用资格
        self.assertEqual(self.eligible_in_db(hid), 1)

        counts = self.install_spies()
        r = self.reverify(hid)
        self.assertEqual(r.headers["x-reverify-decision"], "reused")
        self.assertEqual(r.json()["status"], "paused")
        self.assertEqual(counts, {"scan": 0, "solve": 0})

    def test_reverify_write_failure_keeps_last_conclusion(self):
        self.add_evidence("E1", "A", "M")
        self.add_evidence("E2", "M", "B")
        h = self.create_handover()
        ua = h["updated_at"]

        # 补一条涉及范围的证据 -> 资格撤销，强制走重新判定
        self.add_evidence("E3", "A", "N")

        original = self.dbm.Database._recompute

        def broken(self, conn, handover_id, a, b, on_failure="pending"):
            conn.execute(
                "UPDATE handover SET no_such_column = 1 WHERE id = ?",
                (handover_id,),
            )

        self.dbm.Database._recompute = broken
        try:
            r = self.client.post(f"/handovers/{h['id']}/reverify")
            self.assertEqual(r.status_code, 500)
        finally:
            self.dbm.Database._recompute = original

        # 上次结论仍可查，内容/时间戳不变
        again = self.client.get(f"/handovers/{h['id']}").json()
        self.assertEqual(again["status"], "pending")
        self.assertEqual(again["updated_at"], ua)

        # 故障排除后重核成功
        r = self.reverify(h["id"])
        self.assertEqual(r.headers["x-reverify-decision"], "recomputed")

    def test_failed_withdraw_rolls_back_invalidation_too(self):
        for eid, x, y in [
            ("E1", "A", "M1"), ("E2", "M1", "B"),
            ("E3", "A", "M2"), ("E4", "M2", "B"),
        ]:
            self.add_evidence(eid, x, y)
        h = self.create_handover()
        saved_basis = (h["chain1"]["evidence"], h["chain2"]["evidence"])

        original = self.dbm.Database._recompute

        def broken(self, conn, handover_id, a, b, on_failure="pending"):
            conn.execute(
                "UPDATE handover SET no_such_column = 1 WHERE id = ?",
                (handover_id,),
            )

        self.dbm.Database._recompute = broken
        try:
            r = self.client.post("/evidence/E1/withdraw")
            self.assertEqual(r.status_code, 500)
            # 回滚后资格仍是提交判定时的 1：复用快路不碰 _recompute，
            # 因此即使故障补丁还在，手工重核也能复用上次结论。
            rv = self.client.post(f"/handovers/{h['id']}/reverify")
            self.assertEqual(rv.status_code, 200)
            self.assertEqual(rv.headers["x-reverify-decision"], "reused")
        finally:
            self.dbm.Database._recompute = original

        ev = self.client.get("/evidence/E1").json()
        self.assertFalse(ev["withdrawn"])
        again = self.client.get(f"/handovers/{h['id']}").json()
        self.assertEqual(again["status"], "supplied")
        self.assertEqual(
            (again["chain1"]["evidence"], again["chain2"]["evidence"]),
            saved_basis,
        )

    def test_legacy_database_migrates_to_non_reusable_default(self):
        # 造一个旧版 schema 的库（没有 reachable/reuse_eligible/scope 表）
        legacy = os.path.join(self.tmp.name, "legacy.db")
        conn = sqlite3.connect(legacy)
        conn.executescript(
            """
            CREATE TABLE evidence (
                evidence_id TEXT PRIMARY KEY, reg_a TEXT NOT NULL,
                reg_b TEXT NOT NULL, withdrawn INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, withdrawn_at TEXT
            );
            CREATE TABLE handover (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reg_a TEXT NOT NULL, reg_b TEXT NOT NULL, status TEXT NOT NULL,
                chain1_regs TEXT, chain1_evidence TEXT,
                chain2_regs TEXT, chain2_evidence TEXT, reason TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(reg_a, reg_b)
            );
            CREATE TABLE handover_evidence (
                handover_id INTEGER NOT NULL, evidence_id TEXT NOT NULL,
                PRIMARY KEY (handover_id, evidence_id)
            );
            INSERT INTO evidence VALUES
                ('E1','A','M',0,'t',NULL), ('E2','M','B',0,'t',NULL);
            INSERT INTO handover(reg_a,reg_b,status,created_at,updated_at)
                VALUES ('A','B','pending','t','t');
            """
        )
        conn.commit()
        conn.close()

        db = self.dbm.Database(legacy)
        row = db.get_handover(1)
        self.assertEqual(row["status"], "pending")  # 旧结论照常可查
        # 旧行默认不可复用 -> 首次手工重核重新判定并补齐范围
        outcome = db.reverify_handover_ex(1)
        self.assertFalse(outcome.reused)
        self.assertTrue(outcome.handover["status"] in ("pending", "supplied"))
        self.assertEqual(
            {r for r, in sqlite3.connect(legacy).execute(
                "SELECT register FROM handover_scope WHERE handover_id=1")},
            {"A", "M", "B"},
        )
        outcome2 = db.reverify_handover_ex(1)
        self.assertTrue(outcome2.reused)


if __name__ == "__main__":
    unittest.main(verbosity=2)
