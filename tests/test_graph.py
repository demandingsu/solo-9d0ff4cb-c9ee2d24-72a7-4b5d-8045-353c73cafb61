"""find_two_chains 算法单元测试，直接用 python -I 运行。"""

import sys
import unittest

sys.path.insert(0, "/workspace")

from app.graph import Edge, find_two_chains  # noqa: E402


def chain_edges(a, chains):
    out = []
    for chain in chains:
        regs = [a] + [r for r, _ in chain]
        evs = [e for _, e in chain]
        out.append((regs, evs))
    return out


class FindTwoChainsTest(unittest.TestCase):
    def test_two_disjoint_chains(self):
        # A-B 两条完全独立的链：A-M1-N1-B 与 A-M2-N2-B
        edges = [
            Edge("E1", "A", "M1"), Edge("E2", "M1", "N1"),
            Edge("E3", "N1", "B"),
            Edge("E4", "A", "M2"), Edge("E5", "M2", "N2"),
            Edge("E6", "N2", "B"),
        ]
        chains = find_two_chains("A", "B", edges)
        self.assertIsNotNone(chains)
        (r1, e1), (r2, e2) = chain_edges("A", chains)
        self.assertEqual(r1[0], "A"); self.assertEqual(r1[-1], "B")
        self.assertEqual(r2[0], "A"); self.assertEqual(r2[-1], "B")
        self.assertEqual(len(e1), len(r1) - 1)
        # 中间登记号不共用
        self.assertEqual(set(r1[1:-1]) & set(r2[1:-1]), set())
        # 证据不共用
        self.assertEqual(set(e1) & set(e2), set())

    def test_shared_middle_register_rejected(self):
        # 所有路径都必须经过瓶颈点 M：点容量 1，找不到两条链
        edges = [
            Edge("E1", "A", "M"), Edge("E2", "A", "M"),
            Edge("E3", "M", "B"), Edge("E4", "M", "B"),
        ]
        self.assertIsNone(find_two_chains("A", "B", edges))

    def test_shared_evidence_rejected(self):
        # 只有一条桥证据 E* 连接两侧，证据容量 1，找不到两条链
        edges = [
            Edge("E1", "A", "X"), Edge("E2", "A", "Y"),
            Edge("E3", "X", "Z"), Edge("E4", "Y", "Z"),
            Edge("ES", "Z", "B"),
        ]
        self.assertIsNone(find_two_chains("A", "B", edges))

    def test_direct_plus_detour(self):
        # 一条直连证据 + 一条绕行链
        edges = [
            Edge("ED", "A", "B"),
            Edge("E1", "A", "M"), Edge("E2", "M", "B"),
        ]
        chains = find_two_chains("A", "B", edges)
        self.assertIsNotNone(chains)
        (r1, e1), (r2, e2) = chain_edges("A", chains)
        lengths = sorted((len(r1), len(r2)))
        self.assertEqual(lengths, [2, 3])  # 直连 2 个登记号，绕行 3 个
        self.assertEqual(set(e1) & set(e2), set())

    def test_reroute_after_edge_removal(self):
        # 初始三条独立路径，撤掉一条链上的证据后应换到第三条
        edges = [
            Edge("E1", "A", "M1"), Edge("E2", "M1", "B"),
            Edge("E3", "A", "M2"), Edge("E4", "M2", "B"),
            Edge("E5", "A", "M3"), Edge("E6", "M3", "B"),
        ]
        chains = find_two_chains("A", "B", edges)
        self.assertIsNotNone(chains)
        used = {e for _, evs in chain_edges("A", chains) for e in evs}
        # 撤掉所用证据中的一条（连同它的搭档整条路径作废）
        removed = sorted(used)[0]
        partner = "E2" if removed == "E1" else (
            "E1" if removed == "E2" else (
                "E4" if removed == "E3" else (
                    "E3" if removed == "E4" else (
                        "E6" if removed == "E5" else "E5"))))
        remaining = [e for e in edges if e.evidence_id not in (removed, partner)]
        chains2 = find_two_chains("A", "B", remaining)
        self.assertIsNotNone(chains2)
        used2 = {e for _, evs in chain_edges("A", chains2) for e in evs}
        self.assertNotIn(removed, used2)
        self.assertNotIn(partner, used2)

    def test_pause_when_only_one_chain_left(self):
        edges = [
            Edge("E1", "A", "M1"), Edge("E2", "M1", "B"),
            Edge("E3", "A", "M2"), Edge("E4", "M2", "B"),
        ]
        self.assertIsNotNone(find_two_chains("A", "B", edges))
        # 撤掉第二条路径的一条边后只剩一条链 -> 暂停
        remaining = [e for e in edges if e.evidence_id != "E3"]
        self.assertIsNone(find_two_chains("A", "B", remaining))

    def test_parallel_evidence_between_same_registers(self):
        # 同一对登记号之间两条不同证据：无中间点，仅证据不同，合法
        edges = [Edge("E1", "A", "B"), Edge("E2", "A", "B")]
        chains = find_two_chains("A", "B", edges)
        self.assertIsNotNone(chains)
        (_, e1), (_, e2) = chain_edges("A", chains)
        self.assertEqual(sorted(e1 + e2), ["E1", "E2"])

    def test_same_register_pair_rejected(self):
        self.assertIsNone(find_two_chains("A", "A", [Edge("E1", "A", "B")]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
