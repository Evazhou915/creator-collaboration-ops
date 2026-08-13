# New Project Minimum Onboarding Checklist

Use this checklist to create a private project workspace before running the local late-stage sandbox. Do not place completed project files in this public repository.

## Required Project Inputs

- [ ] Assign a project name, owner, time zone, budget, currency, and optional campaign window.
- [ ] Record the product description, approved claims, required disclosures, brand voice, and prohibited claims.
- [ ] Define target platforms, deliverable descriptions, usage rights, rate ceiling, and included revision rounds.
- [ ] Provide private templates for negotiation, collaboration confirmation, registration, timeline requests, content feedback, payment-detail requests, invoice clarification, and internal payment requests.
- [ ] Decide whether a contract is required and, if so, provide the private contract file.
- [ ] Provide the private legal entity name, registered office address, internal payment-request recipient, and approved CC list.
- [ ] Define who approves price commitments, external messages, content feedback, invoice exceptions, and payment requests.

## Creator And Event Preparation

- [ ] Assign each creator a project-local ID and verify the identity key: platform plus normalized profile URL.
- [ ] Keep creator contact details, account references, invoices, contracts, and payment data outside the public repository.
- [ ] Define one deliverable record per distinct deliverable, including every agreed publication platform.
- [ ] Convert late-stage activity into ordered JSON events using `tests/fixtures/late_stage_sandbox.json` only as a synthetic structural example.
- [ ] Use local evidence references in dry runs. Do not copy real invoice files, payment evidence, or credentials into fixtures.

## Dry Run And Review

- [ ] Run the sandbox locally:

  ```bash
  python3 scripts/run_late_stage_sandbox.py \
    --fixture /absolute/path/to/private-late-stage-events.json \
    --output /absolute/path/to/private-sandbox-report.json
  ```

- [ ] Confirm the report says `sandbox_mode: true`, `external_calls_made: []`, and `all_outbound_drafts_pending: true`.
- [ ] Review rate recommendations, workflow exceptions, missing publication links, invoice errors, and payment gates.
- [ ] Review every generated message as a draft. The sandbox does not approve or send it.
- [ ] Require payment evidence before treating a creator as paid or complete.
- [ ] Run `python3 -m unittest discover -s tests -v` before changing policies or event handling.

## Live-System Boundary

The sandbox does not read or write Gmail, Feishu, payment systems, creator platforms, or account-credit systems. Moving an approved draft into a live system is a separate, explicitly authorized operator action outside this executor.
