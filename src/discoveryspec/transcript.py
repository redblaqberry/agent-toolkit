"""Parser for discovery transcripts with numbered speaker turns.

Format: every extractable statement is one turn starting at column 0:

    [T07] Anna Lindqvist (Operations): Eighty percent of invoices ...

Everything after a turn header up to the next header belongs to that turn,
including blank-line-separated paragraphs; textual content is preserved with
whitespace normalized to single spaces (paragraph boundaries are not kept).
Any line that begins with ``[T`` (indented or not) must be a fully valid,
canonically numbered turn header; anything else is a hard error rather than
prose, so a malformed turn can never be silently attributed to the previous
speaker. Turn numbers must be sequential from T01 with canonical zero
padding; gaps, duplicates, and aliases like ``[T001]`` are parse errors.

The transcript's SHA-256 is captured so contracts can pin the exact bytes
their provenance was extracted from (``metadata.transcript_sha256``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

TURN_RE = re.compile(r"^\[T(\d{2,})\]\s+(.+?)\s+\(([^)]+)\):\s*(.*)$")


class TranscriptError(ValueError):
    """The transcript violates the numbered-turn format."""


@dataclass(frozen=True)
class Turn:
    number: int
    speaker: str
    role: str
    text: str


@dataclass(frozen=True)
class Transcript:
    path: str
    turns: list[Turn]
    sha256: str

    def turn_numbers(self) -> set[int]:
        return {t.number for t in self.turns}

    def turn(self, number: int) -> Turn:
        for t in self.turns:
            if t.number == number:
                return t
        raise KeyError(f"no turn T{number:02d} in {self.path}")


def parse_transcript(path: str | Path) -> Transcript:
    path = Path(path)
    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    lines = data.decode("utf-8").splitlines()

    turns: list[Turn] = []
    current: dict | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            turns.append(
                Turn(
                    number=current["number"],
                    speaker=current["speaker"],
                    role=current["role"],
                    text=" ".join(part for part in current["text"] if part).strip(),
                )
            )
            current = None

    for line_number, line in enumerate(lines, start=1):
        match = TURN_RE.match(line)
        if match:
            digits = match.group(1)
            number = int(digits)
            if digits != f"{number:02d}":
                raise TranscriptError(
                    f"{path}: line {line_number}: noncanonical turn id [T{digits}]; "
                    f"expected [T{number:02d}]"
                )
            flush()
            current = {
                "number": number,
                "speaker": match.group(2).strip(),
                "role": match.group(3).strip(),
                "text": [match.group(4).strip()],
            }
        elif line.lstrip().startswith("[T"):
            raise TranscriptError(
                f"{path}: line {line_number} looks like a turn header but does not "
                f"parse (expected '[Tnn] Speaker (Role): text' at column 0): "
                f"{line.strip()!r}"
            )
        elif current is not None:
            stripped = line.strip()
            if stripped:
                current["text"].append(stripped)
            # blank lines separate paragraphs inside the same turn; the turn
            # ends only at the next turn header or end of file
        # lines before the first turn (header, participants) are ignored
    flush()

    if not turns:
        raise TranscriptError(f"{path}: no numbered turns found")

    numbers = [t.number for t in turns]
    expected = list(range(1, len(turns) + 1))
    if numbers != expected:
        raise TranscriptError(
            f"{path}: turn numbers must be sequential starting at T01; "
            f"got {numbers[:5]}... expected {expected[:5]}..."
        )
    empty = [t.number for t in turns if not t.text]
    if empty:
        raise TranscriptError(f"{path}: turns with empty text: {empty}")

    return Transcript(path=str(path), turns=turns, sha256=sha256)
