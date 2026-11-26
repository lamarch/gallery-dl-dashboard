import io
import json
import os
import sys
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import fb_page_structure as fps  # noqa: E402

SECRETS = ["Vacances", "Bretagne", "Camille", "Dupont", "Новый", "ألبوم", "Léa",
           "123456789012345", "987654321098765", "1234567", "2019", "secret-token-xyz",
           "scontent", "oh=", "camille.dupont"]


def fake_page():
    data = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"result": {"data": {
        "user": {"__isProfile": "User", "name": "Camille Dupont", "id": "123456789012345",
                 "url": "https://www.facebook.com/camille.dupont"},
        "collection": {"pageItems": {"edges": [
            {"node": {"__typename": "TimelineAppCollectionAlbumItem",
                      "title": {"text": "Vacances Bretagne 2019"},
                      "url": "https://www.facebook.com/media/set/?set=a.1234567&type=3",
                      "media": {"count": 87},
                      "node": {"id": "987654321098765", "mediaSetToken": "a.1234567"},
                      "image": {"uri": "https://scontent.xx.fbcdn.net/v/x.jpg?oh=secret-token-xyz"}}},
            {"node": {"__typename": "TimelineAppCollectionAlbumItem",
                      "title": {"text": "Новый год"}, "media": {"count": 1234}}},
        ], "page_info": {"has_next_page": True, "end_cursor": "secret-token-xyz"}}},
        "987654321098765:0": {"x": 1}}}}}]]]}
    return f"""<html><head><title>Camille Dupont | Facebook</title>
<script type="application/json" data-sjs>{json.dumps(data)}</script></head>
<body><a href="https://www.facebook.com/media/set/?set=a.1234567&type=3">Vacances Bretagne 2019</a>
<span>87 éléments</span><span>Vacances Bretagne 2019</span><span>ألبوم العائلة · 12 photos</span>
<span>Mis à jour il y a 3 j</span><a href="/camille.dupont/photos_albums">Albums</a></body></html>"""


def test_no_personal_data_in_output(tmp_path):
    p = tmp_path / "page.html"
    p.write_text(fake_page(), encoding="utf-8")
    out = io.StringIO()
    with redirect_stdout(out):
        fps.main([str(p)])
    text = out.getvalue()
    for s in SECRETS:
        assert s not in text, s
    # ... but the structure we need is there
    assert "collection.pageItems.edges[].node.title.text : str" in text
    assert "collection.pageItems.edges[].node.media.count : int<100×1, int<10k×1" in text
    assert "N éléments" in text and "w … · N photos".replace(" …", "…") in text
    assert "/media/set/?set&type set=a.N" in text
    assert "TimelineAppCollectionAlbumItem×2" in text
    assert fps.mask_key("987654321098765:0") == "<id>" and fps.mask_key("title") == "title"
    assert fps.link_pattern("https://www.facebook.com/some.user/photos_albums") == "/<seg>/photos_albums"
