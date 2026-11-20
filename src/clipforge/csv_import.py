"""Bulk / CSV scheduling for ClipForge (Phase 2).

A CSV row schedules one post: ``clip_candidate_id`` (or ``video``/``clip``
— see below), ``platform``, ``scheduled_for``, optional ``title_override``.
Rows are validated one by one and every failure is reported per-row, so
one bad row never blocks the rest of the file.

Columns (header names are case-insensitive; ``clip`` is accepted as an
alias for ``clip_candidate_id``, ``video`` as an alias for ``video_path``):

* ``clip_candidate_id`` — required; must exist and be ``approved``
  (approval-first is preserved: a CSV can schedule, never publish).
* ``platform`` — required; one of youtube | instagram | facebook | tiktok.
* ``scheduled_for`` — required; ISO 8601 datetime (``2026-11-21T18:00:00Z``
  or with an offset). Naive datetimes are read as UTC.
* ``title_override`` — optional; replaces the SEO title for this post.
* ``account_id`` — optional; must exist if given.
* ``idempotency_key`` — optional; when omitted one is derived from
  ``clip_candidate_id + platform + scheduled_for + title_override``,
  so re-importing the same file is a no-op for rows already imported.
* ``brand_kit_id`` / ``title_variant_id`` — optional; must exist if given.

``import_csv`` runs in two modes: ``dry_run=True`` (default) only
validates and returns the preview — nothing is stored; ``dry_run=False``
stores the valid rows as :class:`clipforge.db.Post` rows with status
``pending_approval`` (they still need a human approve before the
scheduler will publish them) and skips rows whose idempotency key
already exists, reporting them as ``skipped_duplicate``.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from clipforge.db import BrandKit, ClipCandidate, Post, PostStatus, SocialAccount, TitleVariant

PLATFORMS = ("youtube", "instagram", "facebook", "tiktok")

# Header aliases -> canonical column name.
ALIASES = {
    "clip_candidate_id": "clip_candidate_id",
    "clip_id": "clip_candidate_id",
    "clip": "clip_candidate_id",
    "candidate_id": "clip_candidate_id",
    "platform": "platform",
    "scheduled_for": "scheduled_for",
    "scheduled_at": "scheduled_for",
    "schedule_time": "scheduled_for",
    "title_override": "title_override",
    "title": "title_override",
    "account_id": "account_id",
    "idempotency_key": "idempotency_key",
    "video_path": "video_path",
    "video": "video_path",
    "brand_kit_id": "brand_kit_id",
    "title_variant_id": "title_variant_id",
}

REQUIRED_COLUMNS = ("clip_candidate_id", "platform", "scheduled_for")


@dataclass
class CsvRow:
    """One parsed + validated CSV row (valid rows only)."""

    row_number: int  # 1-based data-row number (header is row 1)
    clip_candidate_id: str
    platform: str
    scheduled_for: datetime
    title_override: str = ""
    account_id: str | None = None
    video_path: str | None = None
    brand_kit_id: str | None = None
    title_variant_id: str | None = None
    idempotency_key: str = ""

    def to_dict(self) -> dict:
        d = {
            "row": self.row_number,
            "clip_candidate_id": self.clip_candidate_id,
            "platform": self.platform,
            "scheduled_for": self.scheduled_for.isoformat(),
            "title_override": self.title_override,
            "account_id": self.account_id,
            "video_path": self.video_path,
            "brand_kit_id": self.brand_kit_id,
            "title_variant_id": self.title_variant_id,
            "idempotency_key": self.idempotency_key,
        }
        return d


@dataclass
class CsvRowError:
    row_number: int
    message: str

    def to_dict(self) -> dict:
        return {"row": self.row_number, "error": self.message}


@dataclass
class CsvImportResult:
    created: list[CsvRow] = field(default_factory=list)
    skipped_duplicates: list[CsvRow] = field(default_factory=list)
    errors: list[CsvRowError] = field(default_factory=list)
    dry_run: bool = True

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "created": [r.to_dict() for r in self.created],
            "skipped_duplicates": [r.to_dict() for r in self.skipped_duplicates],
            "errors": [e.to_dict() for e in self.errors],
            "summary": {
                "total_rows": len(self.created) + len(self.skipped_duplicates) + len(self.errors),
                "created": len(self.created),
                "skipped_duplicates": len(self.skipped_duplicates),
                "errors": len(self.errors),
            },
        }


def derive_idempotency_key(
    clip_candidate_id: str, platform: str, scheduled_for: datetime, title_override: str = ""
) -> str:
    """Stable key for a row, so re-importing the same file is idempotent."""
    raw = "|".join(
        [
            clip_candidate_id.strip(),
            platform.strip().lower(),
            scheduled_for.isoformat(),
            title_override.strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def parse_datetime(value: str) -> datetime:
    """Parse an ISO 8601 datetime; naive values are read as UTC."""
    text = (value or "").strip()
    if not text:
        raise ValueError("scheduled_for is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"scheduled_for {text!r} is not a valid ISO 8601 datetime") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_csv(text: str) -> tuple[list[dict], list[CsvRowError]]:
    """Parse CSV text into raw row dicts (canonical keys) + header errors.

    Returns ``(rows, errors)`` where ``rows`` are ``{"row_number": n,
    "values": {...}}`` and ``errors`` covers missing required columns
    and malformed CSV. Row-level validation happens in
    :func:`validate_rows` (it needs the DB).
    """
    errors: list[CsvRowError] = []
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return [], [CsvRowError(1, "CSV is empty — expected a header row")]
    normalised = {}
    for raw_name in reader.fieldnames:
        if raw_name is None:
            continue
        key = raw_name.strip().lower()
        if key in ALIASES:
            normalised[raw_name] = ALIASES[key]
    missing = [c for c in REQUIRED_COLUMNS if c not in normalised.values()]
    if missing:
        return [], [CsvRowError(1, f"missing required column(s): {', '.join(missing)}")]
    rows: list[dict] = []
    for i, raw in enumerate(reader, start=2):
        values = {}
        for raw_name, value in raw.items():
            if raw_name is None or raw_name not in normalised:
                continue
            values[normalised[raw_name]] = (value or "").strip()
        if not any(values.values()):
            continue  # skip blank lines
        rows.append({"row_number": i, "values": values})
    return rows, errors


def validate_rows(session: Session, raw_rows: list[dict]) -> tuple[list[CsvRow], list[CsvRowError]]:
    """Validate parsed rows against the DB; returns (valid, errors)."""
    valid: list[CsvRow] = []
    errors: list[CsvRowError] = []
    for raw in raw_rows:
        n = raw["row_number"]
        v = raw["values"]
        try:
            clip_id = v.get("clip_candidate_id", "")
            if not clip_id:
                raise ValueError("clip_candidate_id is required")
            cand = session.get(ClipCandidate, clip_id)
            if cand is None:
                raise ValueError(f"clip candidate {clip_id!r} not found")
            if cand.status != "approved":
                raise ValueError(
                    f"clip candidate {clip_id!r} is '{cand.status}', not 'approved' — "
                    "approve it in review before scheduling"
                )
            platform = (v.get("platform") or "").lower()
            if platform not in PLATFORMS:
                raise ValueError(f"platform must be one of {PLATFORMS}, got {platform!r}")
            scheduled_for = parse_datetime(v.get("scheduled_for", ""))
            account_id = v.get("account_id") or None
            if account_id and session.get(SocialAccount, account_id) is None:
                raise ValueError(f"account {account_id!r} not found")
            brand_kit_id = v.get("brand_kit_id") or None
            if brand_kit_id and session.get(BrandKit, brand_kit_id) is None:
                raise ValueError(f"brand kit {brand_kit_id!r} not found")
            variant_id = v.get("title_variant_id") or None
            if variant_id:
                variant = session.get(TitleVariant, variant_id)
                if variant is None:
                    raise ValueError(f"title variant {variant_id!r} not found")
                if variant.clip_candidate_id != clip_id or variant.platform != platform:
                    raise ValueError(
                        f"title variant {variant_id!r} does not belong to "
                        f"clip {clip_id!r} on {platform}"
                    )
            title_override = v.get("title_override", "")
            key = v.get("idempotency_key") or derive_idempotency_key(
                clip_id, platform, scheduled_for, title_override
            )
            valid.append(
                CsvRow(
                    row_number=n,
                    clip_candidate_id=clip_id,
                    platform=platform,
                    scheduled_for=scheduled_for,
                    title_override=title_override,
                    account_id=account_id,
                    video_path=v.get("video_path") or None,
                    brand_kit_id=brand_kit_id,
                    title_variant_id=variant_id,
                    idempotency_key=key,
                )
            )
        except ValueError as exc:
            errors.append(CsvRowError(n, str(exc)))
    return valid, errors


def import_csv(session: Session, text: str, dry_run: bool = True) -> CsvImportResult:
    """Parse, validate and (unless dry-run) store a CSV of scheduled posts."""
    raw_rows, header_errors = parse_csv(text)
    result = CsvImportResult(dry_run=dry_run, errors=list(header_errors))
    if header_errors:
        return result
    valid, row_errors = validate_rows(session, raw_rows)
    result.errors.extend(row_errors)
    for row in valid:
        existing = session.query(Post).filter_by(idempotency_key=row.idempotency_key).first()
        if existing is not None:
            result.skipped_duplicates.append(row)
            continue
        if dry_run:
            result.created.append(row)  # preview: would-be-created rows
            continue
        seo: dict = {}
        if row.title_override:
            seo = {"title": row.title_override}
        post = Post(
            clip_candidate_id=row.clip_candidate_id,
            account_id=row.account_id,
            platform=row.platform,
            scheduled_for=row.scheduled_for,
            status=PostStatus.PENDING_APPROVAL.value,
            seo_json=seo,
            video_path=row.video_path,
            idempotency_key=row.idempotency_key,
            brand_kit_id=row.brand_kit_id,
            title_variant_id=row.title_variant_id,
        )
        session.add(post)
        result.created.append(row)
    if not dry_run:
        session.commit()
    return result


__all__ = [
    "CsvRow",
    "CsvRowError",
    "CsvImportResult",
    "PLATFORMS",
    "derive_idempotency_key",
    "parse_datetime",
    "parse_csv",
    "validate_rows",
    "import_csv",
]
