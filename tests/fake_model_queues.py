#!/usr/bin/env python3
"""
Seed a `FakeBedrockClient` response queue per model id, safely when two
roles share a model (issue #148).

`model_client.FakeBedrockClient` keys its canned responses by `model_id` and
pops them in call order. A suite that wrote `{primary_id: [...], critic_id:
[...]}` silently lost the primary's responses the moment the two ids became
the same string -- which is exactly what issue #148 made the policy default
(one model on both passes, so the critic reads the reviewer's prompt cache):
the later key overwrote the earlier one, and the primary pass then consumed
the critic's answer.

`seeded_by_model` merges the lists for a repeated id IN THE ORDER GIVEN, so
`seeded_by_model((primary_id, [p]), (critic_id, [c]))` yields `{id: [p, c]}`
when the ids match -- the primary pass runs first and takes `p`, the critic
takes `c` -- and the ordinary two-key mapping when they do not. No other
behaviour of the fake changes.

This module holds no tests of its own; suites import it.
"""

from __future__ import annotations


def seeded_by_model(*pairs: tuple[str, list[str]]) -> dict[str, list[str]]:
    """`{model_id: responses}` with a repeated model id's lists concatenated
    in argument order (see the module docstring)."""
    queues: dict[str, list[str]] = {}
    for model_id, responses in pairs:
        queues.setdefault(model_id, []).extend(responses)
    return queues
