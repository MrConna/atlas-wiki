from app.retrieval import MAX_HEADING_CHARS, chunk_text


def test_heading_glued_to_body_keeps_heading_metadata_short():
    # No blank line between the heading and the body that follows it is a
    # common real-world markdown shape. The `heading` metadata must stay
    # just the heading line, even though the body still becomes part of
    # chunk content (as it does for a cleanly blank-line-separated heading).
    text = "## 1. Overview\nThis is the body paragraph that follows immediately."
    chunks = chunk_text(text, title="Doc")

    assert len(chunks) == 1
    assert chunks[0]["heading"] == "1. Overview"
    assert "This is the body paragraph that follows immediately." in chunks[0]["content"]


def test_long_first_line_heading_is_truncated():
    # A pathologically long heading line must never exceed MAX_HEADING_CHARS,
    # since it is persisted into a bounded database column.
    long_line = "#" + " x" * 400  # far past MAX_HEADING_CHARS once stripped
    text = f"{long_line}\nbody"
    chunks = chunk_text(text, title="Doc")

    assert len(chunks[0]["heading"]) <= MAX_HEADING_CHARS


def test_heading_glued_to_a_very_long_body_still_bounds_heading():
    # Regression: a heading glued to a long body paragraph previously made
    # `heading` (and downstream `source_location`) grow with the body,
    # overflowing their bounded database columns.
    long_body = "content " * 200
    text = f"## Section\n{long_body}"
    chunks = chunk_text(text, title="Doc")

    assert chunks[0]["heading"] == "Section"
    assert len(chunks[0]["heading"]) <= MAX_HEADING_CHARS
