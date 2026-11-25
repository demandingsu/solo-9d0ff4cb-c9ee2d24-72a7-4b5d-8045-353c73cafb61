"""通过真实 HTTP 调用试办一次完整交接（供数 -> 撤回换链 -> 暂停 -> 补证恢复）。

用法：
  HANDOVER_DB=/tmp/demo.db /tmp/venv/bin/python -m uvicorn app.main:app &
  /tmp/venv/bin/python -I tests/demo_http.py
"""

import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"


def show(title, payload):
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=10)

    # 三条互不相交的 A->B 候选链
    evidence = [
        ("E1", "A", "M1"), ("E2", "M1", "N1"), ("E3", "N1", "B"),
        ("E4", "A", "M2"), ("E5", "M2", "N2"), ("E6", "N2", "B"),
        ("E7", "A", "M3"), ("E8", "M3", "N3"), ("E9", "N3", "B"),
    ]
    for eid, x, y in evidence:
        r = c.post("/evidence",
                   json={"evidence_id": eid, "reg_a": x, "reg_b": y})
        r.raise_for_status()

    r = c.post("/handovers", json={"reg_a": "A", "reg_b": "B"})
    r.raise_for_status()
    handover = r.json()
    show("申请交接（期望 supplied，附两条链）", handover)
    assert handover["status"] == "supplied"
    hid = handover["id"]
    used_chain1 = set(handover["chain1"]["evidence"])

    # 撤回第一条链上的一条证据 -> 应换链继续供数
    victim = sorted(used_chain1)[0]
    r = c.post(f"/evidence/{victim}/withdraw")
    r.raise_for_status()
    show(f"撤回证据 {victim}（期望换链、仍 supplied）", r.json())
    after = c.get(f"/handovers/{hid}").json()
    assert after["status"] == "supplied"
    assert victim not in (
        after["chain1"]["evidence"] + after["chain2"]["evidence"])

    # 把当前依据全部撤回 -> 暂停供数
    current = set(after["chain1"]["evidence"]) | set(
        after["chain2"]["evidence"])
    for ev in sorted(current):
        c.post(f"/evidence/{ev}/withdraw").raise_for_status()
    paused = c.get(f"/handovers/{hid}").json()
    show("依据全部撤回（期望 paused，链为空、带原因）", paused)
    assert paused["status"] == "paused"

    # 补一条全新的双链证据并重核 -> 恢复供数
    for eid, x, y in [
        ("R1", "A", "P1"), ("R2", "P1", "B"),
        ("R3", "A", "P2"), ("R4", "P2", "B"),
    ]:
        c.post("/evidence",
               json={"evidence_id": eid, "reg_a": x, "reg_b": y}
               ).raise_for_status()
    r = c.post(f"/handovers/{hid}/reverify")
    r.raise_for_status()
    show("补证后手工重核（期望恢复 supplied）", r.json())
    assert r.json()["status"] == "supplied"

    print("\n全部试办步骤通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
