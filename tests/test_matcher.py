from PIL import Image, ImageDraw

from app_graph.matcher import compare_screenshots


def test_identical_screenshots_match(tmp_path):
    image = Image.new("RGB", (240, 480), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 60, 220, 120), fill="#2563eb")
    draw.text((30, 75), "Settings", fill="white")
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    image.save(first)
    image.save(second)
    score = compare_screenshots(first, second)
    assert score.phash_distance == 0
    assert score.ssim > 0.99


def test_different_pages_have_lower_similarity(tmp_path):
    first_image = Image.new("RGB", (240, 480), "white")
    ImageDraw.Draw(first_image).rectangle((10, 60, 230, 100), fill="blue")
    second_image = Image.new("RGB", (240, 480), "black")
    ImageDraw.Draw(second_image).ellipse((20, 100, 220, 300), fill="yellow")
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    first_image.save(first)
    second_image.save(second)
    score = compare_screenshots(first, second)
    assert score.ssim < 0.90
