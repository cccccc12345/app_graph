import json

from app_graph.database import GraphDatabase
from app_graph.export import export_graph
from app_graph.models import Action


def test_exports_json_dot_and_offline_html(tmp_path):
    db = GraphDatabase(tmp_path / "graph.db")
    try:
        first = db.add_state(0, "pkg/.Main", "states/a.png", "states/a.xml", 1)
        second = db.add_state(1, "pkg/.Next", "states/b.png", "states/b.xml", 2)
        db.add_edge(first.id, Action("click", x=10, y=20, text="Go"), second.id)
        paths = export_graph(db, tmp_path)
        assert set(paths) == {"json", "dot", "html"}
        payload = json.loads(paths["json"].read_text())
        assert payload["edges"][0]["from"] == 1
        assert "s1 -> s2" in paths["dot"].read_text()
        assert "Android App Graph" in paths["html"].read_text()
    finally:
        db.close()
