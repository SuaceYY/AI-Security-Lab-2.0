# Dual AI GitHub loop — bootstrap (Issue #13)

This is **stage 1 only**: read-only model discovery and validation. It does **not** implement autonomous GLM↔DeepSeek orchestration, create Issues, run OpenCode sessions, or merge PRs.

## Desired workflow
1. GLM reads the versioned project roadmap and GitHub state, then proposes a bounded Issue with acceptance criteria.
2. GitHub Issue with `ai-ready` is claimed by existing DeepSeek/OpenCode worker.
3. DeepSeek works in its **own session**, runs fixed verification, and creates a PR.
4. GLM is triggered by GitHub state changes and independently reviews diff, checks and scope in its **own session**.
5. GLM posts review feedback on GitHub. If rejected, DeepSeek fixes the same task. If accepted, wait for **human merge**; dependent work must not begin until merged.
6. After human merge, GLM publishes the next eligible Issue. Continue until roadmap goal or bounded stop condition.

## Windows read-only preflight
Run after checking out this branch, with the current Worker undisturbed:

```powershell
python tools\dual_ai\preflight.py --list-models
python tools\dual_ai\preflight.py --glm "<exact GLM model ID>" --deepseek "<exact DeepSeek model ID>"
python -m unittest discover -s tools\dual_ai -p "test_*.py" -v
```

Model IDs are provider-qualified strings **copied from local OpenCode output**, not assumed aliases. The preflight fails closed if either model is missing or the IDs are identical. Distinct model IDs are necessary but **not sufficient** for independent sessions; explicit session creation, persisted exact IDs and separation tests belong to stage 2.

## Stage 2 acceptance
- Explicitly create two separate OpenCode sessions, persist session IDs by role, assert they differ, and prevent cross-role resume.
- Capture live stdout/tool events, tag them `GLM` / `DeepSeek` and display in the same Windows console with timestamps. No fabricated conversation.
- No background worker change or protected path change without a separately reviewed PR.

## Stage 3 acceptance
- GitHub-backed planner/reviewer/executor state machine, idempotent event processing, review based on actual diff and fixed verification evidence, and human-merge gate.
- Bounded review iterations, model budget/timeouts, error handling, credential redaction and operator stop.
- A real authorized Issue demonstrates at least two distinct model turns and GitHub feedback.
- Never retry historic failed Issues #2/#6/#9 automatically. Preserve local state.

**Cost note:** OpenCode itself may be free while selected model endpoints may have quotas, rate limits or fees. Confirm the actual provider offering before unattended use.
