from pathlib import Path

from app_graph.webapp import ExplorerService


def test_asset_route_rejects_traversal_and_non_png(tmp_path):
    service = ExplorerService()
    service.output_dir = tmp_path
    states = tmp_path / "states"
    states.mkdir()
    image = states / "page.png"
    image.write_bytes(b"png")
    assert service.screenshot("states/page.png") == image
    assert service.screenshot("../outside.png") is None
    assert service.screenshot("states/page.xml") is None
