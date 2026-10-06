import pytest
from minimize_fixture import MinimizeError, is_pure_deletion, main, minimize

# Deliberately odd formatting: single quotes, a bare attribute value, extra spaces, an entity,
# CRLF line endings. None of it may change.
PAGE = (
    "<!DOCTYPE html>\r\n"
    "<html><head><title>UFC 1</title>\r\n"
    "<link rel='stylesheet' href='/a.css'>\r\n"
    "<style>.x > .y { color: red }</style>\r\n"
    "<script>if (1 < 2 && '<div>') { document.write('</p>') }</script></head>\r\n"
    "<body><!-- tracking -->\r\n"
    "<nav class='top'><ul><li><nav>nested</nav></li></ul></nav>\r\n"
    "<section class='b-block'><h2  class='b-title'>Burns &amp; Malott</h2>\r\n"
    "<table><tr><td class=b-cell data-x='1'>  3:33  </td></tr></table></section>\r\n"
    "<div class='ad-slot'><ins class='adsbygoogle'></ins><iframe src='x'></iframe></div>\r\n"
    "<svg viewBox='0 0 1 1'><path d='M0 0'/><svg><g></g></svg></svg>\r\n"
    "<footer><script>track()</script><p>Terms</p></footer>\r\n"
    "</body></html>\r\n"
)

KEPT = (
    "<!DOCTYPE html>\r\n"
    "<html><head><title>UFC 1</title>\r\n"
    "\r\n"
    "\r\n"
    "</head>\r\n"
    "<body>\r\n"
    "\r\n"
    "<section class='b-block'><h2  class='b-title'>Burns &amp; Malott</h2>\r\n"
    "<table><tr><td class=b-cell data-x='1'>  3:33  </td></tr></table></section>\r\n"
    "<div class='ad-slot'></div>\r\n"
    "\r\n"
    "\r\n"
    "</body></html>\r\n"
)


def test_removes_the_noise_and_leaves_everything_else_exactly_as_saved():
    result = minimize(PAGE)

    assert result.html == KEPT
    assert result.removed == {
        "link": 1,
        "style": 1,
        "script": 1,  # the one inside <footer> goes with the footer
        "comment": 1,
        "nav": 1,  # the nested <nav> goes with the outer one
        "ins": 1,
        "iframe": 1,
        "svg": 1,
        "footer": 1,
    }


def test_the_output_is_only_ever_the_input_with_pieces_deleted():
    assert is_pure_deletion(PAGE, minimize(PAGE).html)
    assert not is_pure_deletion("<b>a</b>", "<b>a</B>")  # a rewritten character is caught
    assert not is_pure_deletion("<td class='x'>", '<td class="x">')  # so is changed quoting


def test_a_page_with_nothing_to_remove_comes_back_identical():
    page = "<html><body><h2 class='t'>Fight</h2><p>3:33</p></body></html>\n"

    assert minimize(page).html == page


def test_script_contents_that_look_like_html_do_not_confuse_it():
    page = "<p>a</p><script>var s = '</nav><footer>';</script><p>b</p>"

    assert minimize(page).html == "<p>a</p><p>b</p>"


def test_extra_tags_classes_and_ids_can_be_added():
    page = "<aside>x</aside><div class='side bar'>y</div><div id='promo'>z</div><p>keep</p>"

    result = minimize(
        page,
        remove_tags=frozenset({"aside"}),
        remove_classes=frozenset({"bar"}),
        remove_ids=frozenset({"promo"}),
    )

    assert result.html == "<p>keep</p>"


def test_nested_same_name_elements_are_cut_at_the_matching_close_tag():
    page = "<div class='cut'>a<div>b</div>c</div><div>keep</div>"

    assert minimize(page, remove_classes=frozenset({"cut"})).html == "<div>keep</div>"


def test_an_unclosed_element_to_remove_is_an_error_not_a_guess():
    with pytest.raises(MinimizeError, match="never closed"):
        minimize("<p>a</p><nav><ul><li>b")


def test_the_bot_challenge_page_is_refused():
    with pytest.raises(MinimizeError, match="bot-challenge"):
        minimize("<p>Checking your browser…</p><script>x</script>")


def test_main_writes_minimized_copies_and_never_touches_the_original(tmp_path, capsys):
    source = tmp_path / "full"
    source.mkdir()
    page = source / "fight.html"
    page.write_bytes(PAGE.encode())
    out = tmp_path / "small"

    assert main([str(page), "--out", str(out)]) == 0

    assert (out / "fight.html").read_bytes() == KEPT.encode()  # CRLF preserved
    assert page.read_bytes() == PAGE.encode()
    assert "fight.html:" in capsys.readouterr().out


def test_main_refuses_to_overwrite_its_input(tmp_path):
    page = tmp_path / "fight.html"
    page.write_text("<p>x</p><script>y</script>")

    assert main([str(page), "--out", str(tmp_path)]) == 1
    assert page.read_text() == "<p>x</p><script>y</script>"


def test_main_exits_nonzero_if_any_page_is_a_challenge_page(tmp_path):
    good = tmp_path / "a.html"
    good.write_text("<p>ok</p>")
    bad = tmp_path / "b.html"
    bad.write_text("<p>Checking your browser…</p>")

    assert main([str(good), str(bad), "--out", str(tmp_path / "out")]) == 1
    assert (tmp_path / "out" / "a.html").exists()  # the good one was still written
    assert not (tmp_path / "out" / "b.html").exists()
