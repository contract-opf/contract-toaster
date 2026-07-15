<!--
Title format: feat: short summary  |  fix: short summary  |  chore: ...
Branch: phase-N/short-description
-->

## Issue

Closes #

## What changed

<!-- One or two sentences. What does this PR do? -->

## Why

<!-- One or two sentences. Why this approach? What did we consider and reject? -->

## How tested

<!-- Specific. What did you actually run, click, or check? Screenshots if UI. -->

- [ ]
- [ ]
- [ ]

## Risk

<!-- What could go wrong? What's the blast radius if this is wrong in production? -->

## Rollback plan

<!-- How do we undo this if we discover an issue post-merge? "git revert" is fine
     for app code; infrastructure changes and release-bundle changes need more thought. -->

## Legal-behavior impact

<!-- If this changes playbooks, prompts, standard forms, model policy, corpus snapshots,
     evaluation gates, redline behavior, retention, legal hold, or audit semantics, name
     the release-bundle fields/hashes and legal approval path affected. Otherwise write N/A. -->

## Checklist

- [ ] Linked to the issue this PR closes
- [ ] Tests added or updated where applicable
- [ ] Documentation updated (README / ARCHITECTURE / RUNBOOK / docs) where applicable
- [ ] `legal-review-required` label applied if this touches `playbooks/`, `prompts/`, model policy, standard forms, corpus governance, evaluation gates, retention/legal hold, audit semantics, or anything legal-facing
- [ ] If this PR adds a new legal-behavior file (playbook governance, output contract, audit semantics, gold fixtures, evaluation rules), confirm that `.github/CODEOWNERS` covers the new path under `@exos-legal/gc`
- [ ] Release-bundle / corpus / model-policy hashes and rollback/quarantine impact considered, or N/A
- [ ] No secrets in code, commit history, or test fixtures
- [ ] `cdk diff` reviewed for any infra changes
