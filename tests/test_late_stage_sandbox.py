from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "late_stage_sandbox.json"


def load_executor():
    scripts = ROOT / "scripts"
    sys.path.insert(0, str(scripts))
    path = scripts / "run_late_stage_sandbox.py"
    spec = importlib.util.spec_from_file_location("public_late_stage_sandbox", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EXECUTOR = load_executor()


class LateStageSandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.report = EXECUTOR.execute_sandbox(cls.payload)
        cls.creators = {
            creator["creator_id"]: creator for creator in cls.report["creators"]
        }

    def test_fixture_contains_exactly_six_synthetic_creators(self):
        self.assertEqual(len(self.payload["creators"]), 6)
        self.assertEqual(len({item["creator_id"] for item in self.payload["creators"]}), 6)
        self.assertTrue(all(item["email"].endswith("@example.test") for item in self.payload["creators"]))
        self.assertTrue(all("synthetic" in item["profile_url"] for item in self.payload["creators"]))

    def test_complete_lifecycle_reaches_paid_only_with_payment_evidence(self):
        creator = self.creators["syn-001"]
        self.assertEqual(creator["rate_assessment"]["decision"], "negotiate")
        self.assertEqual(creator["registration_status"], "confirmed")
        self.assertEqual(creator["recharge_status"], "completed")
        self.assertEqual(creator["script_status"], "approved")
        self.assertEqual(creator["video_status"], "approved")
        self.assertEqual(creator["caption_status"], "approved")
        self.assertEqual(creator["publication_status"], "complete")
        self.assertEqual(creator["invoice_status"], "valid")
        self.assertEqual(creator["payment_request_status"], "submitted")
        self.assertEqual(creator["payment_status"], "paid")
        self.assertEqual(
            creator["payment_evidence"],
            {
                "reference": "local://evidence/payment-syn-001",
                "recorded_by": "synthetic-finance-reviewer",
                "recorded_at": "2026-08-12T16:30:00Z",
            },
        )
        self.assertEqual(creator["status"], "completed")

    def test_published_before_all_content_approvals_is_rejected(self):
        creator = self.payload["creators"][0]
        payload = {"project": self.payload["project"], "creators": [{**creator, "events": creator["events"][:6] + [
            {"event_id": "out-of-order-publish", "type": "published", "links": {
                "TikTok": "https://example.test/publications/early-tiktok",
                "Instagram": "https://example.test/publications/early-instagram",
            }}
        ]}]}
        result = EXECUTOR.execute_sandbox(payload)["creators"][0]
        reasons = [item["reason"] for item in result["exceptions"]]
        self.assertIn(
            "content_approval_required_before_publication:script,video,caption", reasons
        )
        self.assertEqual(result["publication_status"], "pending")
        self.assertEqual(result["publication_links"], {})

    def test_negotiation_target_validates_positive_finite_amount_and_currency(self):
        creator = self.payload["creators"][0]
        invalid_targets = [
            ("negative", -1, "USD", "target_amount_must_be_positive_and_finite"),
            ("nonnumeric", "not-a-number", "USD", "target_amount_must_be_positive_and_finite"),
            ("infinite", "Infinity", "USD", "target_amount_must_be_positive_and_finite"),
            ("currency", 500, "EUR", "target_currency_must_match_project_currency"),
            ("missing-currency", 500, None, "target_currency_must_match_project_currency"),
        ]
        for label, amount, currency, expected_reason in invalid_targets:
            with self.subTest(label=label):
                event = {"event_id": f"invalid-target-{label}", "type": "negotiation_requested", "target_amount": amount}
                if currency is not None:
                    event["currency"] = currency
                payload = {"project": self.payload["project"], "creators": [{**creator, "events": [
                    creator["events"][0],
                    event,
                ]}]}
                result = EXECUTOR.execute_sandbox(payload)["creators"][0]
                self.assertEqual(result["actions"], [])
                self.assertIn(expected_reason, [item["reason"] for item in result["exceptions"]])

    def test_numeric_string_negotiation_target_is_normalized(self):
        creator = self.payload["creators"][0]
        payload = {"project": self.payload["project"], "creators": [{**creator, "events": [
            creator["events"][0],
            {"event_id": "numeric-string-target", "type": "negotiation_requested", "target_amount": "475.5", "currency": "USD"},
        ]}]}
        result = EXECUTOR.execute_sandbox(payload)["creators"][0]
        draft = result["actions"][0]
        self.assertEqual(draft["proposed_amount"], 475.5)
        self.assertIn("USD 475.5", draft["body"])

    def test_negotiation_acceptance_without_evaluated_quote_is_rejected(self):
        creator = self.payload["creators"][0]
        payload = {"project": self.payload["project"], "creators": [{**creator, "events": [
            {"event_id": "accept-without-quote", "type": "negotiation_accepted", "amount": 500, "currency": "USD"}
        ]}]}
        result = EXECUTOR.execute_sandbox(payload)["creators"][0]
        self.assertIsNone(result["final_rate"])
        self.assertIn(
            "negotiation_acceptance_requires_rate_evaluation_state",
            [item["reason"] for item in result["exceptions"]],
        )

    def test_negotiation_acceptance_validates_currency_and_positive_finite_amount(self):
        creator = self.payload["creators"][0]
        base_events = [creator["events"][0]]
        invalid_events = [
            {"event_id": "wrong-currency", "type": "negotiation_accepted", "amount": 500, "currency": "EUR"},
            {"event_id": "zero-amount", "type": "negotiation_accepted", "amount": 0, "currency": "USD"},
            {"event_id": "infinite-amount", "type": "negotiation_accepted", "amount": "Infinity", "currency": "USD"},
        ]
        payload = {"project": self.payload["project"], "creators": [{**creator, "events": base_events + invalid_events}]}
        result = EXECUTOR.execute_sandbox(payload)["creators"][0]
        reasons = [item["reason"] for item in result["exceptions"]]
        self.assertIn("accepted_currency_must_match_evaluated_quote", reasons)
        self.assertEqual(reasons.count("accepted_amount_must_be_positive_and_finite"), 2)
        self.assertIsNone(result["final_rate"])

    def test_payment_evidence_cannot_complete_a_draft_payment_request(self):
        creator = self.payload["creators"][0]
        events = creator["events"][:15] + [
            {"event_id": "early-payment-evidence", "type": "payment_evidence_recorded", "evidence": {"reference": "local://evidence/too-early", "recorded_by": "reviewer", "recorded_at": "2026-08-12T16:30:00Z"}}
        ]
        payload = {"project": self.payload["project"], "creators": [{**creator, "events": events}]}
        result = EXECUTOR.execute_sandbox(payload)["creators"][0]
        self.assertEqual(result["payment_request_status"], "draft_pending_approval")
        self.assertEqual(result["payment_status"], "not_paid")
        self.assertIn(
            "submitted_payment_request_required_before_payment_completion",
            [item["reason"] for item in result["exceptions"]],
        )

    def test_payment_evidence_requires_structured_auditable_fields(self):
        creator = self.payload["creators"][0]
        prefix = creator["events"][:17]
        invalid_evidence = [
            ("string", "local://evidence/payment", "payment_evidence_must_be_an_object"),
            ("missing", {"reference": "ref", "recorded_by": "reviewer"}, "payment_evidence_required_fields_missing"),
            ("bad-time", {"reference": "ref", "recorded_by": "reviewer", "recorded_at": "yesterday"}, "payment_evidence_recorded_at_must_be_iso8601"),
            ("naive-time", {"reference": "ref", "recorded_by": "reviewer", "recorded_at": "2026-08-12T16:30:00"}, "payment_evidence_recorded_at_must_be_iso8601"),
        ]
        for label, evidence, expected_reason in invalid_evidence:
            with self.subTest(label=label):
                event = {"event_id": f"invalid-evidence-{label}", "type": "payment_evidence_recorded", "evidence": evidence}
                payload = {"project": self.payload["project"], "creators": [{**creator, "events": prefix + [event]}]}
                result = EXECUTOR.execute_sandbox(payload)["creators"][0]
                self.assertEqual(result["payment_status"], "not_paid")
                self.assertIsNone(result["payment_evidence"])
                self.assertIn(expected_reason, [item["reason"] for item in result["exceptions"]])

    def test_invoice_event_requires_nonempty_attachment(self):
        creator = self.payload["creators"][0]
        events = [dict(event) for event in creator["events"][:14]]
        events[-1]["attachment"] = "  "
        payload = {"project": self.payload["project"], "creators": [{**creator, "events": events + [
            {"event_id": "prepare-without-attachment", "type": "prepare_payment_request"}
        ]}]}
        result = EXECUTOR.execute_sandbox(payload)["creators"][0]
        self.assertEqual(result["invoice_status"], "invalid")
        self.assertIn("invoice_event_attachment_missing", result["invoice_errors"])
        self.assertEqual(result["payment_request_status"], "not_prepared")
        payment_drafts = [action for action in result["actions"] if action["type"] == "internal_payment_request"]
        self.assertEqual(payment_drafts, [])

    def test_registration_confirmation_requires_email_or_account(self):
        creator = self.payload["creators"][0]
        events = [dict(event) for event in creator["events"][:5]]
        events[-1]["registration_email"] = ""
        payload = {"project": self.payload["project"], "creators": [{**creator, "events": events}]}
        result = EXECUTOR.execute_sandbox(payload)["creators"][0]
        self.assertEqual(result["registration_status"], "requested")
        self.assertEqual(result["recharge_status"], "not_started")
        self.assertIn(
            "registration_email_or_account_required",
            [item["reason"] for item in result["exceptions"]],
        )

    def test_duplicate_event_id_is_ignored_without_replaying_side_effects(self):
        creator = self.payload["creators"][0]
        quote_event = creator["events"][0]
        negotiation_event = creator["events"][1]
        payload = {
            "project": self.payload["project"],
            "creators": [{
                **creator,
                "events": [quote_event, negotiation_event, dict(negotiation_event)],
            }],
        }
        result = EXECUTOR.execute_sandbox(payload)["creators"][0]
        negotiation_drafts = [
            action for action in result["actions"]
            if action["type"] == "negotiation_email"
        ]
        self.assertEqual(len(negotiation_drafts), 1)
        self.assertIn(
            "duplicate_event_id_ignored",
            [item["reason"] for item in result["exceptions"]],
        )

    def test_duplicate_event_id_with_different_payload_is_still_ignored(self):
        creator = self.payload["creators"][0]
        quote_event = creator["events"][0]
        conflicting = {"event_id": quote_event["event_id"], "type": "negotiation_requested", "target_amount": 1, "currency": "USD"}
        payload = {"project": self.payload["project"], "creators": [{**creator, "events": [quote_event, conflicting]}]}
        result = EXECUTOR.execute_sandbox(payload)["creators"][0]
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["status"], "rate_evaluated")
        self.assertIn("duplicate_event_id_ignored", [item["reason"] for item in result["exceptions"]])

    def test_new_event_ids_cannot_replay_business_actions(self):
        creator = self.payload["creators"][0]
        replay_cases = [
            (creator["events"][:2], {**creator["events"][1], "event_id": "replay-negotiation"}, "negotiation_email"),
            (creator["events"][:8], {**creator["events"][7], "event_id": "replay-script-review"}, "script_feedback"),
            (creator["events"][:15], {**creator["events"][14], "event_id": "replay-prepare"}, "internal_payment_request"),
        ]
        for prefix, replay, action_type in replay_cases:
            with self.subTest(action_type=action_type):
                payload = {"project": self.payload["project"], "creators": [{**creator, "events": prefix + [replay]}]}
                result = EXECUTOR.execute_sandbox(payload)["creators"][0]
                actions = [action for action in result["actions"] if action["type"] == action_type]
                self.assertEqual(len(actions), 1)

        payment_replays = [
            (creator["events"][:16], {**creator["events"][15], "event_id": "replay-approval"}),
            (creator["events"][:17], {**creator["events"][16], "event_id": "replay-submission"}),
            (creator["events"], {**creator["events"][17], "event_id": "replay-evidence"}),
        ]
        for prefix, replay in payment_replays:
            with self.subTest(event_type=replay["type"]):
                payload = {"project": self.payload["project"], "creators": [{**creator, "events": prefix + [replay]}]}
                result = EXECUTOR.execute_sandbox(payload)["creators"][0]
                self.assertTrue(result["exceptions"])
                self.assertEqual(result["audit_log"][-1]["type"], replay["type"])

    def test_completed_state_is_immutable(self):
        creator = self.payload["creators"][0]
        regression = {"event_id": "post-completion-quote", "type": "quote_received", "amount": 1, "currency": "USD"}
        payload = {"project": self.payload["project"], "creators": [{**creator, "events": creator["events"] + [regression]}]}
        result = EXECUTOR.execute_sandbox(payload)["creators"][0]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["payment_status"], "paid")
        self.assertIn("terminal_state_is_immutable", [item["reason"] for item in result["exceptions"]])

    def test_caption_before_video_approval_is_rejected(self):
        creator = self.payload["creators"][0]
        payload = {"project": self.payload["project"], "creators": [{**creator, "events": creator["events"][:9] + [
            {"event_id": "early-caption", "type": "caption_submitted"}
        ]}]}
        result = EXECUTOR.execute_sandbox(payload)["creators"][0]
        self.assertEqual(result["caption_status"], "not_submitted")
        self.assertIn("video_approval_required_before_caption_intake", [item["reason"] for item in result["exceptions"]])

    def test_wrong_invoice_and_links_types_become_exceptions(self):
        creator = self.payload["creators"][0]
        bad_links = {"project": self.payload["project"], "creators": [{**creator, "events": creator["events"][:12] + [
            {"event_id": "bad-links", "type": "published", "links": ["not", "an", "object"]}
        ]}]}
        links_result = EXECUTOR.execute_sandbox(bad_links)["creators"][0]
        self.assertIn("publication_links_must_be_an_object", [item["reason"] for item in links_result["exceptions"]])

        bad_invoice_event = {**creator["events"][13], "event_id": "bad-invoice", "invoice": []}
        bad_invoice = {"project": self.payload["project"], "creators": [{**creator, "events": creator["events"][:13] + [bad_invoice_event]}]}
        invoice_result = EXECUTOR.execute_sandbox(bad_invoice)["creators"][0]
        self.assertEqual(invoice_result["invoice_status"], "invalid")
        self.assertIn("invoice_must_be_an_object", [item["reason"] for item in invoice_result["exceptions"]])

    def test_bad_top_level_payload_shapes_raise_clear_value_errors(self):
        bad_payloads = [
            ([], "payload must be an object"),
            ({"project": [], "creators": []}, "project must be an object"),
            ({"project": self.payload["project"], "creators": {}}, "creators must be an array"),
            ({"project": self.payload["project"], "creators": ["creator"]}, "creators[0] must be an object"),
            ({"project": self.payload["project"], "creators": [{**self.payload["creators"][0], "events": {}}]}, "creators[0].events must be an array"),
        ]
        for payload, message in bad_payloads:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message.replace("[", r"\[").replace("]", r"\]")):
                    EXECUTOR.execute_sandbox(payload)

    def test_missing_event_id_is_rejected_without_side_effects(self):
        creator = self.payload["creators"][0]
        payload = {
            "project": self.payload["project"],
            "creators": [{
                **creator,
                "events": [{"type": "quote_received", "amount": 500, "currency": "USD"}],
            }],
        }
        result = EXECUTOR.execute_sandbox(payload)["creators"][0]
        self.assertIsNone(result["quote"])
        self.assertEqual(result["actions"], [])
        self.assertIn(
            "event_id_required",
            [item["reason"] for item in result["exceptions"]],
        )

    def test_every_outbound_action_is_a_pending_draft(self):
        drafts = [
            action
            for creator in self.report["creators"]
            for action in creator["actions"]
            if action.get("type") in EXECUTOR.DRAFT_EVENT_TYPES
        ]
        self.assertTrue(drafts)
        self.assertTrue(self.report["all_outbound_drafts_pending"])
        self.assertTrue(all(action["delivery_mode"] == "draft_only" for action in drafts))
        self.assertTrue(all(action["approval_status"] == "pending" for action in drafts))
        self.assertFalse(any(action.get("status") == "sent" for action in drafts))

    def test_executor_reports_no_external_calls(self):
        self.assertTrue(self.report["sandbox_mode"])
        self.assertEqual(self.report["external_calls_made"], [])
        source = (ROOT / "scripts" / "run_late_stage_sandbox.py").read_text(encoding="utf-8")
        self.assertNotIn("googleapiclient", source)
        self.assertNotIn("requests", source)
        self.assertNotIn("lark", source.casefold())

    def test_missing_cross_post_link_blocks_invoice_intake(self):
        creator = self.creators["syn-002"]
        reasons = [item["reason"] for item in creator["exceptions"]]
        self.assertTrue(any("YouTube" in reason for reason in reasons))
        self.assertIn("content_is_not_marked_published", reasons)
        self.assertEqual(creator["publication_status"], "pending")
        self.assertEqual(creator["invoice_status"], "not_received")
        self.assertEqual(creator["payment_request_status"], "not_prepared")

    def test_duplicate_and_mismatched_invoice_blocks_payment_request(self):
        creator = self.creators["syn-003"]
        self.assertEqual(creator["invoice_status"], "invalid")
        self.assertIn("invoice_number_duplicate", creator["invoice_errors"])
        self.assertIn("address_mismatch", creator["invoice_errors"])
        self.assertIn("amount_mismatch", creator["invoice_errors"])
        self.assertEqual(creator["payment_request_status"], "not_prepared")
        reasons = [item["reason"] for item in creator["exceptions"]]
        self.assertIn("valid_invoice_required_before_payment_request", reasons)

    def test_video_before_script_approval_is_rejected(self):
        creator = self.creators["syn-004"]
        self.assertEqual(creator["video_status"], "not_submitted")
        reasons = [item["reason"] for item in creator["exceptions"]]
        self.assertIn(
            "flow_exception_script_approval_required_before_video_review", reasons
        )

    def test_missing_recharge_evidence_blocks_script_intake(self):
        creator = self.creators["syn-005"]
        reasons = [item["reason"] for item in creator["exceptions"]]
        self.assertIn("recharge_evidence_required", reasons)
        self.assertIn("recharge_completion_required_before_script_intake", reasons)
        self.assertEqual(creator["recharge_status"], "manual_task_pending")
        self.assertEqual(creator["script_status"], "not_submitted")

    def test_revision_limit_requires_manual_decision(self):
        creator = self.creators["syn-006"]
        self.assertEqual(creator["script_revision_rounds"], 2)
        reasons = [item["reason"] for item in creator["exceptions"]]
        self.assertIn("free_revision_rounds_exceeded_manual_decision_required", reasons)

    def test_cli_reports_validation_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = Path(temp_dir) / "bad.json"
            fixture.write_text(json.dumps({"project": [], "creators": []}), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "run_late_stage_sandbox.py"), "--fixture", str(fixture)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, "")
            self.assertIn("validation error: project must be an object", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)

    def test_cli_writes_a_valid_local_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "report.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "run_late_stage_sandbox.py"),
                    "--fixture",
                    str(FIXTURE),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["creator_count"], 6)
            self.assertEqual(report["external_calls_made"], [])


if __name__ == "__main__":
    unittest.main()
