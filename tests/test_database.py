import json

from app_graph.database import GraphDatabase
from app_graph.models import Action, Bounds


def test_persists_states_variants_actions_and_edges(tmp_path):
    db = GraphDatabase(tmp_path / "graph.db")
    try:
        first = db.add_state(0, "pkg/.Main", "states/a.png", "states/a.xml", 42)
        second = db.add_state(1, "pkg/.Next", "states/b.png", "states/b.xml", 43)
        action = Action("click", x=10, y=20, text="Next", bounds=Bounds(0, 0, 20, 40))
        action_id = db.add_action(first.id, action)
        pending = db.pending_actions(first.id)
        assert pending == [(action_id, action)]
        db.add_edge(first.id, action, second.id)
        db.add_variant(first.id, "states/a2.png", "states/a2.xml", 44)
        assert db.get_state(first.id).visit_count == 2
        assert db.variants(first.id)[0].phash == "000000000000002c"
        assert db.pending_actions(first.id) == []
        graph = db.graph_data()
        assert len(graph["edges"]) == 1
        assert graph["edges"][0]["action"]["bounds"] == {"left": 0, "top": 0, "right": 20, "bottom": 40}
        assert db.counts() == (2, 1)
        assert [state_id for state_id, _ in db.reachable_routes(first.id)] == [first.id, second.id]
        assert len(db.graph_data()["states"][0]["variants"]) == 1
    finally:
        db.close()


def test_database_is_bound_to_one_package(tmp_path):
    db = GraphDatabase(tmp_path / "graph.db")
    try:
        db.bind_package("com.example")
        db.bind_package("com.example")
        try:
            db.bind_package("com.other")
        except ValueError as exc:
            assert "another --output" in str(exc)
        else:
            raise AssertionError("Expected package mismatch to fail")
    finally:
        db.close()


def test_depth_is_monotonic_toward_shorter_routes(tmp_path):
    db = GraphDatabase(tmp_path / "graph.db")
    try:
        state = db.add_state(4, "pkg/.Page", "a.png", "a.xml", 42)
        db.update_min_depth(state.id, 2)
        db.update_min_depth(state.id, 3)
        assert db.get_state(state.id).depth == 2
    finally:
        db.close()
