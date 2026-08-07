from bot import TELEGRAM_MESSAGE_LIMIT, split_for_telegram


def test_split_for_telegram_short_text_unchanged():
    text = "Short reply."
    assert split_for_telegram(text) == [text]


def test_split_for_telegram_splits_long_text():
    text = "a" * (TELEGRAM_MESSAGE_LIMIT + 500)
    chunks = split_for_telegram(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= TELEGRAM_MESSAGE_LIMIT
    assert "".join(chunks) == text


def test_split_for_telegram_prefers_newline_boundary():
    # A newline just past the halfway point of the limit — the split should
    # land there rather than mid-line further into the first chunk.
    first_line = "x" * (TELEGRAM_MESSAGE_LIMIT - 10)
    second_line = "y" * 100
    text = f"{first_line}\n{second_line}"
    chunks = split_for_telegram(text)
    assert chunks[0] == first_line
    assert chunks[1] == second_line
