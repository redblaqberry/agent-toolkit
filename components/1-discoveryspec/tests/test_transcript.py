import pytest

from discoveryspec import TranscriptError, parse_transcript
from tests.conftest import TRANSCRIPT_PATH


def test_example_transcript_parses(transcript):
    assert len(transcript.turns) == 41
    assert transcript.turns[0].number == 1
    assert transcript.turns[0].speaker == "Sam Rivera"
    assert transcript.turns[0].role == "FDE"
    assert transcript.turn(15).speaker == "Jonas Weber"
    assert "audit finding" in transcript.turn(15).text


def test_turn_numbers_are_sequential(transcript):
    assert [t.number for t in transcript.turns] == list(range(1, 42))


def test_multiline_turns_are_joined(tmp_path):
    path = tmp_path / "t.md"
    path.write_text(
        "[T01] A (Ops): first line\n"
        "continues here\n"
        "\n"
        "[T02] B (Sec): second turn\n",
        encoding="utf-8",
    )
    transcript = parse_transcript(path)
    assert transcript.turn(1).text == "first line continues here"
    assert transcript.turn(2).text == "second turn"


def test_multi_paragraph_content_is_kept_whitespace_normalized(tmp_path):
    # a blank line inside a turn separates paragraphs; the text after it still
    # belongs to the same turn. Textual content is preserved; paragraph
    # boundaries are normalized to single spaces (documented lossy formatting).
    path = tmp_path / "t.md"
    path.write_text(
        "[T01] A (Ops): first paragraph\n"
        "\n"
        "second paragraph after a blank line\n"
        "\n"
        "third paragraph\n"
        "[T02] B (Sec): next turn\n",
        encoding="utf-8",
    )
    transcript = parse_transcript(path)
    assert transcript.turn(1).text == (
        "first paragraph second paragraph after a blank line third paragraph"
    )
    assert transcript.turn(2).text == "next turn"


def test_malformed_turn_header_is_an_error_not_prose(tmp_path):
    # missing the (Role) part: must raise, not silently merge into T01
    path = tmp_path / "t.md"
    path.write_text(
        "[T01] A (Ops): one\n"
        "[T02] B: two without a role\n",
        encoding="utf-8",
    )
    with pytest.raises(TranscriptError, match="looks like a turn header"):
        parse_transcript(path)


def test_indented_turn_header_is_an_error(tmp_path):
    path = tmp_path / "t.md"
    path.write_text(
        "[T01] A (Ops): one\n"
        "  [T02] B (Sec): indented header must not become prose\n",
        encoding="utf-8",
    )
    with pytest.raises(TranscriptError, match="looks like a turn header"):
        parse_transcript(path)


def test_non_numeric_turn_id_is_an_error(tmp_path):
    path = tmp_path / "t.md"
    path.write_text(
        "[T01] A (Ops): one\n"
        "[TXX] B (Sec): letters instead of digits\n",
        encoding="utf-8",
    )
    with pytest.raises(TranscriptError, match="looks like a turn header"):
        parse_transcript(path)


def test_noncanonical_padded_turn_id_is_an_error(tmp_path):
    # [T001] would alias integer turn 1; only canonical two-digit padding parses
    path = tmp_path / "t.md"
    path.write_text("[T001] A (Ops): aliased id\n", encoding="utf-8")
    with pytest.raises(TranscriptError, match="noncanonical turn id"):
        parse_transcript(path)


def test_gap_in_numbering_is_rejected(tmp_path):
    path = tmp_path / "t.md"
    path.write_text("[T01] A (Ops): one\n[T03] B (Sec): three\n", encoding="utf-8")
    with pytest.raises(TranscriptError):
        parse_transcript(path)


def test_duplicate_turn_is_rejected(tmp_path):
    path = tmp_path / "t.md"
    path.write_text("[T01] A (Ops): one\n[T01] B (Sec): again\n", encoding="utf-8")
    with pytest.raises(TranscriptError):
        parse_transcript(path)


def test_empty_transcript_is_rejected(tmp_path):
    path = tmp_path / "t.md"
    path.write_text("# just a header, no turns\n", encoding="utf-8")
    with pytest.raises(TranscriptError):
        parse_transcript(path)


def test_header_lines_are_not_turns():
    transcript = parse_transcript(TRANSCRIPT_PATH)
    assert all(t.text for t in transcript.turns)
    # header material (participant list, format note) must not leak into turns
    combined = " ".join(t.text for t in transcript.turns)
    assert "Participants:" not in combined
    assert "Customer: Nordlicht GmbH" not in combined
    assert transcript.turn(1).text.startswith("Thanks everyone.")
