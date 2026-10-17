---
name: no-comments
description: Cut comments and workaround code from a scope, then land the smallest root-cause fix. Applies the comment-sicko rules. Use for "no comments", "kill the comments", "clean up the diff".
---

# No comments

Apply the **comment-sicko** skill to the scope, then act on its findings.

## Scope

Use the files or diff the caller names. Otherwise use the current diff against the base branch, default `main`, including the working tree.

## Steps

1. Load the **comment-sicko** skill and review the scoped files or diff against its keep-list.
2. Inspect the report. Reject application-code edits, scope escapes, deletion of exception-protected comments, misstated `MUST KILL` reasons, and flags that treat intentional code as guilty. A keep survives only with proof it is about something we cannot change. Audit missed scoped lint and TypeScript suppressions: correctness or safety suppressions stay actionable `MUST KILL`s. Restore a deletion only with an exact exception and scoped proof. If a kill is ambiguous, do not restore. If an exception claim is refuted or still ambiguous, delete it.
3. Fix trivial accepted flags directly by deleting a dead path, dropping a parameter, or using the real API. If a fix needs a shape, sketch the types and signatures first and stop at the sketch. Sketch the shape, do not implement it yet.
4. Implement the smallest root-cause fix in scope. Remove every named workaround. If the root cause is out of scope, land the smallest in-scope fix and report the rest open. The **principle-fix-root-causes** and **principle-redesign-from-first-principles** skills guide intent only. Neither authorizes widening the fence nor fixing instances outside it. Never bolt on symptom guards.
5. Constraint comments say `do not remove`, `do not change wording`, or `talk to X before changing`. Leave keeps about things we cannot change. Offer the cheapest in-scope type, runtime, test, or CI lint that encodes the constraint. Wait for approval. If approved, encode then delete. Otherwise delete, report the constraint open, and sketch out-of-scope work.
6. Report the deletion count, restored comments, fixes, encoding offers, encodings, unenforced constraints, and other open work.
