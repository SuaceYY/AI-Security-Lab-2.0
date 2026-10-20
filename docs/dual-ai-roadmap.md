# Dual AI staged roadmap

## Stage A — secure GitHub handoff
Goal: use two independent OpenCode sessions. GLM plans a small task from this
versioned roadmap and creates a GitHub Issue; the existing Issue Claimer with
DeepSeek executes the issue, runs fixed tests, and opens a PR. GLM then reviews
the actual PR diff and CI evidence, posts one structured review, and waits for
human approval/merge.

Acceptance criteria:
- Existing Worker, its state and historical failures #2/#6/#9 remain untouched.
- No automatic merge, no unattended privileged edits, no arbitrary commands
  supplied by GitHub Issue text.
- Duplicate events and duplicate review comments are suppressed.
- Every AI call and tool event is visible with role and session.
- Missing evidence causes needs_evidence rather than success.

## Stage B — feedback and corrections
After Stage A is validated, map GLM changes_requested to a bounded DeepSeek
repair task on the same Issue/PR, with a maximum of three review rounds.
Never treat needs_evidence as a code defect; first collect and attach the
existing Worker's fixed-verification evidence.

## Stage C — next task after merge
Only after human merge, GLM checks roadmap progress, picks the next
nondependent task, and publishes a new Issue. Enforce a per-run task budget,
persistent idempotency and an operator stop control. Do not create tasks just
to keep the loop running.

## First small task candidate
Improve the user-facing documentation for the existing dual-AI coordinator:
explain current limitations, safe read-only checks and the manual human merge
gate. Only modify files under docs/. Provide explicit acceptance criteria and
a fixed, non-network verification command. Avoid changes to tools/issue_claimer.
