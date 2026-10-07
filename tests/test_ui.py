from app_graph.models import Action, Bounds
from app_graph.ui import extract_actions, hierarchy_signature, parse_bounds, structure_similarity


def test_parse_bounds():
    assert parse_bounds("[10,20][110,220]") == Bounds(10, 20, 110, 220)
    assert parse_bounds("[1,1][1,3]") is None
    assert parse_bounds("nope") is None


def test_extracts_visible_clicks_and_safe_gestures():
    xml = '''<hierarchy><node text="" clickable="false" bounds="[0,0][50,50]" />
    <node text="Settings" content-desc="" resource-id="com.example:id/settings" clickable="true" enabled="true" visible-to-user="true" bounds="[10,20][110,120]" />
    <node text="Delete account" clickable="true" enabled="true" bounds="[0,150][200,200]" />
    <node text="Offscreen" clickable="true" bounds="[900,900][1000,1000]" />
    <node text="Disabled" clickable="true" enabled="false" bounds="[0,0][20,20]" />
    </hierarchy>'''
    actions = extract_actions(xml, 400, 800)
    assert [a.kind for a in actions] == ["click", "swipe_up", "swipe_down", "back"]
    assert actions[0].text == "Settings"
    assert actions[0].bounds == Bounds(10, 20, 110, 120)
    assert actions[-1] == Action("back")


def test_safety_filter_uses_word_boundaries():
    xml = '''<hierarchy>
    <node text="Delete" clickable="true" bounds="[0,0][20,20]" />
    <node text="Update profile" clickable="true" bounds="[30,0][60,20]" />
    <node text="购买" clickable="true" bounds="[70,0][100,20]" />
    </hierarchy>'''
    actions = extract_actions(xml, 200, 400)
    assert [action.text for action in actions[:1]] == ["Update profile"]


def test_safety_filter_catches_resource_id_underscores():
    xml = '''<hierarchy>
    <node text="" resource-id="app:id/delete_account" clickable="true" bounds="[0,0][30,30]" />
    <node text="" resource-id="app:id/profile" clickable="true" bounds="[40,0][70,30]" />
    </hierarchy>'''
    actions = extract_actions(xml, 100, 200)
    assert len([action for action in actions if action.kind == "click"]) == 1
    assert actions[0].resource_id == "app:id/profile"


def test_navigation_only_keeps_navigation_and_dismiss_but_skips_content():
    xml = '''<hierarchy>
    <node text="" content-desc="我的" resource-id="app:id/profile" clickable="true" bounds="[10,700][90,780]" />
    <node text="" content-desc="关闭" resource-id="app:id/close" clickable="true" bounds="[300,100][380,180]" />
    <node text="免费听歌" resource-id="app:id/playlist_card" clickable="true" bounds="[10,900][390,1100]" />
    <node text="" content-desc="播放" resource-id="app:id/play" clickable="true" bounds="[300,700][380,780]" />
    </hierarchy>'''
    actions = extract_actions(xml, 400, 1200, navigation_only=True)
    clicks = [action for action in actions if action.kind == "click"]
    assert [action.content_desc for action in clicks] == ["我的", "关闭"]


def test_hierarchy_signature_ignores_volatile_labels_and_bounds():
    first = '''<hierarchy>
    <node class="FrameLayout" resource-id="app:id/root">
      <node class="Button" resource-id="app:id/play" text="播放" bounds="[0,0][40,40]" />
      <node class="TextView" resource-id="app:id/hint" text="轮换文案" bounds="[40,0][80,40]" />
    </node>
    </hierarchy>'''
    second = '''<hierarchy>
    <node class="FrameLayout" resource-id="app:id/root">
      <node class="Button" resource-id="app:id/play" text="暂停" bounds="[0,0][50,50]" />
      <node class="TextView" resource-id="app:id/hint" text="全新文案" bounds="[50,0][90,50]" />
    </node>
    </hierarchy>'''
    assert structure_similarity(hierarchy_signature(first), hierarchy_signature(second)) == 1.0


def test_hierarchy_signature_uses_labels_for_nodes_without_ids():
    first = '''<hierarchy>
    <node class="Button" text="关闭" clickable="true" bounds="[0,0][10,10]" />
    </hierarchy>'''
    second = '''<hierarchy>
    <node class="Button" text="首页" clickable="true" bounds="[0,0][10,10]" />
    </hierarchy>'''
    assert structure_similarity(hierarchy_signature(first), hierarchy_signature(second)) == 0.0
    assert structure_similarity(hierarchy_signature(first), hierarchy_signature(first)) == 1.0


def test_hierarchy_signature_separates_different_layouts():
    base = '''<hierarchy>
    <node class="FrameLayout" resource-id="app:id/root">
      <node class="Button" resource-id="app:id/play" />
    </node>
    </hierarchy>'''
    deeper = '''<hierarchy>
    <node class="FrameLayout" resource-id="app:id/root">
      <node class="LinearLayout" resource-id="app:id/panel">
        <node class="Button" resource-id="app:id/play" />
      </node>
    </node>
    </hierarchy>'''
    other = '''<hierarchy>
    <node class="FrameLayout" resource-id="app:id/root">
      <node class="ImageView" />
      <node class="ImageView" />
    </node>
    </hierarchy>'''
    assert structure_similarity(hierarchy_signature(base), hierarchy_signature(deeper)) < 0.9
    assert structure_similarity(hierarchy_signature(base), hierarchy_signature(other)) < 0.9
    assert len(hierarchy_signature("<hierarchy />")) == 0
