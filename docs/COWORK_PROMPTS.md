# Cowork prompts for CageOps

Cowork handles the research, spreadsheet, and writing work around the build. Claude Code handles the code.

---

## 1. Data source audit (before Phase 1)

```
I'm building CageOps, a portfolio project: a production-style ML platform that predicts UFC fight outcomes. The point is the infrastructure (scraping pipeline, job queue, model serving, Kubernetes), not the betting angle.

Research and write a document called "CageOps Data Sources" covering:
1. ufcstats.com — current robots.txt contents, any terms of use, which page types exist (event list, event detail, fight detail, fighter profile) and what fields each has.
2. The main public Kaggle UFC datasets — for each: what it covers, date range, when it was last updated, license, and whether it includes per-round stats, betting odds, and rankings. Recommend which to use as seed data and why.
3. ESPN's public MMA endpoints — what's available and the risks of relying on an unofficial API.
4. Publicly available MMA news RSS feeds — URLs, how often they update, and any terms that affect using them for retrieval.
5. Legal/ethical risks and exactly what my README disclaimer should say.

Cite sources for every claim. Format it as markdown I can save as docs/DATA_SOURCES.md.
```

---

## 2. Hosting and cost spreadsheet (before Phase 2)

```
Build a spreadsheet for hosting CageOps, a small Kubernetes-based ML project: a FastAPI service, Python workers, Postgres with pgvector, Redis, and Prometheus + Grafana, running on k3s.

Sheet 1 — Hosting options: a DigitalOcean droplet (4GB and 8GB) running k3s, DigitalOcean managed Kubernetes, Hetzner Cloud, AWS Lightsail. Columns: monthly cost, vCPU, RAM, storage, bandwidth, pros, cons.
Sheet 2 — GPU rental for a few hours of vLLM benchmarking with a ~7-8B model (24GB-class GPU): RunPod, Lambda, DigitalOcean GPU droplets, and anything comparable. Hourly cost and minimum billing.
Sheet 3 — Monthly budget: hosting, object storage for backups, Anthropic API usage for ~500 short writeups/month with claude-haiku-4-5, domain, plus a one-time GPU line.
Also check whether the GitHub Student Developer Pack currently includes credits for any of these providers.

Use current published prices and cite where each came from.
```

---

## 3. README + blog post (Phase 8)

```
Attached are docs/DECISIONS.md, docs/BENCHMARKS.md, docs/RUNBOOK.md, docs/INFERENCE.md, and the current README from my CageOps repo.

Draft two things:
(a) A README that leads with: one-line description, live links (site, API docs, public Grafana), the architecture diagram, and 4-6 "engineering highlights" with real numbers. Then key design decisions, then how to run it locally. Honest about model performance vs. the betting-favorite baseline.
(b) A 1,200-1,800 word blog post: "What I learned building a production ML platform for UFC predictions." First person, casual but technical, focused on tradeoffs and what went wrong, not a feature tour.

Every number must come from BENCHMARKS.md — flag anything you can't verify instead of guessing.
```

---

## 4. Resume bullets + interview prep (Phase 8)

```
Using the CageOps docs attached, create an interview prep document:
1. Three resume bullet options for this project — infra-focused, quantified, short enough for one or two lines in the Jake's Resume LaTeX template.
2. A 60-second spoken pitch for the project.
3. 25 questions a platform / ML-infrastructure interviewer would likely ask about it (system design, failure modes, scaling, "why X instead of Y"), each with a model answer grounded in my actual decisions from DECISIONS.md.
4. The 5 questions I'd struggle with most based on gaps in the docs, and what I'd need to learn or build to answer them.
```
