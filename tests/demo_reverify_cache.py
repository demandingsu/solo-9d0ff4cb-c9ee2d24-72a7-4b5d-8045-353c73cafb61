"""真实 HTTP 演示“可达范围 + 手工重核复用”。

用法（先起服务，例如）：
  HANDOVER_DB=/tmp/demo-cache.db \\
    /tmp/venv/bin/python -m uvicorn app.main:app --port 8000 &
  /tmp/venv/bin/python -I tests/demo_reverify_cache.py

脚本每次用独立前缀，可对同一个库反复运行。演示：
  1. 无关补证 -> 反复重核都 reused（响应体/updated_at 不变，跳过全表与求解）；
  2. 片区接通（范围内节点接入新片区并凑齐双链）-> recomputed -> supplied；
  3. 片区切断（撤回范围内、但不在当前依据中的枝杈证据）-> 提交时失效、
     手工重核 recomputed；撤回依据证据则自动重核 paused，之后无变化再
     手工重核会 reused 这份 paused 结论；
  4. 补证恢复 supplied。

失败回滚无法纯黑盒注入，由 tests/test_reverify_cache.py 与
tests/test_api.py 中的故障注入用例覆盖。
"""

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
RUN = f"R{int(time.time())}"


def reg(name: str) -> str:
    return f"{RUN}_{name}"


def show(title: str, payload) -> None:
    print(f"\n===== {title} =====")
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def add(c: httpx.Client, eid: str, a: str, b: str) -> None:
    r = c.post("/evidence", json={
        "evidence_id": f"{RUN}_{eid}", "reg_a": reg(a), "reg_b": reg(b)})
    r.raise_for_status()


def reverify(c: httpx.Client, hid: int, last_updated: str | None):
    r = c.post(f"/handovers/{hid}/reverify")
    r.raise_for_status()
    decision = r.headers.get("x-reverify-decision", "?")
    body = r.json()
    changed = (last_updated is not None
               and body["updated_at"] != last_updated)
    print(
        f"  POST /handovers/{hid}/reverify -> "
        f"X-Reverify-Decision={decision}, status={body['status']}, "
        f"updated_at={body['updated_at']}, "
        f"updated_at_changed_vs_prev={changed}"
    )
    return decision, body


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=10)
    A, B, M, N = reg("A"), reg("B"), reg("M"), reg("N")

    # -- 初始：只有一条链 A-M-B -> pending，可达范围 {A,M,B} ------------
    add(c, "E1", "A", "M")
    add(c, "E2", "M", "B")
    r = c.post("/handovers", json={"reg_a": A, "reg_b": B})
    r.raise_for_status()
    h = r.json()
    hid = h["id"]
    show("申请交接（单链，期望 pending）", h)
    assert h["status"] == "pending"
    updated = h["updated_at"]

    # -- 1a. 证据没变：直接复用 -----------------------------------------
    print("\n--- 1. 无关补证 / 无变化：期望全部 reused ---")
    decision, h = reverify(c, hid, updated)
    assert decision == "reused" and h["updated_at"] == updated

    # -- 1b. 无关片区 U-V（两端都在范围外），可反复补、反复核 ------------
    add(c, "U1", "U", "V")
    decision, h = reverify(c, hid, updated)
    assert decision == "reused" and h["updated_at"] == updated
    add(c, "U2", "U", "W")  # 仍在范围外的片区里
    decision, h = reverify(c, hid, updated)
    assert decision == "reused" and h["updated_at"] == updated
    print("  （馆员反复手工重核，均复用同一份 pending 结论与时间戳）")

    # -- 2. 片区接通：从 A 经 N 到 B 补出第二条独立链 -------------------
    print("\n--- 2. 片区接通：补第二条独立链，期望 recomputed -> supplied ---")
    add(c, "C1", "A", "N")  # 端点 A 在范围内 -> 提交时旧结论失效
    add(c, "C2", "N", "B")
    decision, h = reverify(c, hid, updated)
    assert decision == "recomputed"
    assert h["status"] == "supplied", h
    updated = h["updated_at"]
    show("重新判定后的双链结论", h)

    # 紧接着再核一次：无新变化 -> reused supplied
    decision, h2 = reverify(c, hid, updated)
    assert decision == "reused" and h2["updated_at"] == updated

    # -- 3a. 片区切断（不在依据中的枝杈） -------------------------------
    print("\n--- 3a. 片区切断：撤回范围内但不在依据中的枝杈 M-L ---")
    add(c, "B1", "M", "L")
    decision, h = reverify(c, hid, updated)
    assert decision == "recomputed"
    updated = h["updated_at"]
    wid = f"{RUN}_B1"
    r = c.post(f"/evidence/{wid}/withdraw")
    r.raise_for_status()
    wr = r.json()
    print(f"  withdraw {wid}: affected_handovers={wr['affected_handovers']}"
          "（依据索引不含枝杈，故不自动重核；但复用资格已在提交时撤销）")
    assert wr["affected_handovers"] == []
    decision, h = reverify(c, hid, updated)
    assert decision == "recomputed"  # 涉及范围内 M -> 必须重判
    assert h["status"] == "supplied"  # 双链依据未受损
    updated = h["updated_at"]

    # -- 3b. 切断依据证据：同事务自动重核 -> paused ----------------------
    print("\n--- 3b. 切断双链依据 E1：期望撤回接口自动重核 -> paused ---")
    victim = f"{RUN}_E1"
    r = c.post(f"/evidence/{victim}/withdraw")
    r.raise_for_status()
    wr = r.json()
    print(f"  withdraw {victim}: affected_handovers={wr['affected_handovers']}")
    assert hid in wr["affected_handovers"]
    paused = c.get(f"/handovers/{hid}").json()
    assert paused["status"] == "paused"
    updated = paused["updated_at"]
    # 无新变化的手工重核：复用自动重核保存的 paused 结论
    decision, h = reverify(c, hid, updated)
    assert decision == "reused" and h["status"] == "paused"

    # -- 4. 补一条全新的第二链恢复 supplied ------------------------------
    print("\n--- 4. 补证恢复：A-P-B 与尚存的 A-N-B 组成双链 ---")
    add(c, "R1", "A", "P")
    add(c, "R2", "P", "B")
    decision, h = reverify(c, hid, updated)
    assert decision == "recomputed" and h["status"] == "supplied", h
    show("恢复供数结论", h)

    print("\n演示通过：无关增撤复用、片区接通/切断重判、依据撤回自动重核"
          "与恢复均符合预期。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
