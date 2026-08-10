# Creator Collaboration Operations Skill

A reusable, human-in-the-loop workflow for creator collaboration operations.

It covers project-isolated Feishu Bitable setup, creator-list normalization, Gmail thread matching, outreach preparation, content-delivery tracking, publication-link collection, Invoice validation, and payment-readiness checks.

## What This Is

This repository is a generic workflow and reference implementation. It is not a turnkey SaaS product and it does not include credentials, creator data, company billing defaults, contracts, or private campaign material.

## Safety Model

- The project's Feishu Bitable is the operational source of truth.
- A creator is identified by platform plus profile URL.
- First outreach is batch-approval gated.
- Negotiation, contracts, content feedback, and payment requests require explicit human approval.
- TikTok and Instagram DMs remain manual by default.
- The initial Gmail scanner is read-only.
- Actual payment is complete only after payment evidence is recorded.

## Quick Start

1. Copy `config/project-config.example.json` and `config/project-context.example.json` into a private project workspace.
2. Fill in the project configuration and private legal/payment details. Add approved product claims, brand voice, commercial rules, and approval policy to the project context.
3. Run `scripts/bootstrap_feishu_bitable.py` without `--apply` to inspect the schema.
4. Install the Python dependency with `python3 -m pip install -r requirements.txt`.
5. Configure `FEISHU_APP_ID` and `FEISHU_APP_SECRET` through a secret manager or environment, then run the initializer with `--apply`.
6. Review the generated `feishu-project-manifest.json` before importing any list.
7. Run `scripts/import_creators.py` without `--apply` to review normalization and duplicates.
8. Create a private first-outreach template and set `outreach.first_email_template_path` in the project config. Use placeholders such as `{{creator_name}}`, `{{brand_or_product}}`, `{{platform}}`, and `{{profile_url}}`.
9. Use `scripts/build_outreach_queue.py` to generate the approval queue. It validates email, status, existing threads, template values, duplicate recipients, the creator identity snapshot, greeting name, and the optional Brief attachment; it never sends mail:

   ```bash
   python3 scripts/build_outreach_queue.py \\
     --manifest /absolute/path/to/feishu-project-manifest.json \\
     --config /absolute/path/to/project-config.json
   ```

10. Use `scripts/scan_gmail_replies.py` for the read-only first inbox scan. Review the JSON report before any write:

   ```bash
   python3 scripts/scan_gmail_replies.py \\
     --manifest /absolute/path/to/feishu-project-manifest.json \\
     --since 2026-01-01T00:00:00+08:00
   ```

   After reviewing the report, add `--apply` to create immutable Communication Log records and update uniquely matched Creator Master records. `--apply` never sends email; ambiguous or already-processed messages are not written.

See `SKILL.md`, `references/workflow-spec.md`, and `references/safety-model.md` for the complete workflow and approval gates.

## Contributing And Security

Read `CONTRIBUTING.md` before opening a pull request and `SECURITY.md` before reporting a vulnerability. The public release checklist is in `RELEASE-CHECKLIST.md`.

## Outreach Send Gate

Review the full drafts first. The sender defaults to preview mode:

```bash
python3 scripts/send_outreach_queue.py \\
  --queue /absolute/path/to/gmail-outreach-queue.json
```

Only after every item has been individually marked `approval_status=已确认` and the project owner has explicitly approved the batch may the caller add `--send`. The sender then validates all recipients, subjects, bodies, attachments, duplicate addresses, first-outreach thread rules, and recipient/creator identity consistency before calling Gmail. It records returned `message_id` and `thread_id` plus the identity snapshot in a separate result file. There is no partial-send mode.

## Outreach Result Write-Back

After a real send, the sender writes a result file containing the recipient, complete body, attachment path, sent time, Gmail `message_id`, and `thread_id`. Review the pending Feishu updates first:

```bash
python3 scripts/record_outreach_results.py \\
  --manifest /absolute/path/to/feishu-project-manifest.json \\
  --result /absolute/path/to/gmail-outreach-queue-send-result.json
```

Only after reviewing the generated record-result file may the caller add `--apply`. This creates one immutable Communication Log record per new Message ID and updates the matching Creator Master record to `已触达`. Existing Message IDs, missing creator records, and recipient/creator identity mismatches are skipped or sent to manual review. The local attachment path remains in the send-result file; the Feishu attachment column is not populated with a fake token. Uploading the Brief into Feishu is a separate explicit step. This command never sends Gmail messages.

## Gmail Write-Back Mode

The scanner is read-only by default. With `--apply`, it writes only:

- one Communication Log record per uniquely matched, new Gmail message;
- the creator's thread ID, latest Message ID, last interaction time, progress summary, and safely extracted quote;
- a conservative status transition for quote, interest, registration, asset submission, or decline.

Classification that does not fit an existing Bitable option, ambiguous matches, duplicate message IDs, and messages without a unique creator match stay in the report for manual review. Existing Communication Log message IDs are checked before `--apply`, so a retry after a partial failure does not create the same email record twice. The scanner still never sends Gmail messages.

## Tests

The public tree includes synthetic tests that do not call Gmail or Feishu:

```bash
python3 -m unittest discover -s tests -v
```

## Public vs Private Configuration

The example configuration intentionally contains placeholders. Keep legal entity data, payment recipients, contracts, creator records, email content, OAuth tokens, scan state, and generated reports outside a public repository.

## License

MIT. See `LICENSE`.
