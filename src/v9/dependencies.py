"""Versioned dependency DAG and consumer-scoped eligibility."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import Quality, Status, digest, text
from .quality import FixturePolicy


@dataclass(frozen=True, slots=True)
class Node:
    name: str
    mandatory: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    required_policy: tuple[str, ...] = ()

    def __post_init__(self):
        text(self.name)
        for group in (self.mandatory, self.optional, self.required_policy):
            if type(group) is not tuple:
                raise TypeError("dependencies must be immutable tuples")
            for item in group:
                text(item)
        if set(self.mandatory) & set(self.optional):
            raise ValueError("dependency cannot be both mandatory and optional")


class Graph:
    def __init__(self, version: str, nodes: tuple[Node, ...]):
        text(version)
        if type(nodes) is not tuple or any(type(node) is not Node for node in nodes):
            raise TypeError("nodes must be an immutable tuple of Node values")
        self.version = version
        self.nodes = nodes
        by_name = {n.name: n for n in nodes}
        if len(by_name) != len(nodes):
            raise ValueError("duplicate node")
        visiting, done, order = set(), set(), []

        def visit(name):
            if name not in by_name:
                raise ValueError(f"unresolved dependency: {name}")
            if name in visiting:
                raise ValueError(f"dependency cycle: {name}")
            if name in done:
                return
            visiting.add(name)
            for child in by_name[name].mandatory + by_name[name].optional:
                visit(child)
            visiting.remove(name)
            done.add(name)
            order.append(name)

        for name in by_name:
            visit(name)
        self.order = tuple(order)

    def eligible(
        self, name: str, evidence: dict[str, Quality], policy: tuple[FixturePolicy, ...]
    ) -> tuple[bool, tuple[str, ...]]:
        nodes = {n.name: n for n in self.nodes}
        if name not in nodes:
            raise ValueError(f"unknown node: {name}")
        if type(policy) is not tuple or any(type(p) is not FixturePolicy for p in policy):
            raise ValueError("POLICY_INVALID")
        policies = {p.name: p for p in policy}
        conflicts = {
            p.name for p in policy if any(other.name == p.name and other != p for other in policy)
        }
        reasons = []

        def check(key):
            node = nodes[key]
            if key not in evidence or evidence[key].status != Status.VALID:
                reasons.append(f"invalid:{key}")
            for required in node.required_policy:
                if required in conflicts:
                    reasons.append(f"conflicting_policy:{required}")
                elif required not in policies:
                    reasons.append(f"missing_policy:{required}")
                elif key in evidence and (
                    evidence[key].policy_name != required
                    or evidence[key].policy_version != policies[required].version
                    or evidence[key].policy_digest != digest(policies[required])
                ):
                    reasons.append(f"policy_mismatch:{key}:{required}")
            for child in node.mandatory:
                check(child)

        check(name)
        return not reasons, tuple(reasons)

    def policy_diagnostics(self, policy: tuple[FixturePolicy, ...]) -> tuple[str, ...]:
        if type(policy) is not tuple or any(type(p) is not FixturePolicy for p in policy):
            raise ValueError("POLICY_INVALID")
        return tuple(
            f"conflicting_policy:{name}"
            for name in sorted(
                {p.name for p in policy if any(q.name == p.name and q != p for q in policy)}
            )
        )

    def optional_diagnostics(self, name: str, evidence: dict[str, Quality]) -> tuple[str, ...]:
        nodes = {node.name: node for node in self.nodes}
        if name not in nodes:
            raise ValueError(f"unknown node: {name}")
        return tuple(
            f"optional:{dep}:{evidence[dep].status.value if dep in evidence else 'MISSING'}"
            for dep in nodes[name].optional
            if dep not in evidence or evidence[dep].status != Status.VALID
        )
