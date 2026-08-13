"""Thin Neo4j / offline-fixture client for ORBIT KG examples."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args, **_kwargs):
        return False


class OrbitKGClient:
    """Query the ORBIT knowledge graph.

    Modes
    -----
    - ``neo4j``: live Bolt connection (requires ``neo4j`` package + credentials)
    - ``fixture``: return pre-recorded rows keyed by query name (offline demos)
    """

    def __init__(
        self,
        *,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        fixture_path: Optional[str | Path] = None,
    ) -> None:
        load_dotenv()
        self.uri = uri or os.getenv("NEO4J_URI")
        self.user = user or os.getenv("NEO4J_USER", "neo4j")
        self.password = password or os.getenv("NEO4J_PASSWORD")
        self.database = database or os.getenv("NEO4J_DATABASE", "neo4j")
        self._driver = None
        self._fixtures: Dict[str, Any] = {}

        if fixture_path is not None:
            path = Path(fixture_path)
            self._fixtures = json.loads(path.read_text(encoding="utf-8"))

        if self.uri and self.password and fixture_path is None:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(
                self.uri, auth=(self.user, self.password)
            )

    @property
    def mode(self) -> str:
        if self._driver is not None:
            return "neo4j"
        if self._fixtures:
            return "fixture"
        return "unconfigured"

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def __enter__(self) -> "OrbitKGClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def run(
        self,
        cypher: str,
        parameters: Optional[Mapping[str, Any]] = None,
        *,
        name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Execute Cypher, or look up a named fixture when offline."""
        parameters = dict(parameters or {})

        if self._driver is not None:
            with self._driver.session(database=self.database) as session:
                result = session.run(cypher, parameters)
                return [dict(record) for record in result]

        if name and name in self._fixtures:
            payload = self._fixtures[name]
            if isinstance(payload, dict) and "rows" in payload:
                return list(payload["rows"])
            if isinstance(payload, list):
                return list(payload)
            raise TypeError(f"Fixture {name!r} must be a list or {{'rows': [...]}}")

        raise RuntimeError(
            "OrbitKGClient is not connected. Set NEO4J_* env vars for a live "
            "graph, or pass fixture_path=... and call run(..., name='query_id')."
        )

    def run_named(self, name: str, **parameters: Any) -> List[Dict[str, Any]]:
        """Run a fixture-backed named query, or its Cypher if present online."""
        if name not in self._fixtures:
            raise KeyError(f"Unknown query name: {name}")
        entry = self._fixtures[name]
        cypher = entry.get("cypher", "") if isinstance(entry, dict) else ""
        if self._driver is not None and cypher:
            return self.run(cypher, parameters, name=name)
        return self.run("", parameters, name=name)

    def list_fixture_queries(self) -> Iterable[str]:
        return sorted(self._fixtures.keys())
