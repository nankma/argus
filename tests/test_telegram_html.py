import telegram_html


def test_is_html_balanced():
    assert telegram_html.is_html_balanced("plain text") is True
    assert telegram_html.is_html_balanced("<b>bold</b>") is True
    assert telegram_html.is_html_balanced("<b>bold and <i>italic</i></b>") is True
    assert telegram_html.is_html_balanced("<b>unclosed") is False
    assert telegram_html.is_html_balanced("closed</b> with no opener") is False


def test_strip_html_tags():
    assert telegram_html.strip_html_tags('<b>bold</b> and <a href="x">link</a>') == "bold and link"
    assert telegram_html.strip_html_tags("plain text") == "plain text"


# --- validate() ------------------------------------------------------------


def test_validate_accepts_plain_text():
    assert telegram_html.validate("just some text, no tags at all") is None


def test_validate_accepts_the_allowed_tags():
    assert telegram_html.validate("<b>bold</b> and <i>italic</i>") is None


def test_validate_accepts_a_real_link():
    assert telegram_html.validate('🔗 <a href="https://example.com/article">Ars Technica</a>') is None


def test_validate_accepts_an_escaped_ampersand():
    # The exact shape of the incident this module exists to catch --
    # "AT&T" written correctly, as "AT&amp;T".
    assert telegram_html.validate("AT&amp;T unveils new chip") is None


def test_validate_rejects_a_disallowed_tag():
    reason = telegram_html.validate("<span>not asked for</span>")
    assert reason is not None
    assert "span" in reason


def test_validate_rejects_crossed_nesting():
    reason = telegram_html.validate("<b>bold <i>and italic</b></i>")
    assert reason is not None
    assert "nesting" in reason


def test_validate_rejects_an_unclosed_tag():
    reason = telegram_html.validate("<b>never closed")
    assert reason is not None
    assert "unclosed" in reason


def test_validate_rejects_an_unmatched_closing_tag():
    reason = telegram_html.validate("closed</b> with no opener")
    assert reason is not None
    assert "nesting" in reason


def test_validate_rejects_a_bare_link_with_no_href():
    reason = telegram_html.validate("<a>text with no href</a>")
    assert reason is not None
    assert "href" in reason


def test_validate_rejects_an_unescaped_ampersand():
    # The real incident this whole feature was built for: an article
    # title like "AT&T unveils..." with the & left unescaped.
    reason = telegram_html.validate("AT&T unveils new chip")
    assert reason is not None
    assert "&" in reason


def test_validate_rejects_an_unescaped_angle_bracket():
    reason = telegram_html.validate("revenue grew 5 < 10 percent")
    assert reason is not None
    assert "<" in reason


def test_validate_stops_at_the_first_problem():
    # Two independent problems (a disallowed tag AND an unescaped &) --
    # the allowed-tags check runs first, so that's the reason reported,
    # not the ampersand.
    reason = telegram_html.validate("<span>AT&T</span>")
    assert reason is not None
    assert "span" in reason
