"""交接 API 端到端测试（使用 FastAPI TestClient + 临时 SQLite）。

运行：/tmp/venv/bin/python -I tests/test_api.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, "/workspace")

from fastapi.testclient import TestClient  # noqa: E402


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["HANDOVER_DB"] = os.path.join(self.tmp.name, "test.db")
        # 确保模块以测试库路径重新初始化
        for name in list(sys.modules):
            if name.startswith("app."):
                del sys.modules[name]
        from app.main import app  # noqa: E402

        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_evidence(self, eid, a, b):
        r = self.client.post(
            "/evidence", json={"evidence_id": eid, "reg_a": a, "reg_b": b}
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_full_workflow_supplied_pending_reroute_pause(self):
        c = self.client

        # 证据拓扑（A↔B 的候选路径）：
        # 路径1: A-M1-N1-B (E1,E2,E3)
        # 路径2: A-M2-N2-B (E4,E5,E6)
        # 备用 : A-M3-N3-B (E7,E8,E9)
        for eid, x, y in [
            ("E1", "A", "M1"), ("E2", "M1", "N1"), ("E3", "N1", "B"),
            ("E4", "A", "M2"), ("E5", "M2", "N2"), ("E6", "N2", "B"),
            ("E7", "A", "M3"), ("E8", "M3", "N3"), ("E9", "N3", "B"),
        ]:
            self.add_evidence(eid, x, y)

        # 健康检查与列表
        self.assertEqual(c.get("/health").json()["status"], "ok")
        self.assertEqual(len(c.get("/evidence").json()), 9)

        # 申请交接：两条链齐备 -> supplied，附两条链的登记号与证据号
        r = c.post("/handovers", json={"reg_a": "A", "reg_b": "B"})
        self.assertEqual(r.status_code, 201, r.text)
        h = r.json()
        self.assertEqual(h["status"], "supplied")
        self.assertEqual(h["reg_a"], "A")  # 规范化后 A 在前
        for key in ("chain1", "chain2"):
            self.assertEqual(h[key]["registrations"][0], "A")
            self.assertEqual(h[key]["registrations"][-1], "B")
            self.assertEqual(
                len(h[key]["evidence"]),
                len(h[key]["registrations"]) - 1,
            )
        ev1, ev2 = set(h["chain1"]["evidence"]), set(h["chain2"]["evidence"])
        self.assertEqual(ev1 & ev2, set())  # 证据不共用
        mid1 = set(h["chain1"]["registrations"][1:-1])
        mid2 = set(h["chain2"]["registrations"][1:-1])
        self.assertEqual(mid1 & mid2, set())  # 中间登记号不共用
        hid = h["id"]

        # 查询直接读已保存结论，两次一致
        self.assertEqual(c.get(f"/handovers/{hid}").json()["updated_at"],
                         h["updated_at"])

        # 重复申请冲突
        r = c.post("/handovers", json={"reg_a": "B", "reg_b": "A"})
        self.assertEqual(r.status_code, 409)

        # 证据号重复冲突
        r = c.post("/evidence",
                   json={"evidence_id": "E1", "reg_a": "X", "reg_b": "Y"})
        self.assertEqual(r.status_code, 409)

        # ---- 试办“换链”：撤回当前依据中的一条证据 ----
        withdrawn = sorted(ev1)[0]
        r = c.post(f"/evidence/{withdrawn}/withdraw")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["withdrawn_evidence"], withdrawn)
        self.assertEqual(body["affected_handovers"], [hid])  # 只重核用到它的
        h2 = c.get(f"/handovers/{hid}").json()
        self.assertEqual(h2["status"], "supplied")  # 仍能凑齐两条 -> 继续供数
        new_ev = set(h2["chain1"]["evidence"]) | set(h2["chain2"]["evidence"])
        self.assertNotIn(withdrawn, new_ev)  # 依据已更新
        self.assertEqual(h2["updated_at"] >= h["updated_at"], True)

        # 未受影响的证据撤回不会触发该交接对重核
        self.add_evidence("EX", "X1", "X2")  # 与 A-B 完全无关
        r = c.post("/evidence/EX/withdraw")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["affected_handovers"], [])
        self.assertEqual(c.get(f"/handovers/{hid}").json()["status"],
                         "supplied")

        # ---- 试办“暂停供数”：把当前依据所用证据全部撤回 ----
        snapshot = c.get(f"/handovers/{hid}").json()
        used = set(snapshot["chain1"]["evidence"]) \
            | set(snapshot["chain2"]["evidence"])
        for ev in sorted(used):
            r = c.post(f"/evidence/{ev}/withdraw")
            self.assertEqual(r.status_code, 200)
        final = c.get(f"/handovers/{hid}").json()
        self.assertEqual(final["status"], "paused")
        self.assertIsNone(final["chain1"]["evidence"])
        self.assertIsNone(final["chain2"]["evidence"])
        self.assertIsNotNone(final["reason"])
        # 暂停结论已保存：列表接口读到的也是同一份
        self.assertEqual(c.get("/handovers").json()[0]["status"], "paused")

    def test_pending_then_reverify_after_more_evidence(self):
        c = self.client
        # 只有一条链时申请 -> 待补
        self.add_evidence("E1", "A", "M1")
        self.add_evidence("E2", "M1", "B")
        r = c.post("/handovers", json={"reg_a": "A", "reg_b": "B"})
        self.assertEqual(r.json()["status"], "pending")
        self.assertIsNotNone(r.json()["reason"])
        hid = r.json()["id"]

        # 待补期间查询不重算，结论保持
        self.assertEqual(c.get(f"/handovers/{hid}").json()["status"],
                         "pending")

        # 补入第二条独立链的证据后手工重核 -> 供数
        self.add_evidence("E3", "A", "M2")
        self.add_evidence("E4", "M2", "B")
        r = c.post(f"/handovers/{hid}/reverify")
        self.assertEqual(r.status_code, 200, r.text)
        h = r.json()
        self.assertEqual(h["status"], "supplied")
        ev = set(h["chain1"]["evidence"]) | set(h["chain2"]["evidence"])
        self.assertEqual(ev, {"E1", "E2", "E3", "E4"})

    def test_withdraw_nonexistent_and_duplicate_withdraw(self):
        c = self.client
        self.add_evidence("E1", "A", "B")
        self.assertEqual(c.post("/evidence/NOPE/withdraw").status_code, 404)
        self.assertEqual(c.post("/evidence/E1/withdraw").status_code, 200)
        self.assertEqual(c.post("/evidence/E1/withdraw").status_code, 404)
        # 已撤回证据默认不出现在列表
        self.assertEqual(c.get("/evidence").json(), [])
        self.assertEqual(len(c.get(
            "/evidence", params={"include_withdrawn": "true"}).json()), 1)

    def test_validation_errors(self):
        c = self.client
        self.assertEqual(
            c.post("/evidence",
                   json={"evidence_id": "E", "reg_a": "A", "reg_b": "A"})
            .status_code, 400)
        self.assertEqual(
            c.post("/handovers",
                   json={"reg_a": "A", "reg_b": "A"}).status_code, 400)
        self.assertEqual(c.get("/handovers/999").status_code, 404)

    def test_write_failure_keeps_last_saved_conclusion(self):
        """撤回事务中途写入失败 -> 整体回滚：证据仍有效、结论仍是上次结果。"""
        c = self.client
        for eid, x, y in [
            ("E1", "A", "M1"), ("E2", "M1", "B"),
            ("E3", "A", "M2"), ("E4", "M2", "B"),
        ]:
            self.add_evidence(eid, x, y)
        h = c.post("/handovers",
                   json={"reg_a": "A", "reg_b": "B"}).json()
        self.assertEqual(h["status"], "supplied")
        saved_basis = (h["chain1"]["evidence"], h["chain2"]["evidence"])

        from app import db as db_module

        # 让重核落库时的 UPDATE 必然失败，模拟写入故障
        original = db_module.Database._recompute

        def broken(self, conn, handover_id, a, b, on_failure="pending"):
            conn.execute("UPDATE handover SET no_such_column = 1 WHERE id = ?",
                         (handover_id,))

        db_module.Database._recompute = broken
        try:
            r = c.post("/evidence/E1/withdraw")
            self.assertEqual(r.status_code, 500)
        finally:
            db_module.Database._recompute = original

        # 撤回与重核一起回滚：证据未被撤回
        ev = c.get("/evidence/E1").json()
        self.assertFalse(ev["withdrawn"])
        # 查询读到的仍是上次保存的供数结论，依据不变
        again = c.get(f"/handovers/{h['id']}").json()
        self.assertEqual(again["status"], "supplied")
        self.assertEqual(
            (again["chain1"]["evidence"], again["chain2"]["evidence"]),
            saved_basis,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
