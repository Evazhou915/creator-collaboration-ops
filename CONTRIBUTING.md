# Contributing

Thanks for helping improve this workflow.

## Before Opening A Pull Request

1. Keep changes generic and provider-safe. Do not add real creator records, email content, invoices, contracts, payment evidence, API tokens, or private campaign details.
2. Add or update synthetic tests for behavior changes.
3. Run the local checks from the README.
4. Explain any new automation and identify the human approval gate it preserves.
5. Keep external-message sending disabled in tests and examples.

## Scope

This repository provides workflow primitives and reference scripts. Contributions that automate unsolicited DMs, account switching, platform evasion, payment execution, or silent price commitments are out of scope.

## Pull Request Checklist

- [ ] No private data or credentials are included.
- [ ] Tests cover the changed behavior and relevant failure path.
- [ ] Documentation reflects any new configuration or state transition.
- [ ] External sends and financial actions remain approval-gated.
- [ ] `git diff --check` passes.
