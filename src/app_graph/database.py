"""SQLite persistence for explored UI states and transitions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Action


@dataclass(frozen=True)
class State:
    id: int
    depth: int
    activity: str
    screenshot_path: str
    xml_path: str
    phash: str
    visit_count: int
    created_at: str


@dataclass(frozen=True)
class StateVariant:
    id: int
    state_id: int
    screenshot_path: str
    xml_path: str
    phash: str
    created_at: str


class GraphDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS states (
                id INTEGER PRIMARY KEY,
                depth INTEGER NOT NULL,
                activity TEXT NOT NULL DEFAULT '',
                screenshot_path TEXT NOT NULL,
                xml_path TEXT NOT NULL,
                phash TEXT NOT NULL,
                visit_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS variants (
                id INTEGER PRIMARY KEY,
                state_id INTEGER NOT NULL REFERENCES states(id) ON DELETE CASCADE,
                screenshot_path TEXT NOT NULL,
                xml_path TEXT NOT NULL,
                phash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_variants_state ON variants(state_id);
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY,
                state_id INTEGER NOT NULL REFERENCES states(id) ON DELETE CASCADE,
                action_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                data_json TEXT NOT NULL,
                attempted INTEGER NOT NULL DEFAULT 0,
                UNIQUE(state_id, action_key)
            );
            CREATE TABLE IF NOT EXISTS edges (
                id INTEGER PRIMARY KEY,
                from_state INTEGER NOT NULL REFERENCES states(id) ON DELETE CASCADE,
                action_id INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
                to_state INTEGER NOT NULL REFERENCES states(id) ON DELETE CASCADE,
                UNIQUE(from_state, action_id)
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def bind_package(self, package: str) -> None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'package'"
        ).fetchone()
        if row and row["value"] != package:
            raise ValueError(
                f"Output directory is already bound to {row['value']!r}, not {package!r}; use another --output directory."
            )
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('package', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (package,),
        )
        self.connection.commit()

    def add_state(
        self,
        depth: int,
        activity: str,
        screenshot_path: str,
        xml_path: str,
        phash: int,
    ) -> State:
        cursor = self.connection.execute(
            "INSERT INTO states(depth, activity, screenshot_path, xml_path, phash) VALUES (?, ?, ?, ?, ?)",
            (depth, activity, screenshot_path, xml_path, f"{phash:016x}"),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM states WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        assert row is not None
        return State(**dict(row))

    def get_state(self, state_id: int) -> State:
        row = self.connection.execute("SELECT * FROM states WHERE id = ?", (state_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown state {state_id}")
        return State(**dict(row))

    def update_min_depth(self, state_id: int, depth: int) -> None:
        self.connection.execute(
            "UPDATE states SET depth = MIN(depth, ?) WHERE id = ?", (depth, state_id)
        )
        self.connection.commit()

    def states(self) -> list[State]:
        rows = self.connection.execute("SELECT * FROM states ORDER BY id").fetchall()
        return [State(**dict(row)) for row in rows]

    def variants(self, state_id: int) -> list[StateVariant]:
        rows = self.connection.execute(
            "SELECT * FROM variants WHERE state_id = ? ORDER BY id", (state_id,)
        ).fetchall()
        return [StateVariant(**dict(row)) for row in rows]

    def add_variant(
        self, state_id: int, screenshot_path: str, xml_path: str, phash: int
    ) -> StateVariant:
        cursor = self.connection.execute(
            "INSERT INTO variants(state_id, screenshot_path, xml_path, phash) VALUES (?, ?, ?, ?)",
            (state_id, screenshot_path, xml_path, f"{phash:016x}"),
        )
        self.connection.execute("UPDATE states SET visit_count = visit_count + 1 WHERE id = ?", (state_id,))
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM variants WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        assert row is not None
        return StateVariant(**dict(row))

    def increment_visit(self, state_id: int) -> None:
        self.connection.execute("UPDATE states SET visit_count = visit_count + 1 WHERE id = ?", (state_id,))
        self.connection.commit()

    def add_edge(self, from_state: int, action: Action, to_state: int) -> None:
        cursor = self.connection.execute(
            "INSERT INTO actions(state_id, action_key, kind, data_json, attempted) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(state_id, action_key) DO UPDATE SET attempted = 1 RETURNING id",
            (from_state, action.key, action.kind, json.dumps(action.as_dict(), ensure_ascii=False)),
        )
        action_id = cursor.fetchone()[0]
        self.connection.execute(
            "INSERT INTO edges(from_state, action_id, to_state) VALUES (?, ?, ?) "
            "ON CONFLICT(from_state, action_id) DO UPDATE SET to_state = excluded.to_state",
            (from_state, action_id, to_state),
        )
        self.connection.commit()

    def add_action(self, state_id: int, action: Action) -> int:
        cursor = self.connection.execute(
            "INSERT INTO actions(state_id, action_key, kind, data_json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(state_id, action_key) DO UPDATE SET action_key = excluded.action_key RETURNING id",
            (state_id, action.key, action.kind, json.dumps(action.as_dict(), ensure_ascii=False)),
        )
        action_id = int(cursor.fetchone()[0])
        self.connection.commit()
        return action_id

    def has_actions(self, state_id: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM actions WHERE state_id = ? LIMIT 1", (state_id,)
        ).fetchone() is not None

    def pending_actions(self, state_id: int) -> list[tuple[int, Action]]:
        rows = self.connection.execute(
            "SELECT id, data_json FROM actions WHERE state_id = ? AND attempted = 0 ORDER BY id",
            (state_id,),
        ).fetchall()
        return [(row["id"], Action.from_dict(json.loads(row["data_json"]))) for row in rows]

    def mark_action_attempted(self, action_id: int) -> None:
        self.connection.execute("UPDATE actions SET attempted = 1 WHERE id = ?", (action_id,))
        self.connection.commit()

    def reachable_routes(self, root_state: int) -> list[tuple[int, list[tuple[Action, int]]]]:
        """Return known reachable states in BFS order with shortest replay paths."""
        rows = self.connection.execute(
            "SELECT e.from_state, e.to_state, a.data_json FROM edges e "
            "JOIN actions a ON a.id = e.action_id ORDER BY e.id"
        ).fetchall()
        adjacency: dict[int, list[tuple[int, Action]]] = {}
        for row in rows:
            adjacency.setdefault(int(row["from_state"]), []).append(
                (int(row["to_state"]), Action.from_dict(json.loads(row["data_json"])))
            )

        routes: list[tuple[int, list[tuple[Action, int]]]] = [(root_state, [])]
        seen = {root_state}
        index = 0
        while index < len(routes):
            state_id, path = routes[index]
            index += 1
            for target_id, action in adjacency.get(state_id, []):
                if target_id in seen:
                    continue
                seen.add(target_id)
                routes.append((target_id, path + [(action, target_id)]))
        return routes

    def graph_data(self) -> dict[str, Any]:
        states = [dict(row) for row in self.connection.execute("SELECT * FROM states ORDER BY id")]
        edges = self.connection.execute(
            "SELECT e.from_state, e.to_state, a.data_json FROM edges e "
            "JOIN actions a ON a.id = e.action_id ORDER BY e.id"
        ).fetchall()
        variants_by_state: dict[int, list[dict[str, Any]]] = {}
        for row in self.connection.execute("SELECT * FROM variants ORDER BY id"):
            variants_by_state.setdefault(int(row["state_id"]), []).append(
                {
                    "screenshot_path": row["screenshot_path"],
                    "xml_path": row["xml_path"],
                    "phash": row["phash"],
                    "created_at": row["created_at"],
                }
            )
        for state in states:
            state["variants"] = variants_by_state.get(int(state["id"]), [])
        return {
            "states": states,
            "edges": [
                {
                    "from": row["from_state"],
                    "to": row["to_state"],
                    "action": json.loads(row["data_json"]),
                }
                for row in edges
            ],
        }

    def counts(self) -> tuple[int, int]:
        states = self.connection.execute("SELECT COUNT(*) FROM states").fetchone()[0]
        edges = self.connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return int(states), int(edges)
