---
name: interrogate
description: Use for "interrogate", "adversarial review", "multi-model review", "challenge this", "stress test this code", "find blind spots", or "tear this apart". Independent subagent reviewers challenge changes from different angles; you synthesize one verdict. Does not auto-apply changes.
---

# Interrogate

Spawn independent reviewers to adversarially review code changes. The adversarial signal comes from independent perspectives, not assigned personas. The deliverable is a synthesized verdict. Do NOT auto-apply changes.

## Step 1, Scope

Identify what to review from context:

- If the caller points at files or a diff, use that.
- If on a feature branch, run `git diff main...HEAD` (or the appropriate base branch) for the full changeset.
- If the message references recent work, gather the relevant files.

Package the diff or file contents with the surrounding context a reviewer needs to follow the code.

## Step 2, Intent

Before spawning reviewers, state the intent in one clear paragraph from the message, commit messages, any PR description, and the code. If the intent is unclear, ask before proceeding.

## Step 3, Spawn reviewers

Launch all reviewers concurrently in one message using the `subagent` tool. Use at least two, and vary the agent or model so the perspectives differ. Default pair: `code-reviewer` and `general` (use `cto` for architecture-heavy changes, `security-engineer` when the diff touches auth, input handling, or secrets). Each reviewer is read-only: it reports findings and changes nothing.

Give every reviewer the same prompt, containing:

- The stated intent.
- The diff or file contents and context.
- The rubric: correctness against the intent on real paths; edge cases and error handling; security (injection, authorization, secrets, untrusted input at boundaries); data loss or migration risk; concurrency and shared state; readability (layers to trace, hidden state); tests (behavior vs implementation); and anything removable.

## Step 4, Synthesize

As results return, build a unified picture. Merge findings that describe the same issue differently and note which reviewers raised each. Findings two or more reviewers raised independently are the highest signal. Read a lone reviewer's finding, but weight it accordingly. Note disagreements: one reviewer flagging what another explicitly clears is context for the verdict.

## Step 5, Lead judgment

You are the lead reviewer, a pragmatic senior engineer, not a neutral aggregator. Put every finding in one of four buckets, with the reviewer(s) that raised it and a one-line rationale.

- **Act on**. Real issues affecting correctness, security, or maintainability given the actual goals. They would block a real change.
- **Consider**. Legitimate, but may not outweigh the cost of addressing them now. Worth the caller's attention.
- **Noted**. Technically valid but not actionable: context-dependent, premature optimization, or low-impact at this stage.
- **Dismissed**. Wrong, nitpicky, or missing context, with a brief reason.

## Output format

### Intent
> [The stated intent paragraph from Step 2]

### Reviewers
- Reviewer [label]: [agent/model], [N findings] (one bullet per reviewer)

### Act On
[Each: description, which reviewers raised it, why it matters.]

### Consider
[Each: description, which reviewers raised it, the tradeoff.]

### Noted
[Brief list.]

### Dismissed
[Each with a brief rationale.]

### Agreement Map
[Where reviewers agreed and diverged, and what that pattern tells you.]
