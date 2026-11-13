"""Versioned dependency DAG and consumer-scoped eligibility."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import Quality, Status, text


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
        if type(nodes) is not tuple:
            raise TypeError("nodes must be a tuple")
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
        self, name: str, evidence: dict[str, Quality], policy: frozenset[str]
    ) -> tuple[bool, tuple[str, ...]]:
        nodes = {n.name: n for n in self.nodes}
        if name not in nodes:
            raise ValueError(f"unknown node: {name}")
        reasons = []

        def check(key):
            node = nodes[key]
            if key not in evidence or evidence[key].status != Status.VALID:
                reasons.append(f"invalid:{key}")
            reasons.extend(f"missing_policy:{p}" for p in node.required_policy if p not in policy)
            for child in node.mandatory:
                check(child)

        check(name)
        return not reasons, tuple(reasons)
