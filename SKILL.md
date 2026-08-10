---
name: creator-collaboration-ops
description: >
  Set up and operate a creator collaboration project from outreach-list import through
  Gmail/DM outreach, reply triage, rate negotiation, content review, publication-link
  collection, invoice validation, and payment-request preparation. Creates a project-specific
  Feishu Bitable as the source of truth and applies explicit approval gates for high-impact
  actions. Use when starting a creator/KOC/influencer collaboration project, importing a creator
  list, processing creator replies, negotiating creator rates, tracking creator deliverables, or
  preparing creator payment requests.
---

# Creator Collaboration Operations

Create one isolated project workflow at a time. Use the project's Feishu Bitable as the sole operational record; do not split current data across old spreadsheets, inbox labels, and personal notes.

Read [workflow-spec.md](references/workflow-spec.md) and [safety-model.md](references/safety-model.md) before creating a project, changing its schema, processing creator mail, or progressing a creator to a new stage. Read the private project context before drafting claims, commercial terms, or external messages.

## Start A Project

1. Collect project name, owner, time zone, product/brand description, Brief, budget/currency, target platforms, templates, and optional campaign window.
2. Ask whether the project attaches a contract. If yes, collect the contract template.
3. Copy `config/project-config.example.json` and `config/project-context.example.json` into the new project's private workspace and fill the project-specific values. Keep product claims, brand voice, commercial limits, and private billing data out of the public repository.
4. Run the initializer in dry-run mode first:

   ```bash
   python3 scripts/bootstrap_feishu_bitable.py \\
     --config /absolute/path/to/project-config.json
   ```

5. After checking the field plan, create the new base explicitly with `--apply`. Set `FEISHU_APP_ID` and `FEISHU_APP_SECRET` through the approved secret/environment configuration; never put credentials in the JSON file or source code.
6. The initializer creates a new base, four tables, and the scalar fields/options, then writes `feishu-project-manifest.json` with the app/table IDs. It does not modify an existing base, configure views, or send external messages.
7. Use `平台 + 主页链接` as the cross-table identity key in the first version. The initializer stores this key in each operational table; linked-record fields can be added only after their exact Feishu payload is validated in a disposable base.
8. Store the project configuration, source links, and file versions where the operating team can find them.
9. Do not reuse a prior project's live Bitable. Historical projects are read-only references.

## Import And Prepare Outreach

1. Read the supplied creator list with a structured spreadsheet parser. For CSV/TSV input, use `scripts/import_creators.py` with the project's `feishu-project-manifest.json`:

   ```bash
   python3 scripts/import_creators.py \\
     --manifest /absolute/path/to/feishu-project-manifest.json \\
     --input /absolute/path/to/creators.csv
   ```

2. Review the generated JSON report. Add `--apply` only after confirming the candidate and skipped counts. The importer reads the current Creator Master first when applying, writes at most 500 records per request, and never sends mail.
3. Normalize platform, profile URL, email, follower count, and outreach channel. Handles such as `@name` are expanded using the platform domain.
4. Deduplicate against the current project's Creator Master using platform plus profile URL. Show skipped duplicates and their matching evidence.
5. Classify records with valid email as Gmail candidates; records without usable email become manual-DM candidates.
6. Create Creator Master and Communication Log records before creating any mail.
7. Generate the Gmail outreach queue with the correct greeting, template, and Brief attachment. Validate creator name, recipient email, attachments, and project before presenting the queue.
8. Send the entire first-outreach Gmail queue only after the project owner explicitly approves the batch. For every Gmail message, record the returned threadId and message ID.
9. Before sending, cross-check the queue identity snapshot against `record_id`, creator name, recipient email, platform, and profile URL. Also verify the greeting name and stop if another known creator name appears in the subject or body.
10. Never automate TikTok or Instagram DMs. Generate copy and a manual task, then record the actual send time when supplied.

## Process Gmail Replies

Run the active-project inbox scan at 10:30 in the project's time zone. Read only messages received since the last successful scan. The read-only first stage is available as:

```bash
python3 scripts/scan_gmail_replies.py \\
  --manifest /absolute/path/to/feishu-project-manifest.json \\
  --since 2026-08-06T10:30:00+08:00
```

The first run requires `--since`; later runs use the local cursor beside the manifest. The scanner reads Gmail and the current Creator Master, writes a local report, and advances the cursor only after all reads succeed. It does not write Bitable records or send messages.

1. Match each message to exactly one creator by threadId first, then by email. If no unique match exists, create a manual-review item instead of guessing.
2. Add an immutable Communication Log record and update the creator's latest status, latest progress, last interaction time, and quoted price when available.
3. Classify replies as quoted, interested without quote, declined, registration confirmation, asset submission, automated response, or manual review.
4. When writing send results back, compare the result creator name, recipient, platform, profile URL, greeting, and known creator names against Creator Master. Any mismatch becomes manual review and is not written.
5. When a uniquely matched creator explicitly expresses interest but gives no rate, prepare a standard rate-inquiry draft in the existing Gmail thread. Set the recipient explicitly from Creator Master, show the full draft, and send only after the owner explicitly approves it.
6. Do not prepare a sendable draft when the message is a bounce, out-of-office, ambiguous agency response, incomplete quote, or unknown sender.

## Negotiate And Confirm Collaboration

1. For quoted creators, present rate, currency, deliverables, platform, rights, CPM when available, and a recommended negotiation number.
2. Send negotiation emails only after the owner confirms each creator's amount. Keep every reply in the existing thread and explicitly set the creator recipient.
3. When the creator accepts a negotiated rate, prepare a confirmation email listing final price and deliverables. Include the contract only when the project configuration enables it.
4. Send confirmation and download/registration instructions only after owner approval.

## Onboard And Deliver

1. After collaboration confirmation, request registration confirmation. Record the registration email in Finance.
2. Create a manual recharge task. Do not operate account credits directly.
3. After a teammate records recharge completion and evidence, prepare a message asking the creator for a specific script-draft date and include the project campaign window when configured. Send only after owner approval.
4. Create one Deliverables record for each deliverable. A single video cross-posted across platforms stays one record, with all required platforms and publication links recorded on it.
5. Require script approval before accepting video drafts. Scripts, video drafts, and captions receive feedback within 48 hours.
6. Allow two free script revision rounds and two free video revision rounds. Flag further rounds for owner decision.
7. The owner writes review feedback and explicitly approves every script, video, and caption feedback email before it is sent.

## Publish And Pay

1. Do not set a default publication deadline after final approval. Record an optional creator-provided date and the actual publish times.
2. Keep the deliverable in pending publication until every agreed platform is published and every platform URL is collected.
3. Once all deliverables are complete, prepare one creator email requesting payment method, payment account, and Invoice. Include the legal entity name and registered office address from the project configuration.
4. Send that email only after owner approval. Validate the returned Invoice before preparing an internal payment request.
5. Draft internal payment requests in the project's configured language, including recipient, configured CCs, amount, payee/payment method, body, attachments, and relevant links. Never use a public default for legal or payment details.
6. Send payment requests only after owner approval. Mark actual payment complete only after a teammate records payment evidence; a sent request is not a payment.

## Safety Gates

- Treat a profile URL plus platform as the creator identity. Names and similar email handles are not sufficient.
- Before every Gmail send, verify the creator, explicit `To` email, threadId, subject, attachment, and workflow state. Stop for manual review if any check fails.
- Keep emails in their original threads. Never create a new outreach thread while an active collaboration thread exists.
- Keep external messages, content approvals, contracts, recharge, invoices, and payment evidence traceable through linked Bitable records.
- Use manual review for missing information, contradictory data, unexpected attachments, failed sends, expired links, or state transitions that violate the workflow spec.
- Do not infer product claims, rates, rights, deadlines, or payment readiness from incomplete project context.
