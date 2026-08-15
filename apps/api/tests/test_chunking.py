from app.retrieval import MAX_HEADING_CHARS, chunk_text


def test_heading_glued_to_body_does_not_swallow_paragraph():
    # No blank line between the heading and the body that follows it is a
    # common real-world markdown shape. The heading must stay just the
    # heading line; the body must still become chunk content.
    text = "## 1. Overview\nThis is the body paragraph that follows immediately."
    chunks = chunk_text(text, title="Doc")

    assert len(chunks) == 1
    assert chunks[0]["heading"] == "1. Overview"
    assert chunks[0]["content"] == "This is the body paragraph that follows immediately."


def test_long_first_line_heading_is_truncated():
    # A pathologically long heading line must never exceed MAX_HEADING_CHARS,
    # since it is persisted into a bounded database column.
    long_line = "#" + " x" * 400  # far past MAX_HEADING_CHARS once stripped
    text = f"{long_line}\nbody"
    chunks = chunk_text(text, title="Doc")

    assert len(chunks[0]["heading"]) <= MAX_HEADING_CHARS


def test_heading_only_paragraph_still_produces_no_empty_chunk():
    text = "## Just a heading\n\nActual content."
    chunks = chunk_text(text, title="Doc")

    assert [c["content"] for c in chunks] == ["Actual content."]
    assert chunks[0]["heading"] == "Just a heading"
