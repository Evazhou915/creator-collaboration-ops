#!/usr/bin/env python3
"""Run a synthetic, local-only late-stage creator collaboration sandbox.

The executor reads JSON events, applies deterministic workflow gates, and writes a
JSON report. It has no Gmail or Feishu dependencies and never sends messages.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from workflow_policy import (
    can_enter_payment_collection,
    revision_gate,
    validate_invoice,
    video_intake_gate,
)


DRAFT_EVENT_TYPES = {
    "negotiation_email",
    "collaboration_confirmation",
    "registration_instructions",
    "script_timeline_request",
    "script_feedback",
    "video_feedback",
    "caption_feedback",
    "payment_details_request",
    "invoice_clarification",
    "internal_payment_request",
}


def _draft(kind: str, recipient: str, subject: str, body: str, **details: Any) -> dict[str, Any]:
    draft = {
        "type": kind,
        "delivery_mode": "draft_only",
        "approval_status": "pending",
        "recipient": recipient,
        "subject": subject,
        "body": body,
    }
    draft.update(details)
    return draft


def _initial_state(creator: dict[str, Any]) -> dict[str, Any]:
    return {
        "creator_id": creator["creator_id"],
        "creator_name": creator["name"],
        "platform": creator["platform"],
        "profile_url": creator["profile_url"],
        "email": creator["email"],
        "status": "quoted_pending_evaluation",
        "quote": None,
        "final_rate": None,
        "registration_status": "not_started",
        "recharge_status": "not_started",
        "recharge_evidence": "",
        "script_status": "not_submitted",
        "script_revision_rounds": 0,
        "video_status": "not_submitted",
        "video_revision_rounds": 0,
        "caption_status": "not_submitted",
        "publication_status": "pending",
        "publication_links": {},
        "invoice_status": "not_received",
        "invoice_errors": [],
        "payment_request_status": "not_prepared",
        "payment_status": "not_paid",
        "payment_evidence": None,
        "actions": [],
        "exceptions": [],
        "audit_log": [],
    }


def _exception(state: dict[str, Any], event_type: str, reason: str) -> None:
    state["exceptions"].append({"event_type": event_type, "reason": reason})


def _add_draft(state: dict[str, Any], draft: dict[str, Any]) -> None:
    if draft["type"] not in DRAFT_EVENT_TYPES:
        raise ValueError(f"unsupported_draft_type:{draft['type']}")
    state["actions"].append(draft)


def _rate_assessment(quote: float, max_rate: float | None) -> dict[str, Any]:
    if max_rate is None:
        return {
            "decision": "manual_review",
            "recommended_rate": None,
            "reason": "project_max_rate_missing",
        }
    if quote > max_rate:
        return {
            "decision": "negotiate",
            "recommended_rate": max_rate,
            "reason": "quote_exceeds_project_limit",
        }
    recommended = round(quote * 0.9, 2)
    return {
        "decision": "acceptable_or_negotiate",
        "recommended_rate": recommended,
        "reason": "quote_within_project_limit",
    }


def _publication_gate(state: dict[str, Any], creator: dict[str, Any]) -> tuple[bool, str]:
    return can_enter_payment_collection(
        content_status="published" if state["publication_status"] == "complete" else "pending",
        agreed_platforms=creator["deliverable"]["platforms"],
        publication_links=state["publication_links"],
        published_status="published",
    )


def _process_event(
    state: dict[str, Any],
    creator: dict[str, Any],
    event: dict[str, Any],
    project: dict[str, Any],
    seen_invoice_numbers: set[str],
) -> None:
    event_type = event.get("type", "")
    state["audit_log"].append({"event_id": event.get("event_id", ""), "type": event_type})
    if state["status"] == "completed":
        _exception(state, event_type, "terminal_state_is_immutable")
        return
    email = state["email"]
    currency = project["commercial_rules"]["currency"]

    if event_type == "quote_received":
        if state["status"] != "quoted_pending_evaluation":
            _exception(state, event_type, "quote_received_requires_initial_state")
            return
        try:
            quote = float(event["amount"])
        except (KeyError, TypeError, ValueError):
            _exception(state, event_type, "quote_amount_must_be_positive_and_finite")
            return
        quote_currency = str(event.get("currency", "")).strip()
        if not math.isfinite(quote) or quote <= 0:
            _exception(state, event_type, "quote_amount_must_be_positive_and_finite")
            return
        if quote_currency != currency:
            _exception(state, event_type, "quote_currency_must_match_project_currency")
            return
        state["quote"] = {"amount": quote, "currency": quote_currency}
        state["rate_assessment"] = _rate_assessment(
            quote, project["commercial_rules"].get("max_creator_rate")
        )
        state["status"] = "rate_evaluated"
        return

    if event_type == "negotiation_requested":
        if state["status"] != "rate_evaluated" or not state.get("rate_assessment"):
            _exception(state, event_type, "quote_must_be_evaluated_before_negotiation")
            return
        raw_amount = event.get("target_amount", state["rate_assessment"]["recommended_rate"])
        if isinstance(raw_amount, bool):
            _exception(state, event_type, "target_amount_must_be_positive_and_finite")
            return
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            _exception(state, event_type, "target_amount_must_be_positive_and_finite")
            return
        if not math.isfinite(amount) or amount <= 0:
            _exception(state, event_type, "target_amount_must_be_positive_and_finite")
            return
        target_currency = str(event.get("currency", "")).strip()
        if target_currency != currency:
            _exception(state, event_type, "target_currency_must_match_project_currency")
            return
        _add_draft(
            state,
            _draft(
                "negotiation_email",
                email,
                "Collaboration rate proposal",
                f"Hello {state['creator_name']},\n\nWould you consider {currency} {amount:g} for the agreed deliverable?",
                proposed_amount=amount,
                currency=currency,
            ),
        )
        state["status"] = "negotiation_draft_pending_approval"
        return

    if event_type == "negotiation_accepted":
        quote = state.get("quote")
        if state["status"] not in {"rate_evaluated", "negotiation_draft_pending_approval"}:
            _exception(state, event_type, "negotiation_acceptance_requires_rate_evaluation_state")
            return
        if not quote or not state.get("rate_assessment"):
            _exception(state, event_type, "evaluated_quote_required_before_acceptance")
            return
        accepted_currency = str(event.get("currency", "")).strip()
        if accepted_currency != quote["currency"] or accepted_currency != currency:
            _exception(state, event_type, "accepted_currency_must_match_evaluated_quote")
            return
        try:
            accepted_amount = float(event["amount"])
        except (KeyError, TypeError, ValueError):
            _exception(state, event_type, "accepted_amount_must_be_positive_and_finite")
            return
        if not math.isfinite(accepted_amount) or accepted_amount <= 0:
            _exception(state, event_type, "accepted_amount_must_be_positive_and_finite")
            return
        state["final_rate"] = accepted_amount
        state["status"] = "collaboration_confirmation_pending_approval"
        deliverable = creator["deliverable"]["description"]
        _add_draft(
            state,
            _draft(
                "collaboration_confirmation",
                email,
                "Collaboration confirmation",
                f"Hello {state['creator_name']},\n\nPlease confirm {deliverable} for {currency} {state['final_rate']:g}.",
                final_rate=state["final_rate"],
                currency=currency,
                deliverable=deliverable,
            ),
        )
        return

    if event_type == "collaboration_confirmed":
        if state["status"] != "collaboration_confirmation_pending_approval":
            _exception(state, event_type, "collaboration_confirmation_draft_required")
            return
        if state["final_rate"] is None:
            _exception(state, event_type, "final_rate_required_before_confirmation")
            return
        state["status"] = "registration_pending"
        state["registration_status"] = "requested"
        _add_draft(
            state,
            _draft(
                "registration_instructions",
                email,
                "Registration instructions",
                f"Hello {state['creator_name']},\n\nPlease complete registration and confirm the account email.",
            ),
        )
        return

    if event_type == "registration_confirmed":
        if state["registration_status"] != "requested":
            _exception(state, event_type, "registration_was_not_requested")
            return
        account_reference = str(
            event.get("registration_email") or event.get("registration_account") or ""
        ).strip()
        if not account_reference:
            _exception(state, event_type, "registration_email_or_account_required")
            return
        state["registration_status"] = "confirmed"
        state["registration_email"] = str(event.get("registration_email", "")).strip()
        state["registration_account"] = account_reference
        state["recharge_status"] = "manual_task_pending"
        state["actions"].append({
            "type": "manual_recharge_task",
            "approval_status": "manual_action_required",
            "delivery_mode": "local_task",
            "account_reference": account_reference,
        })
        state["status"] = "recharge_pending"
        return

    if event_type == "recharge_completed":
        if state["recharge_status"] != "manual_task_pending":
            _exception(state, event_type, "manual_recharge_task_required_before_completion")
            return
        if state["registration_status"] != "confirmed":
            _exception(state, event_type, "registration_confirmation_required_before_recharge")
            return
        evidence = str(event.get("evidence", "")).strip()
        if not evidence:
            _exception(state, event_type, "recharge_evidence_required")
            return
        state["recharge_status"] = "completed"
        state["recharge_evidence"] = evidence
        state["status"] = "script_timeline_pending_approval"
        _add_draft(
            state,
            _draft(
                "script_timeline_request",
                email,
                "Script timeline",
                f"Hello {state['creator_name']},\n\nPlease confirm a specific date for your script draft.",
            ),
        )
        return

    if event_type == "script_submitted":
        if state["script_status"] not in {"not_submitted", "revision_requested"}:
            _exception(state, event_type, "script_intake_requires_submission_state")
            return
        if state["recharge_status"] != "completed":
            _exception(state, event_type, "recharge_completion_required_before_script_intake")
            return
        state["script_status"] = "review_pending"
        state["status"] = "script_review_pending"
        return

    if event_type == "script_reviewed":
        if state["script_status"] != "review_pending":
            _exception(state, event_type, "script_submission_required_before_review")
            return
        decision = event.get("decision")
        if decision == "approved":
            state["script_status"] = "approved"
        elif decision == "revision_requested":
            allowed, reason = revision_gate(
                used_rounds=state["script_revision_rounds"],
                allowed_rounds=project["delivery_policy"]["script_free_revision_rounds"],
            )
            if not allowed:
                _exception(state, event_type, reason)
                return
            state["script_revision_rounds"] += 1
            state["script_status"] = "revision_requested"
        else:
            _exception(state, event_type, "unsupported_script_review_decision")
            return
        _add_draft(
            state,
            _draft(
                "script_feedback",
                email,
                "Script review feedback",
                event.get("feedback", "Script review completed."),
                decision=decision,
            ),
        )
        return

    if event_type == "video_submitted":
        if state["video_status"] not in {"not_submitted", "revision_requested"}:
            _exception(state, event_type, "video_intake_requires_submission_state")
            return
        allowed, reason = video_intake_gate(
            script_status=state["script_status"],
            submitted_asset_type="video",
            approved_status="approved",
        )
        if not allowed:
            _exception(state, event_type, reason)
            return
        state["video_status"] = "review_pending"
        state["status"] = "video_review_pending"
        return

    if event_type == "video_reviewed":
        if state["video_status"] != "review_pending":
            _exception(state, event_type, "video_submission_required_before_review")
            return
        decision = event.get("decision")
        if decision == "approved":
            state["video_status"] = "approved"
        elif decision == "revision_requested":
            allowed, reason = revision_gate(
                used_rounds=state["video_revision_rounds"],
                allowed_rounds=project["delivery_policy"]["video_free_revision_rounds"],
            )
            if not allowed:
                _exception(state, event_type, reason)
                return
            state["video_revision_rounds"] += 1
            state["video_status"] = "revision_requested"
        else:
            _exception(state, event_type, "unsupported_video_review_decision")
            return
        _add_draft(
            state,
            _draft(
                "video_feedback",
                email,
                "Video review feedback",
                event.get("feedback", "Video review completed."),
                decision=decision,
            ),
        )
        return

    if event_type == "caption_submitted":
        if state["caption_status"] not in {"not_submitted", "revision_requested"}:
            _exception(state, event_type, "caption_intake_requires_submission_state")
            return
        if state["video_status"] != "approved":
            _exception(state, event_type, "video_approval_required_before_caption_intake")
            return
        state["caption_status"] = "review_pending"
        return

    if event_type == "caption_reviewed":
        if state["caption_status"] != "review_pending":
            _exception(state, event_type, "caption_submission_required_before_review")
            return
        decision = event.get("decision")
        if decision not in {"approved", "revision_requested"}:
            _exception(state, event_type, "unsupported_caption_review_decision")
            return
        state["caption_status"] = decision
        _add_draft(
            state,
            _draft(
                "caption_feedback",
                email,
                "Caption review feedback",
                event.get("feedback", "Caption review completed."),
                decision=decision,
            ),
        )
        return

    if event_type == "published":
        if state["publication_status"] != "pending":
            _exception(state, event_type, "publication_requires_pending_state")
            return
        links = event.get("links")
        if not isinstance(links, dict):
            _exception(state, event_type, "publication_links_must_be_an_object")
            return
        if not all(isinstance(platform, str) and isinstance(url, str) for platform, url in links.items()):
            _exception(state, event_type, "publication_links_must_map_strings_to_strings")
            return
        unapproved_content = [
            name
            for name in ("script", "video", "caption")
            if state[f"{name}_status"] != "approved"
        ]
        if unapproved_content:
            _exception(
                state,
                event_type,
                "content_approval_required_before_publication:" + ",".join(unapproved_content),
            )
            return
        state["publication_links"].update(links)
        allowed, reason = can_enter_payment_collection(
            content_status="published",
            agreed_platforms=creator["deliverable"]["platforms"],
            publication_links=state["publication_links"],
            published_status="published",
        )
        if not allowed:
            state["publication_status"] = "pending"
            _exception(state, event_type, reason)
            return
        state["publication_status"] = "complete"
        state["status"] = "payment_details_pending_approval"
        billing = project["billing"]
        _add_draft(
            state,
            _draft(
                "payment_details_request",
                email,
                "Payment details and invoice",
                "Please provide your payment method, payment account, and invoice "
                f"issued to {billing['legal_name']}, {billing['registered_office_address']}.",
            ),
        )
        return

    if event_type == "invoice_received":
        if state["invoice_status"] != "not_received":
            _exception(state, event_type, "invoice_intake_requires_not_received_state")
            return
        publication_ready, publication_reason = _publication_gate(state, creator)
        if not publication_ready:
            _exception(state, event_type, publication_reason)
            return
        invoice = event.get("invoice")
        if not isinstance(invoice, dict):
            state["invoice_status"] = "invalid"
            state["invoice_errors"] = ["invoice_must_be_an_object"]
            state["status"] = "invoice_review_exception"
            _exception(state, event_type, "invoice_must_be_an_object")
            return
        attachment = str(event.get("attachment", "")).strip()
        expected = {
            "legal_name": project["billing"]["legal_name"],
            "address": project["billing"]["registered_office_address"],
            "currency": currency,
            "amount": state["final_rate"],
        }
        valid, errors = validate_invoice(
            invoice=invoice,
            expected=expected,
            previously_seen_numbers=seen_invoice_numbers,
        )
        if not attachment:
            errors.append("invoice_event_attachment_missing")
        payment_method = str(event.get("payment_method", "")).strip()
        payment_account = str(event.get("payment_account", "")).strip()
        if not payment_method:
            errors.append("payment_method_missing")
        if not payment_account:
            errors.append("payment_account_missing")
        state["invoice_errors"] = errors
        if errors:
            state["invoice_status"] = "invalid"
            state["status"] = "invoice_review_exception"
            _exception(state, event_type, "invoice_validation_failed")
            _add_draft(
                state,
                _draft(
                    "invoice_clarification",
                    email,
                    "Invoice clarification needed",
                    "Please review the following invoice issues: " + ", ".join(errors),
                    validation_errors=errors,
                ),
            )
            return
        seen_invoice_numbers.add(str(invoice["number"]).strip())
        state["invoice_status"] = "valid"
        state["invoice_number"] = invoice["number"]
        state["invoice_attachment"] = attachment
        state["payment_method"] = payment_method
        state["payment_account"] = payment_account
        state["status"] = "payment_request_ready"
        return

    if event_type == "prepare_payment_request":
        if state["payment_request_status"] != "not_prepared":
            _exception(state, event_type, "payment_request_must_not_be_prepared_twice")
            return
        if state["invoice_status"] != "valid":
            _exception(state, event_type, "valid_invoice_required_before_payment_request")
            return
        billing = project["billing"]
        _add_draft(
            state,
            _draft(
                "internal_payment_request",
                billing["payment_request_recipient"],
                f"Payment request: {state['creator_name']}",
                f"Please review payment of {currency} {state['final_rate']:g} to {state['creator_name']}.",
                cc=billing.get("payment_request_cc", []),
                amount=state["final_rate"],
                currency=currency,
                payee=state["creator_name"],
                payment_method=state["payment_method"],
                payment_account=state["payment_account"],
                attachments=[state.get("invoice_attachment", "")],
                relevant_links=list(state["publication_links"].values()),
            ),
        )
        state["payment_request_status"] = "draft_pending_approval"
        state["status"] = "payment_request_pending_approval"
        return

    if event_type == "payment_request_approved":
        if state["payment_request_status"] != "draft_pending_approval":
            _exception(state, event_type, "payment_request_draft_required_before_approval")
            return
        state["payment_request_status"] = "approved"
        state["status"] = "payment_request_approved"
        return

    if event_type == "payment_request_submitted":
        if state["payment_request_status"] != "approved":
            _exception(state, event_type, "payment_request_approval_required_before_submission")
            return
        state["payment_request_status"] = "submitted"
        state["status"] = "payment_pending_evidence"
        return

    if event_type == "payment_evidence_recorded":
        if state["payment_request_status"] != "submitted":
            _exception(state, event_type, "submitted_payment_request_required_before_payment_completion")
            return
        evidence = event.get("evidence")
        if not isinstance(evidence, dict):
            _exception(state, event_type, "payment_evidence_must_be_an_object")
            return
        required = ("reference", "recorded_by", "recorded_at")
        if any(not isinstance(evidence.get(field), str) or not evidence[field].strip() for field in required):
            _exception(state, event_type, "payment_evidence_required_fields_missing")
            return
        try:
            recorded_at = datetime.fromisoformat(evidence["recorded_at"].replace("Z", "+00:00"))
        except ValueError:
            _exception(state, event_type, "payment_evidence_recorded_at_must_be_iso8601")
            return
        if recorded_at.tzinfo is None:
            _exception(state, event_type, "payment_evidence_recorded_at_must_be_iso8601")
            return
        state["payment_evidence"] = {
            field: evidence[field].strip() for field in required
        }
        state["payment_status"] = "paid"
        state["status"] = "completed"
        return

    _exception(state, event_type, "unsupported_event_type")


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def _require_nonempty_string(mapping: dict[str, Any], field: str, path: str) -> None:
    if not isinstance(mapping.get(field), str) or not mapping[field].strip():
        raise ValueError(f"{path}.{field} must be a nonempty string")


def _validate_payload(payload: Any) -> dict[str, Any]:
    payload = _require_dict(payload, "payload")
    project = _require_dict(payload.get("project"), "project")
    commercial = _require_dict(project.get("commercial_rules"), "project.commercial_rules")
    delivery = _require_dict(project.get("delivery_policy"), "project.delivery_policy")
    billing = _require_dict(project.get("billing"), "project.billing")
    _require_nonempty_string(commercial, "currency", "project.commercial_rules")
    max_rate = commercial.get("max_creator_rate")
    if max_rate is not None:
        if isinstance(max_rate, bool):
            raise ValueError("project.commercial_rules.max_creator_rate must be a positive finite number")
        try:
            normalized_max_rate = float(max_rate)
        except (TypeError, ValueError):
            raise ValueError("project.commercial_rules.max_creator_rate must be a positive finite number")
        if not math.isfinite(normalized_max_rate) or normalized_max_rate <= 0:
            raise ValueError("project.commercial_rules.max_creator_rate must be a positive finite number")
        commercial["max_creator_rate"] = normalized_max_rate
    for field in ("script_free_revision_rounds", "video_free_revision_rounds"):
        if not isinstance(delivery.get(field), int) or isinstance(delivery[field], bool) or delivery[field] < 0:
            raise ValueError(f"project.delivery_policy.{field} must be a nonnegative integer")
    for field in ("legal_name", "registered_office_address", "payment_request_recipient"):
        _require_nonempty_string(billing, field, "project.billing")
    if "payment_request_cc" in billing:
        cc = billing["payment_request_cc"]
        if not isinstance(cc, list) or not all(isinstance(item, str) and item.strip() for item in cc):
            raise ValueError("project.billing.payment_request_cc must be an array of nonempty strings")
    creators = payload.get("creators")
    if not isinstance(creators, list):
        raise ValueError("creators must be an array")
    if "previously_seen_invoice_numbers" in payload:
        numbers = payload["previously_seen_invoice_numbers"]
        if not isinstance(numbers, list) or not all(isinstance(item, str) and item.strip() for item in numbers):
            raise ValueError("previously_seen_invoice_numbers must be an array of nonempty strings")
    for index, creator_value in enumerate(creators):
        path = f"creators[{index}]"
        creator = _require_dict(creator_value, path)
        for field in ("creator_id", "name", "platform", "profile_url", "email"):
            _require_nonempty_string(creator, field, path)
        deliverable = _require_dict(creator.get("deliverable"), f"{path}.deliverable")
        _require_nonempty_string(deliverable, "description", f"{path}.deliverable")
        platforms = deliverable.get("platforms")
        if not isinstance(platforms, list) or not platforms or not all(isinstance(item, str) and item.strip() for item in platforms):
            raise ValueError(f"{path}.deliverable.platforms must be a nonempty array of strings")
        events = creator.get("events", [])
        if not isinstance(events, list):
            raise ValueError(f"{path}.events must be an array")
        if not all(isinstance(event, dict) for event in events):
            raise ValueError(f"{path}.events must contain only objects")
    return payload


def execute_sandbox(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute all creator event streams without external side effects."""

    payload = _validate_payload(payload)
    project = payload["project"]
    seen_invoice_numbers = set(payload.get("previously_seen_invoice_numbers", []))
    results = []
    for creator in payload.get("creators", []):
        state = _initial_state(creator)
        seen_event_ids: set[str] = set()
        for event in creator.get("events", []):
            event_type = event.get("type", "")
            if not isinstance(event_type, str) or not event_type.strip():
                _exception(state, "", "event_type_required")
                continue
            event_type = event_type.strip()
            event_id_value = event.get("event_id")
            if not isinstance(event_id_value, str) or not event_id_value.strip():
                _exception(state, event_type, "event_id_required")
                continue
            event_id = event_id_value.strip()
            if event_id in seen_event_ids:
                _exception(state, event_type, "duplicate_event_id_ignored")
                continue
            seen_event_ids.add(event_id)
            _process_event(state, creator, event, project, seen_invoice_numbers)
        results.append(state)

    outbound_drafts = [
        action
        for result in results
        for action in result["actions"]
        if action.get("type") in DRAFT_EVENT_TYPES
    ]
    all_outbound_drafts_pending = all(
        draft.get("delivery_mode") == "draft_only"
        and draft.get("approval_status") == "pending"
        for draft in outbound_drafts
    )
    return {
        "sandbox_mode": True,
        "external_calls_made": [],
        "creator_count": len(results),
        "draft_count": len(outbound_drafts),
        "all_outbound_drafts_pending": all_outbound_drafts_pending,
        "creators": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = json.loads(args.fixture.read_text(encoding="utf-8"))
        report = execute_sandbox(deepcopy(payload))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"validation error: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
