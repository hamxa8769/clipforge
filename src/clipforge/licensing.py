"""Source-licence record for the lawful CC / public-domain pipeline (v0.7.0).

Shared field names (Worker A licence detection + Worker B thumbnail /
attribution + web UI all use exactly these — do not rename without
updating every consumer):

  ``source_license``   canonical licence key, see :data:`CC_LICENSES` /
                       :data:`STANDARD_LICENSE` (``None`` = unknown / not
                       recorded yet — local own-content jobs)
  ``source_url``       URL of the original video (YouTube watch URL)
  ``source_title``     title of the original video
  ``source_channel``   creator / channel name of the original video
  ``attribution_text`` the credit block appended to every generated
                       description (built by :func:`build_attribution_text`)

Rules:
- Only Creative Commons, CC0, public-domain and own content may be
  clipped and posted. A third-party video under the YouTube Standard
  licence must NEVER be published — :func:`publish_blocked_reason`
  is the defence-in-depth check the publish path calls even if the
  agent / job-creation checks were bypassed.
- Attribution is built into SEO generation (see :mod:`clipforge.seo`),
  never pasted by hand, and :func:`append_attribution` is idempotent:
  re-running SEO keeps exactly one credit block, never two.
"""

from __future__ import annotations

import re

# Canonical licence keys. ``own`` = the user's own video; ``standard`` =
# the YouTube Standard licence (third-party, NOT publishable).
STANDARD_LICENSE = "standard"
OWN_LICENSE = "own"
PUBLIC_DOMAIN_LICENSE = "public-domain"

# canonical key -> (display name, licence URL). ``url`` is "" for own /
# public-domain content (no licence deed to link to).
CC_LICENSES: dict[str, tuple[str, str]] = {
    "cc-by-4.0": (
        "Creative Commons Attribution 4.0 International",
        "https://creativecommons.org/licenses/by/4.0/",
    ),
    "cc-by-3.0": (
        "Creative Commons Attribution 3.0 Unported",
        "https://creativecommons.org/licenses/by/3.0/",
    ),
    "cc-by-sa-4.0": (
        "Creative Commons Attribution-ShareAlike 4.0 International",
        "https://creativecommons.org/licenses/by-sa/4.0/",
    ),
    "cc-by-sa-3.0": (
        "Creative Commons Attribution-ShareAlike 3.0 Unported",
        "https://creativecommons.org/licenses/by-sa/3.0/",
    ),
    "cc-by-nc-4.0": (
        "Creative Commons Attribution-NonCommercial 4.0 International",
        "https://creativecommons.org/licenses/by-nc/4.0/",
    ),
    "cc-by-nc-sa-4.0": (
        "Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International",
        "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    ),
    "cc-by-nd-4.0": (
        "Creative Commons Attribution-NoDerivatives 4.0 International",
        "https://creativecommons.org/licenses/by-nd/4.0/",
    ),
    "cc0": ("CC0 1.0 Universal (Public Domain Dedication)", "https://creativecommons.org/publicdomain/zero/1.0/"),
    "public-domain": ("Public Domain", ""),
    "own": ("Own content", ""),
}

# Every licence key the pipeline recognises (CC keys + own + standard).
KNOWN_LICENSES = frozenset(CC_LICENSES) | {STANDARD_LICENSE}

#: The five field names every clip / job / post record carries.
SOURCE_LICENSE_FIELDS = (
    "source_license",
    "source_url",
    "source_title",
    "source_channel",
    "attribution_text",
)

# Free-text spellings users / the YouTube API may send, mapped to canonical.
_ALIASES = {
    "creative commons attribution 4.0": "cc-by-4.0",
    "creative commons attribution 4.0 international": "cc-by-4.0",
    "cc by 4.0": "cc-by-4.0",
    "cc-by": "cc-by-4.0",
    "creative commons attribution 3.0": "cc-by-3.0",
    "creative commons attribution 3.0 unported": "cc-by-3.0",
    "cc by 3.0": "cc-by-3.0",
    "creative commons attribution-sharealike 4.0": "cc-by-sa-4.0",
    "creative commons attribution-sharealike 4.0 international": "cc-by-sa-4.0",
    "cc by-sa 4.0": "cc-by-sa-4.0",
    "creative commons attribution-sharealike 3.0": "cc-by-sa-3.0",
    "creative commons attribution-sharealike 3.0 unported": "cc-by-sa-3.0",
    "cc by-sa 3.0": "cc-by-sa-3.0",
    "creative commons attribution-noncommercial 4.0": "cc-by-nc-4.0",
    "creative commons attribution-noncommercial 4.0 international": "cc-by-nc-4.0",
    "cc by-nc 4.0": "cc-by-nc-4.0",
    "creative commons attribution-noncommercial-sharealike 4.0": "cc-by-nc-sa-4.0",
    "creative commons attribution-noncommercial-sharealike 4.0 international": "cc-by-nc-sa-4.0",
    "cc by-nc-sa 4.0": "cc-by-nc-sa-4.0",
    "creative commons attribution-noderivatives 4.0": "cc-by-nd-4.0",
    "creative commons attribution-noderivatives 4.0 international": "cc-by-nd-4.0",
    "cc by-nd 4.0": "cc-by-nd-4.0",
    "cc0": "cc0",
    "cc0 1.0": "cc0",
    "creative commons cc0": "cc0",
    "public domain": "public-domain",
    "public-domain": "public-domain",
    "own": "own",
    "own content": "own",
    "standard": "standard",
    "standard youtube license": "standard",
    "youtube standard license": "standard",
    "youtube standard": "standard",
    "youtube": "standard",
}


def normalize_license(raw: str | None) -> str | None:
    """Map free text to a canonical licence key, or ``None`` when blank.

    Unknown text is returned lower-cased and stripped (never silently
    re-labelled as a CC licence) so callers can reject it explicitly.
    """
    if raw is None:
        return None
    key = re.sub(r"\s+", " ", raw.strip().lower())
    if not key:
        return None
    if key in KNOWN_LICENSES:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    # "CC BY 4.0" / "CC-BY-SA-3.0" style spellings with odd separators.
    squashed = key.replace(" ", "-")
    if squashed in KNOWN_LICENSES:
        return squashed
    return key


def is_creative_commons(license_key: str | None) -> bool:
    """True for any CC licence (including CC0) — not for own / standard."""
    return bool(license_key) and license_key.startswith("cc")


def is_lawful_source(license_key: str | None, own_content: bool = False) -> bool:
    """True when the source may be clipped: own, public-domain, or any CC."""
    if own_content:
        return True
    if license_key in (OWN_LICENSE, PUBLIC_DOMAIN_LICENSE):
        return True
    return is_creative_commons(license_key)


def publish_blocked_reason(license_key: str | None, own_content: bool = False) -> str | None:
    """Plain-language refusal reason, or ``None`` when publishing is allowed.

    Only an explicitly recorded Standard-licence third-party source is
    refused here. An unrecorded licence (``None``) is allowed through —
    local own-content jobs never record one — because the job-creation
    gate (YouTube ingest requires a declaration or a lawful licence)
    is what keeps unknown YouTube sources out in the first place.
    """
    if own_content:
        return None
    if normalize_license(license_key) == STANDARD_LICENSE:
        return (
            "This clip comes from a video under the YouTube Standard licence, "
            "so ClipForge cannot publish it. Only your own videos, public-domain "
            "videos, or Creative Commons videos may be posted."
        )
    return None


def license_display_name(license_key: str | None) -> str:
    """Human-readable licence name ("" when unknown)."""
    key = normalize_license(license_key)
    if key in CC_LICENSES:
        return CC_LICENSES[key][0]
    if key == STANDARD_LICENSE:
        return "YouTube Standard licence"
    return ""


def license_url(license_key: str | None) -> str:
    """Licence deed URL ("" when the licence has none / is unknown)."""
    key = normalize_license(license_key)
    return CC_LICENSES.get(key, ("", ""))[1] if key else ""


def build_attribution_text(
    source_title: str | None,
    source_channel: str | None,
    source_url: str | None,
    source_license: str | None,
) -> str:
    """Credit block for a CC / public-domain source, or "" when none applies.

    Format (single block, one line per fact — the SEO step appends this
    verbatim to every generated description)::

        Source: <title> by <channel> — <url> — Licensed under
        <licence name> (<licence url>)

    Public-domain sources get the same block without a licence URL.
    Own content and Standard-licence sources get no block (own content
    needs no credit; Standard-licence content must never be posted).
    """
    key = normalize_license(source_license)
    if key is None or key in (OWN_LICENSE, STANDARD_LICENSE):
        return ""
    if not is_creative_commons(key) and key != PUBLIC_DOMAIN_LICENSE:
        return ""
    title = (source_title or "").strip() or "Untitled video"
    channel = (source_channel or "").strip() or "Unknown creator"
    url = (source_url or "").strip()
    name = license_display_name(key)
    deed = license_url(key)
    licence_part = f"{name} ({deed})" if deed else name
    source_part = f"Source: {title} by {channel}"
    if url:
        source_part += f" — {url}"
    return f"{source_part} — Licensed under {licence_part}"


def append_attribution(description: str, attribution_text: str) -> str:
    """Append the credit block to a description, idempotently.

    Any credit block already present (a trailing paragraph starting
    with ``Source:`` that contains ``Licensed under``) is stripped
    first, so re-running SEO keeps exactly one block, never two — even
    if the licence changed between runs (the new block wins).
    """
    attribution_text = (attribution_text or "").strip()
    if not attribution_text:
        return description
    text = (description or "").rstrip()
    # Strip a previously appended block: it is always the trailing
    # paragraph starting with "Source: " and mentioning "Licensed under".
    marker = "\n\nSource: "
    idx = text.rfind(marker)
    if idx != -1 and "Licensed under" in text[idx:]:
        text = text[:idx].rstrip()
    elif text.startswith("Source: ") and "Licensed under" in text:
        text = ""
    if text:
        return f"{text}\n\n{attribution_text}"
    return attribution_text


def licence_record(
    source_license: str | None = None,
    source_url: str | None = None,
    source_title: str | None = None,
    source_channel: str | None = None,
    attribution_text: str | None = None,
) -> dict:
    """One record dict with exactly the shared field names.

    ``attribution_text`` is derived from the other four fields when
    omitted, so callers never have to build the credit block by hand.
    """
    key = normalize_license(source_license)
    text = attribution_text
    if text is None:
        text = build_attribution_text(source_title, source_channel, source_url, key)
    return {
        "source_license": key,
        "source_url": source_url or None,
        "source_title": source_title or None,
        "source_channel": source_channel or None,
        "attribution_text": text or None,
    }


def record_from_obj(obj: object) -> dict:
    """Read the licence record off a Job / ClipCandidate / Post row.

    Missing attributes read as ``None`` (older rows predate the
    columns), so this is safe on any row.
    """
    return licence_record(
        source_license=getattr(obj, "source_license", None),
        source_url=getattr(obj, "source_url", None),
        source_title=getattr(obj, "source_title", None),
        source_channel=getattr(obj, "source_channel", None),
        attribution_text=getattr(obj, "attribution_text", None),
    )


def merge_records(primary: dict, fallback: dict) -> dict:
    """Candidate record wins field-by-field; job record fills the gaps."""
    merged = {}
    for field in SOURCE_LICENSE_FIELDS:
        merged[field] = primary.get(field) or fallback.get(field)
    if not merged.get("attribution_text"):
        merged["attribution_text"] = build_attribution_text(
            merged.get("source_title"),
            merged.get("source_channel"),
            merged.get("source_url"),
            merged.get("source_license"),
        )
    return merged


__all__ = [
    "STANDARD_LICENSE",
    "OWN_LICENSE",
    "PUBLIC_DOMAIN_LICENSE",
    "CC_LICENSES",
    "KNOWN_LICENSES",
    "SOURCE_LICENSE_FIELDS",
    "normalize_license",
    "is_creative_commons",
    "is_lawful_source",
    "publish_blocked_reason",
    "license_display_name",
    "license_url",
    "build_attribution_text",
    "append_attribution",
    "licence_record",
    "record_from_obj",
    "merge_records",
]
