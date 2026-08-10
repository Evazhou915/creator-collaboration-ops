"""Pure policy checks for the public creator-operations workflow.

These functions do not call external APIs or mutate project data. They make the
high-risk gates executable and easy to test with synthetic records.
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import urlparse


UNIQUE_MATCH_STATUSES = {"unique_thread", "unique_email"}


def _identity_text(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def validate_creator_identity(
    *,
    record_id: str = "",
    creator_name: str = "",
    creator_email: str = "",
    to: str = "",
    body: str,
    subject: str = "",
    platform: str = "",
    profile_url: str = "",
    source_identity: dict[str, Any] | None = None,
    known_creator_names: Iterable[str] = (),
) -> list[str]:
    """Cross-check the intended creator before an external email or write-back."""

    errors: list[str] = []
    expected_name = _identity_text(creator_name)
    expected_email = _identity_text(creator_email)
    actual_email = _identity_text(to)
    if not expected_name:
        errors.append("creator_name_missing")
    if not expected_email:
        errors.append("creator_email_missing")
    if expected_email and actual_email != expected_email:
        errors.append("recipient_does_not_match_creator_master")

    if source_identity is not None:
        snapshot_checks = [
            ("creator_name", creator_name, source_identity.get("creator_name")),
            ("creator_email", creator_email, source_identity.get("creator_email")),
            ("platform", platform, source_identity.get("platform")),
            ("profile_url", profile_url, source_identity.get("profile_url")),
        ]
        if "record_id" in source_identity:
            snapshot_checks.insert(0, ("record_id", record_id, source_identity.get("record_id")))
        for label, current, snapshot in snapshot_checks:
            if _identity_text(current) != _identity_text(snapshot):
                errors.append(f"source_identity_mismatch:{label}")

    text_to_scan = f"{subject}\n{body}"
    greeting = re.search(
        r"(?im)^\s*(?:hi|hello|dear)\s+([^,!:\n]+)", text_to_scan
    )
    if greeting and expected_name:
        greeted_name = _identity_text(greeting.group(1))
        if greeted_name != expected_name:
            errors.append("greeting_name_does_not_match_creator")

    for other_name in known_creator_names:
        normalized = _identity_text(other_name)
        if len(normalized) >= 3 and normalized != expected_name and normalized in _identity_text(text_to_scan):
            errors.append(f"other_creator_name_in_message:{other_name}")
    return errors


def can_auto_send_rate_inquiry(
    *,
    match_status: str,
    classification: str,
    has_explicit_interest: bool,
    has_quote: bool,
    pending_same_type_count: int,
) -> tuple[bool, str]:
    """Return whether the narrow no-quote inquiry exception is safe."""

    if match_status not in UNIQUE_MATCH_STATUSES:
        return False, "creator_match_is_not_unique"
    if classification != "interested":
        return False, "message_is_not_a_standard_interest_reply"
    if not has_explicit_interest:
        return False, "interest_is_not_explicit"
    if has_quote:
        return False, "creator_already_provided_a_quote"
    if pending_same_type_count:
        return False, "same_type_inquiry_is_pending"
    return False, "external_creator_email_requires_human_approval"


def can_enter_payment_collection(
    *,
    content_status: str,
    agreed_platforms: Iterable[str],
    publication_links: dict[str, str],
) -> tuple[bool, str]:
    """Require published status and one valid URL for every agreed platform."""

    platforms = [platform.strip() for platform in agreed_platforms if platform.strip()]
    if content_status != "已发布":
        return False, "content_is_not_marked_published"
    if not platforms:
        return False, "agreed_platforms_are_missing"
    missing = [platform for platform in platforms if not _valid_http_url(publication_links.get(platform, ""))]
    if missing:
        return False, "publication_link_missing_or_invalid:" + ",".join(missing)
    return True, "all_agreed_platforms_have_valid_publication_links"


def validate_invoice(
    *,
    invoice: dict[str, Any],
    expected: dict[str, Any],
    previously_seen_numbers: set[str] | None = None,
) -> tuple[bool, list[str]]:
    """Validate invoice fields without accepting payment or sending anything."""

    errors: list[str] = []
    seen = previously_seen_numbers or set()
    number = str(invoice.get("number", "")).strip()
    if not number:
        errors.append("invoice_number_missing")
    elif number in seen:
        errors.append("invoice_number_duplicate")
    if invoice.get("legal_name") != expected.get("legal_name"):
        errors.append("legal_name_mismatch")
    if invoice.get("address") != expected.get("address"):
        errors.append("address_mismatch")
    if invoice.get("currency") != expected.get("currency"):
        errors.append("currency_mismatch")
    if invoice.get("amount") != expected.get("amount"):
        errors.append("amount_mismatch")
    if not invoice.get("payee"):
        errors.append("payee_missing")
    if not invoice.get("attachment_readable"):
        errors.append("invoice_attachment_missing_or_unreadable")
    return not errors, errors


def video_intake_gate(*, script_status: str, submitted_asset_type: str) -> tuple[bool, str]:
    """Prevent video review when the required script gate was skipped."""

    if submitted_asset_type.lower() == "video" and script_status != "已通过":
        return False, "flow_exception_script_approval_required_before_video_review"
    return True, "asset_can_enter_current_review_stage"


def revision_gate(*, used_rounds: int, allowed_rounds: int) -> tuple[bool, str]:
    if used_rounds >= allowed_rounds:
        return False, "free_revision_rounds_exceeded_manual_decision_required"
    return True, "revision_round_available"


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
