---
name: phase-done
description: Check whether the current CageOps phase meets its definition of done before opening a pull request. Use when Ari says a phase is finished or types /phase-done.
---

1. Read the current phase's "Done when" checklist in docs/ROADMAP.md.
2. Verify every item with evidence — actually run the tests, lint, and the relevant commands; don't rely on memory of earlier runs. Report each item as PASS / FAIL with the evidence.
3. Also check:
   - docs/DECISIONS.md has entries for the choices made in this phase
   - every number claimed anywhere is in docs/BENCHMARKS.md with its command
   - .env.example covers every new environment variable
   - no secrets, tokens, or .env files in the diff against main
   - the README "Status" section reflects reality
4. If anything fails, list exactly what to fix and stop there.
5. If everything passes, draft the PR title and description (what changed, why, how to test, benchmark results). Ask Ari before running `gh pr create`.
6. Ask Ari which parts of this phase he couldn't explain cold in an interview, and offer to run /explain on them.
