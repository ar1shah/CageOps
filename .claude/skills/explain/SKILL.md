---
name: explain
description: Teach Ari what was just built in CageOps so he can defend it in a technical interview, then quiz him. Use after finishing a task, or when Ari types /explain (optionally followed by a topic).
---

Ari is new to most of this stack and will be interviewed on this project. Your job here is to teach, not to summarize.

1. Explain what we just built (or the topic Ari named) in plain language, using one concrete analogy. Say why it exists in the system — what would go wrong without it.
2. Walk the request or data path through it step by step, pointing to the actual files and functions in the repo.
3. Name the key design decision, the alternative we didn't pick, and the tradeoff. Link the matching entry in docs/DECISIONS.md, or add one if it's missing.
4. Describe one realistic way this breaks in production and how the system handles it (or doesn't yet).
5. Ask Ari 3 interview-style questions about it, **one at a time**. Wait for his answer before continuing. Grade honestly: what was right, what was missing, and what a strong answer sounds like. Do not reveal the answer before he tries.
6. If he struggles with a concept, give him one small hands-on exercise to do himself — break something on purpose, change a config and observe, trace a specific request through the logs. Hands-on beats more reading.
