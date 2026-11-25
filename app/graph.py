"""同件证据链查找。

证据表中的每条证据把两个登记号连为一条无向边，证据号即边的编号。
一次交接需要两条能 *独立复核* 的同件证据链：

1. 两条链都从 ``a`` 出发、到 ``b`` 结束；
2. 除首尾两个登记号外，两条链不得共用任何登记号；
3. 两条链不得共用任何证据。

做法：对中间登记号做 *点拆点* 并把容量约束为 1，无向边的两个方向
各给容量 1，再用 Edmonds-Karp 求从 ``a`` 到 ``b`` 的最大流。最大流
达到 2 即存在两条满足约束的链；每次增广时直接记录增广路，天然把
流分解为两条源到汇的路径，无需再从残余网络反推。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Edge:
    evidence_id: str
    u: str
    v: str


@dataclass
class _Arc:
    """残余网络中的一条有向弧。"""

    to: int
    cap: int  # 残余容量
    label: str | None  # 经过该弧使用的证据号；拆点弧为 None
    reverse: int  # 反向弧在邻接表中的下标


class _MaxFlow:
    def __init__(self, n: int) -> None:
        self.g: list[list[_Arc]] = [[] for _ in range(n)]
        # 每次成功增广对应的弧路径：[(起点节点, 弧在其邻接表中的下标)]
        self.flow_paths: list[list[tuple[int, int]]] = []

    def add_arc(self, u: int, v: int, cap: int, label: str | None = None) -> None:
        forward = _Arc(to=v, cap=cap, label=label, reverse=len(self.g[v]))
        backward = _Arc(to=u, cap=0, label=None, reverse=len(self.g[u]))
        self.g[u].append(forward)
        self.g[v].append(backward)

    def augment(self, source: int, sink: int) -> bool:
        """BFS 找一条增广路，推 1 个单位流，并记录该路径。"""
        n = len(self.g)
        prev: list[tuple[int, int] | None] = [None] * n
        prev[source] = (-1, -1)
        queue = deque([source])
        while queue:
            x = queue.popleft()
            if x == sink:
                break
            for idx, arc in enumerate(self.g[x]):
                if arc.cap > 0 and prev[arc.to] is None:
                    prev[arc.to] = (x, idx)
                    queue.append(arc.to)
        if prev[sink] is None:
            return False

        arc_path: list[tuple[int, int]] = []
        node = sink
        while node != source:
            px, idx = prev[node]  # type: ignore[misc]
            arc = self.g[px][idx]
            arc.cap -= 1
            self.g[node][arc.reverse].cap += 1
            arc_path.append((px, idx))
            node = px
        arc_path.reverse()
        self.flow_paths.append(arc_path)
        return True

    def max_flow(self, source: int, sink: int, limit: int = 2) -> int:
        flow = 0
        while flow < limit and self.augment(source, sink):
            flow += 1
        return flow


def find_two_chains(
    a: str,
    b: str,
    edges: list[Edge],
) -> list[list[tuple[str, str]]] | None:
    """返回两条链；找不到满足约束的两条链时返回 ``None``。

    每条链表示为 ``[(到达登记号, 使用证据号), ...]`` 的边序列，
    首登记号 ``a`` 由调用方补全，末项的到达登记号为 ``b``。
    """
    if a == b:
        return None

    nodes = {a, b}
    for e in edges:
        nodes.add(e.u)
        nodes.add(e.v)
    node_list = sorted(nodes)
    index = {name: i for i, name in enumerate(node_list)}
    n = len(node_list)

    # 拆点：i_in = i, i_out = i + n
    def vin(name: str) -> int:
        return index[name]

    def vout(name: str) -> int:
        return index[name] + n

    source = vout(a)  # 起点直接从出点出发，不受点容量限制
    sink = vin(b)  # 终点到入点结束，不受点容量限制
    mf = _MaxFlow(2 * n)

    # 点容量：中间登记号容量 1（两条链不能共用）；首尾允许 2
    for name in node_list:
        cap = 2 if name in (a, b) else 1
        mf.add_arc(vin(name), vout(name), cap, None)

    # 无向边：两个方向各容量 1
    # —— 同一证据不能被两条链共用，也不能在链内来回走
    for e in edges:
        mf.add_arc(vout(e.u), vin(e.v), 1, e.evidence_id)
        mf.add_arc(vout(e.v), vin(e.u), 1, e.evidence_id)

    if mf.max_flow(source, sink, limit=2) < 2:
        return None

    chains = []
    for arc_path in mf.flow_paths:
        chain: list[tuple[str, str]] = []
        for node, arc_idx in arc_path:
            arc = mf.g[node][arc_idx]
            if arc.label is not None:
                # 弧指向某登记号的 in 点，对回登记号名
                chain.append((node_list[arc.to % n], arc.label))
        chains.append(chain)

    if not _validate(a, b, chains):
        return None
    return chains


def _validate(a: str, b: str, chains: list[list[tuple[str, str]]]) -> bool:
    """防御性校验：两条链证据互斥、中间登记号互斥、各自为简单路径。"""
    seen_evidence: set[str] = set()
    seen_middle: set[str] = set()
    for chain in chains:
        if not chain:
            return False
        regs = [a] + [reg for reg, _ in chain]
        if regs[-1] != b:
            return False
        if len(regs) != len(set(regs)):
            return False
        for reg in regs[1:-1]:
            if reg in seen_middle:
                return False
            seen_middle.add(reg)
        for _, ev in chain:
            if ev in seen_evidence:
                return False
            seen_evidence.add(ev)
    return True
