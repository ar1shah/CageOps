from bs4 import BeautifulSoup
from minimize_wikipedia_fixture import minimize

SCRIPT = '<script>RLCONF={"wgArticleId":7,"wgRevisionId":9};</script>'
PAGE = f"""<html><head><title>UFC T - Wikipedia</title>
<link rel="canonical" href="https://en.wikipedia.org/wiki/UFC_T">{SCRIPT}<style>.x{{}}</style></head>
<body><nav>menu</nav><h1 id="firstHeading">UFC T</h1><p>Long prose we drop.</p>
<table class="infobox"><tr><th>Date</th><td>2026-01-01</td></tr></table>
<div class="mw-heading mw-heading2"><h2 id="Background">Background</h2></div><p>drop</p>
<div class="mw-heading mw-heading2"><h2 id="Results">Results</h2></div>
<table class="toccolours"><tr><td>A</td>
<td><sup><a href="#cite_note-1">[a]</a></sup></td></tr></table>
<div class="mw-heading mw-heading2"><h2 id="Bonus">Bonus</h2></div>
<table class="wikitable">drop</table>
<ol><li id="cite_note-1"><span class="reference-text">For the UFC Championship.</span></li>
<li id="cite_note-2">unreferenced</li></ol></body></html>"""


def test_it_keeps_only_the_head_infobox_results_and_the_footnotes_they_point_at():
    soup = BeautifulSoup(minimize(PAGE), "lxml")

    assert soup.title.string == "UFC T - Wikipedia"
    assert "wgArticleId" in soup.find("script").string  # the real config script, kept as is
    assert soup.find("link", rel="canonical")["href"].endswith("/wiki/UFC_T")
    assert soup.find(id="firstHeading") and soup.find(class_="infobox")
    assert soup.find(id="Results") and soup.find("table", class_="toccolours")
    assert [li["id"] for li in soup.select("ol.references li")] == ["cite_note-1"]
    text = soup.get_text(" ")
    for dropped in ("menu", "Long prose", "Background", "Bonus", "unreferenced"):
        assert dropped not in text
    assert soup.find("style") is None and soup.find("nav") is None
