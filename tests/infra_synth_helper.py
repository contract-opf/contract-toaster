#!/usr/bin/env python3
"""Shared explicit CDK context for infra structural-gate tests (issue #316).

Issue #316 removes the internal `githubOwner` / `alarmsEmail` / `appDomain`
CDK context defaults from `infra/lib/contract-toaster-stack.ts` (previously
`exos-legal` / `legal-eng@example.com` / `teamexos.com`) and makes
`cdk synth` fail closed — with a message naming the missing keys — when any
of them is absent.

Roughly two dozen existing infra structural-gate test files each run a
baseline `cdk synth --context env=dev` as their own setup step to exercise
unrelated stack behavior (IAM, KMS, S3, WAF, CI/CD wiring, ...) and were
relying on the now-removed defaults just to make that baseline synth
succeed. This module supplies ONE shared, neutral, non-tenant-identifying
set of `--context` args those tests splice into their existing synth
command so they keep synthesizing without re-litigating tenant identity
themselves.

NOT for production use — these are placeholder values for offline test
synths only, distinct from any real deploy context.
"""

NEUTRAL_CDK_CONTEXT: list[str] = [
    "--context", "githubOwner=example-org",
    "--context", "alarmsEmail=alarms@example.com",
    "--context", "appDomain=example.com",
]
