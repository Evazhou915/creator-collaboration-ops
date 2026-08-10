# Release Checklist

Use this checklist before creating or pushing a public remote repository.

## Repository Hygiene

- [ ] Public tree is separate from the internal workspace.
- [ ] No creator data, real email addresses, message bodies, contracts, invoices, payment evidence, or product secrets are present.
- [ ] No API credentials, OAuth tokens, manifests, scan state, or generated reports are tracked.
- [ ] Example configuration uses placeholders only.
- [ ] Git history has been scanned, not only the current working tree.

## Behavior

- [ ] First outreach remains batch-approval gated.
- [ ] Negotiation and price commitments remain per-creator approval gated.
- [ ] DM actions remain manual.
- [ ] Gmail scanning is read-only unless an explicitly documented narrow exception applies.
- [ ] Script approval is required before video review.
- [ ] All agreed publication links are required before payment collection.
- [ ] Invoice mismatches and duplicate numbers stop payment preparation.
- [ ] Actual payment requires recorded evidence.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py tests/test_public_workflow.py
python3 -m json.tool config/project-config.example.json >/dev/null
python3 -m json.tool config/project-context.example.json >/dev/null
git diff --check
```

Then run a repository-wide secret scan and inspect `git log --all --stat` before the first push.

## Publishing

- [ ] Choose repository owner and public name.
- [ ] Choose a public license and confirm it matches `LICENSE`.
- [ ] Review README installation and limitations.
- [ ] Create the remote only after the checklist is complete.
- [ ] Push only the reviewed public branch.
- [ ] Enable branch protection and CI after the first push.
