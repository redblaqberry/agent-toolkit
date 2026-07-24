"""Brand configuration for manager-facing reports: data in, never CSS in.

A brand file supplies identity, a palette, font stacks, and POLICY, the
customer's hard style rules (no shadows, no gradients, corner radius caps,
no emoji). The renderer treats every policy flag as a constraint that cannot
be silently violated: all styling in the emitted report flows through a
typed style serializer that rejects forbidden declarations before anything
is written, and a report whose brand palette fails WCAG AA contrast is
refused with every failing pair listed, never auto-adjusted.

The schema is strict (unknown keys are errors) and colors and font family
names are grammar-checked, so a brand file cannot smuggle CSS or markup into
the document.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Color = Annotated[str, Field(pattern=r"^#[0-9a-f]{6}$")]
FontFamily = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 \-]*$")]

MIN_CONTRAST = 4.5  # WCAG AA for normal text, applied to every pairing


class BrandError(ValueError):
    """The brand file is invalid or violates a hard constraint; exit 2."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.exit_code = 2
        self.problems = problems or []


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrandIdentity(_Strict):
    company: str = Field(min_length=1)
    product_line: Optional[str] = None


class BrandPalette(_Strict):
    paper: Color
    ink: Color
    muted_ink: Color
    accent: Color
    pass_background: Color
    pass_ink: Color
    fail_background: Color
    fail_ink: Color


class BrandTypography(_Strict):
    display_stack: list[FontFamily] = Field(min_length=1)
    body_stack: list[FontFamily] = Field(min_length=1)
    mono_stack: list[FontFamily] = Field(min_length=1)


class BrandPolicy(_Strict):
    max_corner_radius_px: int = Field(ge=0, le=32)
    allow_shadows: bool
    allow_gradients: bool
    allow_emoji: bool


class Brand(_Strict):
    schema_version: Literal["brand.v1"]
    identity: BrandIdentity
    palette: BrandPalette
    typography: BrandTypography
    policy: BrandPolicy


DEFAULT_BRAND = Brand(
    schema_version="brand.v1",
    identity=BrandIdentity(company="DiscoverySpec"),
    palette=BrandPalette(
        paper="#f6f7f5", ink="#1e2833", muted_ink="#57646f", accent="#33618f",
        pass_background="#e7f2ec", pass_ink="#1f5942",
        fail_background="#f8ebe9", fail_ink="#9c2f28",
    ),
    typography=BrandTypography(
        display_stack=["Iowan Old Style", "Palatino Linotype", "Georgia", "serif"],
        body_stack=["Segoe UI", "system-ui", "sans-serif"],
        mono_stack=["Consolas", "Menlo", "monospace"],
    ),
    policy=BrandPolicy(
        max_corner_radius_px=2, allow_shadows=False,
        allow_gradients=False, allow_emoji=False,
    ),
)


def _channel(value: int) -> float:
    scaled = value / 255
    return scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (relative_luminance(foreground), relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def contrast_problems(palette: BrandPalette) -> list[str]:
    """Every foreground/background pairing the report actually renders."""
    pairs = [
        ("ink on paper", palette.ink, palette.paper),
        ("muted_ink on paper", palette.muted_ink, palette.paper),
        ("accent on paper", palette.accent, palette.paper),
        ("pass_ink on pass_background", palette.pass_ink, palette.pass_background),
        ("fail_ink on fail_background", palette.fail_ink, palette.fail_background),
        ("fail_ink on paper", palette.fail_ink, palette.paper),
    ]
    problems = []
    for name, foreground, background in pairs:
        ratio = contrast_ratio(foreground, background)
        if ratio < MIN_CONTRAST:
            problems.append(
                f"{name}: contrast {ratio:.2f}:1, minimum {MIN_CONTRAST}:1"
            )
    return problems


def validate_brand(brand: Brand) -> None:
    problems = contrast_problems(brand.palette)
    if problems:
        raise BrandError(
            "brand palette fails WCAG AA contrast; a customer-facing report "
            "with unreadable text is a defect, and colors are never adjusted "
            "silently",
            problems=problems,
        )


def load_brand(path: str | Path) -> Brand:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BrandError(f"cannot read brand file {path}: {exc}") from exc
    try:
        brand = Brand.model_validate(raw)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        ]
        raise BrandError(f"brand file {path} is invalid", problems=problems) from exc
    validate_brand(brand)
    return brand
