"""Brand kits / templates for ClipForge (Phase 2).

A brand kit styles a clip's captions and SEO copy — colours, font,
caption style, logo path, intro/outro text — without changing the clip
itself. Kits are stored in :class:`clipforge.db.BrandKit`; this module
holds the pure logic: validation, the two built-in templates, and
applying a kit to an SEO variant (or a caption style name) at
render/SEO time.

Built-in templates (no DB row needed to use them — the API exposes
them at ``GET /brand-kits/templates`` and ``POST /brand-kits`` accepts
``template`` to clone one into a stored kit):

* ``bold-pop`` — high-contrast Hormozi-style: white on black, gold
  accent, bold-pop captions.
* ``clean-minimal`` — muted, editorial: dark slate on off-white, teal
  accent, boxed captions.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

HEX_COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Caption styles available in clipforge.captions.STYLES.
CAPTION_STYLES = ("bold-pop", "karaoke-fill", "boxed", "default")


@dataclass
class BrandKitSpec:
    """Validated, transport-friendly view of a brand kit."""

    name: str
    primary_colour: str = "#FFFFFF"
    secondary_colour: str = "#000000"
    accent_colour: str = "#FFD700"
    font_name: str = "DejaVu Sans"
    caption_style: str = "bold-pop"
    logo_path: str | None = None
    intro_text: str = ""
    outro_text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


BUILTIN_TEMPLATES: dict[str, BrandKitSpec] = {
    "bold-pop": BrandKitSpec(
        name="Bold Pop",
        primary_colour="#FFFFFF",
        secondary_colour="#000000",
        accent_colour="#FFD700",
        font_name="DejaVu Sans",
        caption_style="bold-pop",
        intro_text="",
        outro_text="Follow for more.",
    ),
    "clean-minimal": BrandKitSpec(
        name="Clean Minimal",
        primary_colour="#1F2937",
        secondary_colour="#F9FAFB",
        accent_colour="#0D9488",
        font_name="DejaVu Sans",
        caption_style="boxed",
        intro_text="",
        outro_text="More in the next one.",
    ),
}


class BrandKitError(ValueError):
    """Raised when a brand-kit payload fails validation."""


def validate_kit(spec: BrandKitSpec) -> BrandKitSpec:
    """Validate a kit spec; raises :class:`BrandKitError` on bad input."""
    if not spec.name.strip():
        raise BrandKitError("brand kit name is required")
    for field_name in ("primary_colour", "secondary_colour", "accent_colour"):
        value = getattr(spec, field_name)
        if not HEX_COLOUR_RE.match(value or ""):
            raise BrandKitError(f"{field_name} must be a #RRGGBB hex colour, got {value!r}")
    if spec.caption_style not in CAPTION_STYLES:
        raise BrandKitError(
            f"caption_style must be one of {CAPTION_STYLES}, got {spec.caption_style!r}"
        )
    if spec.logo_path and spec.logo_path.strip().lower().startswith(("http://", "https://")):
        raise BrandKitError("logo_path must be a local file path, not a URL")
    return spec


def builtin_template(name: str) -> BrandKitSpec:
    """Return a copy of a built-in template, or raise BrandKitError."""
    template = BUILTIN_TEMPLATES.get(name)
    if template is None:
        raise BrandKitError(
            f"unknown built-in template {name!r}; available: {sorted(BUILTIN_TEMPLATES)}"
        )
    return BrandKitSpec(**template.to_dict())


def list_builtin_templates() -> list[dict]:
    """All built-in templates as dicts (each includes its template key)."""
    return [{"template": key, **spec.to_dict()} for key, spec in BUILTIN_TEMPLATES.items()]


def apply_kit_to_seo(seo: dict, kit: BrandKitSpec) -> dict:
    """Apply a kit to one platform SEO variant; returns a new dict.

    * ``intro_text`` is prepended to the description/caption (if set).
    * ``outro_text`` is appended (if set).
    * ``title`` is left untouched — kits style copy, they never rewrite
      the title (that is the A/B test's job).
    * Kit metadata is recorded under ``brand_kit`` so the render step
      and the review UI can show what was applied.
    """
    out = dict(seo)
    for key in ("description", "caption"):
        text = (out.get(key) or "").strip()
        parts = [p for p in (kit.intro_text.strip(), text, kit.outro_text.strip()) if p]
        if parts:
            out[key] = "\n\n".join(parts) if key == "description" else "\n".join(parts)
    out["brand_kit"] = {
        "name": kit.name,
        "primary_colour": kit.primary_colour,
        "secondary_colour": kit.secondary_colour,
        "accent_colour": kit.accent_colour,
        "font_name": kit.font_name,
        "caption_style": kit.caption_style,
        "logo_path": kit.logo_path,
    }
    return out


def caption_style_for_kit(kit: BrandKitSpec | None) -> str:
    """Caption style name to pass to the renderer for a kit (or default)."""
    if kit is None or kit.caption_style == "default":
        return "bold-pop"
    return kit.caption_style


__all__ = [
    "BrandKitSpec",
    "BrandKitError",
    "BUILTIN_TEMPLATES",
    "CAPTION_STYLES",
    "validate_kit",
    "builtin_template",
    "list_builtin_templates",
    "apply_kit_to_seo",
    "caption_style_for_kit",
]
