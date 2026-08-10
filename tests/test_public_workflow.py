from __future__ import annotations

import base64
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_script(filename: str, module_name: str):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


IMPORTERS = load_script("import_creators.py", "public_import_creators")
SCANNER = load_script("scan_gmail_replies.py", "public_scan_gmail_replies")
QUEUE = load_script("build_outreach_queue.py", "public_build_outreach_queue")
SENDER = load_script("send_outreach_queue.py", "public_send_outreach_queue")
RECORDER = load_script("record_outreach_results.py", "public_record_outreach_results")
POLICY = load_script("workflow_policy.py", "public_workflow_policy")


class PublicWorkflowTests(unittest.TestCase):
    def test_outreach_queue_renders_ready_records_and_skips_existing_threads(self):
        records = [
            {
                "record_id": "rec-ready",
                "fields": {
                    "达人昵称": "Alice",
                    "联系邮箱": "Alice@Example.com",
                    "平台": "TikTok",
                    "主页链接": {"link": "https://tiktok.com/@alice"},
                    "最新状态": "待触达",
                    "Gmail线程 ID": "",
                },
            },
            {
                "record_id": "rec-thread",
                "fields": {
                    "达人昵称": "Bob",
                    "联系邮箱": "bob@example.com",
                    "平台": "Instagram",
                    "主页链接": "https://instagram.com/bob",
                    "最新状态": "待触达",
                    "Gmail线程 ID": "thread-1",
                },
            },
        ]
        report = QUEUE.build_queue(
            records,
            project_name="Example Campaign",
            brand_or_product="Cola",
            subject_template="{{brand_or_product}} opportunity for {{creator_name}}",
            body_template="Hi {{creator_name}},\nWe would love to discuss {{brand_or_product}}.",
            attachment_path="/tmp/brief.pdf",
        )
        self.assertEqual(report["queue_count"], 1)
        self.assertEqual(report["queue"][0]["to"], "alice@example.com")
        self.assertEqual(report["queue"][0]["creator_email"], "alice@example.com")
        self.assertEqual(report["queue"][0]["source_identity"]["creator_name"], "Alice")
        self.assertEqual(report["queue"][0]["approval_status"], "待确认")
        self.assertIn("existing_gmail_thread", report["skipped"][0]["reason"])

    def test_sender_blocks_unapproved_batch_and_preserves_complete_draft(self):
        queue = {
            "mode": "review_only",
            "queue": [{
                "record_id": "rec-1",
                "creator_name": "Alice",
                "to": "alice@example.com",
                "subject": "Cola collaboration",
                "body": "Hi Alice,\n\nLet's work together.",
                "approval_status": "待确认",
            }],
        }
        items, errors = SENDER.approved_items(queue)
        self.assertEqual(items, [])
        self.assertIn("unapproved_items:Alice", errors)

        queue["queue"][0]["approval_status"] = "已确认"
        items, errors = SENDER.approved_items(queue)
        self.assertEqual(errors, [])
        message = SENDER.build_message(items[0])
        self.assertEqual(message["To"], "alice@example.com")
        self.assertEqual(message["Subject"], "Cola collaboration")
        self.assertIn("Let's work together.", message.get_content())

    def test_record_result_validation_requires_real_sent_ids(self):
        report = {
            "mode": "sent",
            "results": [{
                "record_id": "rec-1",
                "creator_name": "Alice",
                "to": "alice@example.com",
                "subject": "Cola collaboration",
                "body": "Hi Alice",
                "status": "sent",
                "sent_at": "2026-08-10T02:00:00+00:00",
                "message_id": "msg-1",
                "thread_id": "thread-1",
            }],
        }
        self.assertEqual(RECORDER.validate_result_file(report), [])
        bad = {**report, "mode": "review_only"}
        self.assertIn("result_mode_must_be_sent", RECORDER.validate_result_file(bad))

    def test_record_result_builds_audited_communication_and_creator_updates(self):
        result = {
            "record_id": "rec-1",
            "creator_name": "Alice",
            "to": "alice@example.com",
            "subject": "Cola collaboration",
            "body": "Hi Alice,\nLet's collaborate.",
            "attachment_path": "/tmp/brief.pdf",
            "status": "sent",
            "sent_at": "2026-08-10T02:00:00+00:00",
            "message_id": "msg-1",
            "thread_id": "thread-1",
        }
        communication, creator_update = RECORDER.build_fields(
            result,
            {
                "平台": "TikTok",
                "主页链接": {"link": "https://tiktok.com/@alice"},
            },
        )
        self.assertEqual(communication["方向"], "发出")
        self.assertEqual(communication["邮件 Message ID"], "msg-1")
        self.assertEqual(communication["发送审批状态"], "已发送")
        self.assertEqual(creator_update["Gmail线程 ID"], "thread-1")
        self.assertEqual(creator_update["最新状态"], "已触达")

    def test_result_writeback_rejects_creator_identity_mismatch(self):
        errors = RECORDER.validate_result_creator_identity(
            {
                "record_id": "rec-2",
                "creator_name": "Preksha",
                "creator_email": "preksha@example.com",
                "to": "preksha@example.com",
                "subject": "Re: Collaboration",
                "body": "Hi Preksha,\nPlease send the draft.",
                "platform": "TikTok",
                "profile_url": "https://tiktok.com/@alice",
                "source_identity": {
                    "record_id": "rec-2",
                    "creator_name": "Preksha",
                    "creator_email": "preksha@example.com",
                    "platform": "TikTok",
                    "profile_url": "https://tiktok.com/@alice",
                },
            },
            "rec-1",
            {
                "达人昵称": "Alice",
                "联系邮箱": "alice@example.com",
            },
            ["Alice", "Preksha"],
        )
        self.assertIn("result_record_id_does_not_match_creator_master", errors)
        self.assertIn("result_creator_name_does_not_match_creator_master", errors)
        self.assertIn("result_creator_email_does_not_match_creator_master", errors)
        self.assertIn("recipient_does_not_match_creator_master", errors)

    def test_sender_rejects_identity_mismatch_and_cross_creator_body(self):
        queue = {
            "mode": "review_only",
            "queue": [{
                "record_id": "rec-1",
                "creator_name": "Alice",
                "creator_email": "alice@example.com",
                "to": "nits@example.com",
                "subject": "Cola collaboration",
                "body": "Hi Preksha,\nLet's collaborate.",
                "platform": "TikTok",
                "profile_url": "https://tiktok.com/@alice",
                "source_identity": {
                    "creator_name": "Alice",
                    "creator_email": "alice@example.com",
                    "platform": "TikTok",
                    "profile_url": "https://tiktok.com/@alice",
                },
                "known_creator_names": ["Alice", "Preksha"],
                "approval_status": "已确认",
            }],
        }
        items, errors = SENDER.approved_items(queue)
        self.assertEqual(items, [])
        self.assertTrue(any("recipient_does_not_match_creator_master" in error for error in errors))
        self.assertTrue(any("greeting_name_does_not_match_creator" in error for error in errors))
        self.assertTrue(any("other_creator_name_in_message:Preksha" in error for error in errors))

    def test_sender_rejects_reused_thread_on_first_outreach(self):
        queue = {
            "mode": "review_only",
            "queue": [{
                "record_id": "rec-1",
                "creator_name": "Alice",
                "to": "alice@example.com",
                "subject": "Cola collaboration",
                "body": "Hi Alice",
                "thread_id": "thread-existing",
                "approval_status": "已确认",
            }],
        }
        items, errors = SENDER.approved_items(queue)
        self.assertEqual(items, [])
        self.assertIn("must_not_reuse_existing_thread", errors[0])

    def test_outreach_queue_skips_unresolved_template_values(self):
        report = QUEUE.build_queue(
            [{
                "record_id": "rec-1",
                "fields": {
                    "达人昵称": "Alice",
                    "联系邮箱": "alice@example.com",
                    "平台": "TikTok",
                    "主页链接": "https://tiktok.com/@alice",
                    "最新状态": "待触达",
                },
            }],
            project_name="Example Campaign",
            brand_or_product="",
            subject_template="{{brand_or_product}} opportunity",
            body_template="Hi {{creator_name}}",
            attachment_path="",
        )
        self.assertEqual(report["queue_count"], 0)
        self.assertEqual(report["skipped"][0]["reason"], "missing_template_values")

    def test_creator_import_normalizes_and_deduplicates_handles(self):
        rows = [
            (2, {"name": "Alice", "platform": "TikTok", "profile_url": "@alice", "followers": "1,200", "email": "alice@example.com"}),
            (3, {"name": "Alice duplicate", "platform": "TikTok", "profile_url": "https://www.tiktok.com/@alice/"}),
            (4, {"name": "Missing URL", "platform": "Instagram", "profile_url": ""}),
        ]

        creators, skipped = IMPORTERS.normalize_creators(rows)

        self.assertEqual(len(creators), 1)
        self.assertEqual(creators[0].profile_url, "https://tiktok.com/@alice")
        self.assertEqual(creators[0].followers, 1200)
        self.assertEqual(len(skipped), 2)
        self.assertEqual(skipped[0]["reason"], "duplicate_in_input")
        self.assertEqual(skipped[1]["reason"], "missing_name_or_profile_url")

    def test_gmail_matching_prefers_thread_then_falls_back_to_email(self):
        thread_creator = SCANNER.CreatorRef(
            "rec-1", "Alice", "alice@example.com", "thread-1", "", "https://tiktok.com/@alice", "TikTok", "已触达"
        )
        email_creator = SCANNER.CreatorRef(
            "rec-2", "Bob", "bob@example.com", "", "", "https://instagram.com/bob", "Instagram", "待触达"
        )

        thread_message = SCANNER.GmailMessage(
            "msg-1", "thread-1", 1, "Alice", "alice@example.com", "Re: Collaboration", "", "", "", "", "", ""
        )
        email_message = SCANNER.GmailMessage(
            "msg-2", "thread-2", 2, "Bob", "bob@example.com", "Re: Collaboration", "", "", "", "", "", ""
        )

        self.assertEqual(
            SCANNER.match_creator(thread_message, [thread_creator, email_creator])["status"],
            "unique_thread",
        )
        self.assertEqual(
            SCANNER.match_creator(email_message, [thread_creator, email_creator])["status"],
            "unique_email",
        )

    def test_plain_text_body_is_decoded(self):
        encoded = base64.urlsafe_b64encode(b"Interested. Rate: $300").decode().rstrip("=")
        message = SCANNER.parse_gmail_message(
            {
                "id": "msg-1",
                "threadId": "thread-1",
                "internalDate": "1",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [{"name": "From", "value": "Alice <alice@example.com>"}],
                    "body": {"data": encoded},
                },
            }
        )
        self.assertIn("Rate: $300", message.body)
        self.assertEqual(message.sender_email, "alice@example.com")

    def test_reply_classification_and_quote_extraction(self):
        message = SCANNER.GmailMessage(
            "msg-quote",
            "thread-quote",
            1,
            "Alice",
            "alice@example.com",
            "Re: Collaboration",
            "",
            "",
            "",
            "",
            "My rate is USD 280 for one Reel.",
            "",
        )
        self.assertEqual(SCANNER.classify_message(message), "已报价")
        self.assertEqual(SCANNER.parse_quote(message), (280, "USD"))

        interested = SCANNER.GmailMessage(
            "msg-interest",
            "thread-interest",
            1,
            "Alice",
            "alice@example.com",
            "Re: Collaboration",
            "",
            "",
            "",
            "",
            "I would love to collaborate with you.",
            "",
        )
        self.assertEqual(SCANNER.classify_message(interested), "未报价有意向")

    def test_reply_payloads_preserve_audit_fields(self):
        message = SCANNER.GmailMessage(
            "msg-1",
            "thread-1",
            1700000000000,
            "Alice",
            "alice@example.com",
            "Re: Collaboration",
            "<msg@example.com>",
            "",
            "",
            "",
            "My rate is $300.",
            "",
        )
        match = {
            "platform": "TikTok",
            "profile_url": "https://tiktok.com/@alice",
        }
        communication = SCANNER.communication_fields(
            message, match, "已报价", (300, "USD")
        )
        self.assertEqual(communication["邮件 Message ID"], "msg-1")
        self.assertEqual(communication["发送审批状态"], "不发送")
        self.assertEqual(communication["提取报价"], 300)
        update = SCANNER.creator_update_fields(message, "已报价", (300, "USD"))
        self.assertEqual(update["最新状态"], "已报价待评估")
        self.assertEqual(update["最近邮件 Message ID"], "msg-1")

    def test_rate_inquiry_requires_human_approval_after_eligibility_checks(self):
        allowed, reason = POLICY.can_auto_send_rate_inquiry(
            match_status="unique_thread",
            classification="interested",
            has_explicit_interest=True,
            has_quote=False,
            pending_same_type_count=0,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "external_creator_email_requires_human_approval")

        blocked, reason = POLICY.can_auto_send_rate_inquiry(
            match_status="ambiguous_thread",
            classification="interested",
            has_explicit_interest=True,
            has_quote=False,
            pending_same_type_count=0,
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "creator_match_is_not_unique")

        blocked, reason = POLICY.can_auto_send_rate_inquiry(
            match_status="unique_email",
            classification="interested",
            has_explicit_interest=True,
            has_quote=False,
            pending_same_type_count=1,
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "same_type_inquiry_is_pending")

    def test_payment_gate_requires_all_platform_links(self):
        allowed, _ = POLICY.can_enter_payment_collection(
            content_status="已发布",
            agreed_platforms=["TikTok", "Instagram"],
            publication_links={
                "TikTok": "https://tiktok.com/@alice/video/1",
                "Instagram": "https://instagram.com/p/1",
            },
        )
        self.assertTrue(allowed)

        blocked, reason = POLICY.can_enter_payment_collection(
            content_status="已发布",
            agreed_platforms=["TikTok", "Instagram"],
            publication_links={"TikTok": "https://tiktok.com/@alice/video/1"},
        )
        self.assertFalse(blocked)
        self.assertIn("Instagram", reason)

    def test_video_and_revision_gates_require_manual_intervention(self):
        blocked, reason = POLICY.video_intake_gate(script_status="脚本审核中", submitted_asset_type="video")
        self.assertFalse(blocked)
        self.assertIn("script_approval_required", reason)

        blocked, reason = POLICY.revision_gate(used_rounds=2, allowed_rounds=2)
        self.assertFalse(blocked)
        self.assertIn("exceeded", reason)

    def test_invoice_validation_catches_mismatch_and_duplicate(self):
        invoice = {
            "number": "INV-001",
            "legal_name": "Example Entity",
            "address": "Example Address",
            "currency": "USD",
            "amount": 300,
            "payee": "Alice",
            "attachment_readable": True,
        }
        valid, errors = POLICY.validate_invoice(
            invoice=invoice,
            expected={
                "legal_name": "Example Entity",
                "address": "Example Address",
                "currency": "USD",
                "amount": 300,
            },
            previously_seen_numbers={"INV-000"},
        )
        self.assertTrue(valid)
        self.assertEqual(errors, [])

        invalid, errors = POLICY.validate_invoice(
            invoice={**invoice, "amount": 250},
            expected={
                "legal_name": "Example Entity",
                "address": "Example Address",
                "currency": "USD",
                "amount": 300,
            },
            previously_seen_numbers={"INV-001"},
        )
        self.assertFalse(invalid)
        self.assertIn("invoice_number_duplicate", errors)
        self.assertIn("amount_mismatch", errors)

    def test_public_tree_has_no_internal_defaults(self):
        config = (ROOT / "config" / "project-config.example.json").read_text(encoding="utf-8")
        self.assertIn("YOUR LEGAL ENTITY NAME", config)
        self.assertIn('"payment_request_cc": []', config)

        context = (ROOT / "config" / "project-context.example.json").read_text(encoding="utf-8")
        self.assertIn('"preferred_language": "en"', context)
        self.assertIn('"external_email_send": "human_approval"', context)


if __name__ == "__main__":
    unittest.main()
