# Safety Model

The public workflow is designed around reversible, auditable actions. External communication and financial actions are not implied by a parsed email or a state transition.

## Identity

Use `platform + normalized profile URL` as the creator identity. A name, email handle, or display name alone is insufficient. Gmail matching tries the original thread first and uses the sender email only as a unique fallback.

For every external Gmail send and first-outreach write-back, cross-check the intended creator name, Creator Master email, explicit `To`, platform, profile URL, and the queue's source identity snapshot. If the message has a greeting, it must name the intended creator; if the subject or body contains another known creator's name, stop for manual review. A mismatch must never be resolved by guessing.

## Automatic Actions

Only narrow, low-risk actions may be automatic:

- normalize and deduplicate imported records;
- read and classify new Gmail messages;
- validate recipient/creator identity snapshots and create manual-review items for mismatches;
- create local reports and manual-review items;
- prepare a rate-inquiry or script-date draft when all required identity and workflow checks pass; neither draft is sent without explicit owner approval.

## Human Gates

Require explicit approval before:

- first outreach;
- any price commitment or negotiation;
- collaboration confirmation or contract confirmation;
- content feedback;
- payment-detail requests;
- payment requests and actual payment.

## Payment Gate

Payment collection can begin only when the deliverable is marked `已发布`, every agreed platform has a valid HTTP(S) publication URL, and no agreed platform is missing. A sent payment request is not proof of payment.

## Failure Handling

When a condition is ambiguous, preserve the raw input, create a manual-review item, and do not guess. Examples include multiple thread matches, incomplete quotes, duplicate inquiries, missing attachments, skipped script approval, exceeded revision rounds, missing cross-post links, and invoice mismatches.
