# openhack-scanner — Augmentation Plan (2026-07-02)

**Decision:** augment the EXISTING production scanner (this repo) with the two differentiators proven in the
`Benchmarks/lean-harness` research — do NOT rebuild. Production already wins at scale (2/4 vs our harness's
1/4 on real CVEs); our job is to add the moat it lacks:
1. **Cross-service / cross-language attack-CHAIN reconstruction** (production does per-file single-vuln only).
2. **Honest, adversarial validation** (production self-validates by default — inflates precision).

Full evidence: `Benchmarks/lean-harness/LEARNINGS_2026-07-02.md`.

---

## Current pipeline (confirmed in `openhack/agents/coordinator.py`)
```
framework-detect + discover_attack_surface (deterministic, tools/coverage.py)
  -> Step 1 Recon        (recon.py, ReconAgent)            -> context["recon"]
  -> Step 2 Hunt swarm   (hunter_swarm.py)                 -> potential_findings
  -> Step 2.5 coverage 2nd pass / 2.25 feature deep-dive
  -> Step 3 Validate swarm (validator_swarm.py)            -> validated Finding objects
  -> sandbox_verifier_swarm / browser_verifier_swarm       (dynamic exploit proof)
```
Per-agent models are configurable: `settings.{recon,hunter,validator,feature_hunter}_model_id` (config.py).

---

## What we add, in priority order

### P0 — Turn on cross-model validation + port the hardened exploitability checks (small, high ROI)
**Why:** `validator_swarm.py:32` = `settings.validator_model_id or self.llm.model`. Unptset -> the validator
uses the SAME model as the hunter = self-validation, which we measured inflating precision (tie->confirmed +
model grading its own work). This is the cheapest credibility win in the whole plan.
**Do:**
1. Default `validator_model_id` to a DIFFERENT model than the hunter (e.g. hunter GLM-5.2 -> validator K2.5).
   Add a startup check that warns if they're equal.
2. Port the hardened validator SYSTEM prompt from `lean-harness/validator.py` — the mandatory exploitability
   checks that killed our recurring false positives:
   - CSRF: confirm only if the auth cookie is NOT SameSite=Lax/Strict (read the cookie-set code).
   - XXE: confirm only if the parser resolves EXTERNAL entities (xml2js/sax do not -> false_positive).
   - XSS: confirm only if input reaches a raw-HTML sink (JSX text is auto-escaped -> false_positive).
   - Framework default config (Nuxt devtools, etc.) -> false_positive; blank-rationale finding -> false_positive.
   - `false_positive` = MITIGATED/not-present, NOT merely low-severity (low-but-real -> confirmed low).
3. Use STRICT majority (>=2-of-3), not tie->confirmed.
**Files:** `openhack/agents/validator_swarm.py`, `openhack/agents/validator.py`, `openhack/config.py`.
**Test:** re-run the 8-repo benchmark; precision should hold/rise, recall unchanged.

### P1 — Chain composition stage (the flagship differentiator)
**Why:** on a polyglot fintech app (Vaultwise) the isolated "Critical SSRF" buried in 192 findings is what a
human dismisses, while the CHAIN it enables (SSRF -> internal token -> mint money, crossing TS->Go) is the
breach. Production reports the bricks; this reports the ladder. Semgrep/CodeQL can't do it (per-language,
per-repo). We reconstructed 6/7 Vaultwise chains, code-verified, crossing TS/Go/Python.
**Do:** add **Step 4: Chain composition** to the coordinator, after validation, porting
`lean-harness/chain_tracer.py`:
1. **Topology** — detect services (docker-compose / k8s) or monolith. Reuse `discover_attack_surface` output
   for endpoints; add outbound-call + trust extraction.
2. **Endpoint/call graph** — services connect by NETWORK CALLS, not imports. Per service (ANY language),
   LLM-extract {exposes+auth+auth-bypass, outbound_calls+headers, trusts, secret_exposure}; join
   outbound-target -> exposing-service via topology.
3. **Directed archetype hunters** — one agentic hunter per impact class (money / RCE / stored-XSS / race /
   auth-bypass / secret-theft / offline-crack) that FOLLOWS the chain through real code and verifies each hop
   (drops unverified hops; honestly reports "not reachable"). Key lesson: one blind pass converges — direct it.
4. Emit **Chain objects** (ordered hops, services traversed, per-hop evidence) alongside single-vuln Findings.
**Files:** new `openhack/agents/chain_tracer.py` (port + adapt); wire a Step 4 into `coordinator.py`; new
`Chain` result type in `session.py`.
**Model:** GLM-5.2 for extraction (free), Opus 4.8 for the chain-following hunters (more robust on big graphs
— we measured 6/7 vs GLM-script 4/7). Make it configurable (`chain_model_id`).
**Test:** score vs the 7 Vaultwise ground-truth chains (`samples/ground-truths/vaultwise.md`).

### P2 — The adversarial verification panel (the moat / "proves its findings")
**Why:** this is the actual product wedge and the one thing that works at ANY scale. It reads the artifacts,
not the narrative — it caught our own gaming three times. Production has DYNAMIC verifiers (sandbox/browser);
this adds the STATIC adversarial panel that re-derives recall/precision and flags hallucination/gaming.
**Do:** offer as a post-scan **verification report** (independent panel re-checks a sample of findings +
tries to refute each) and/or a "verified" badge per finding. Port the `verify-*` workflow logic.
**Files:** new `openhack/agents/verification_panel.py` + a report renderer.
**Note:** lower urgency than P0/P1 for detection, but it's the marketing/trust differentiator ("AI-native SAST
that proves its findings"). Sequence after P0/P1 land.

---

## De-risking gate (do this BEFORE trusting any of it in prod)
Every number we have is from vuln-DENSE seeded apps. We have ZERO data on real, mostly-CLEAN production code,
where the false-positive rate — the thing that kills SAST adoption — actually gets decided.
**Gate:** run the augmented scanner on 1-2 REAL un-seeded repos, measure the FP rate + whether it finds real
bugs, before shipping P0/P1 defaults. If the FP rate on clean code is bad, tune the validator (P0) first.

---

## Sequencing
1. **P0** (cross-model default + hardened validator prompts) — days; immediate precision credibility.
2. **De-risking run** on real code — in parallel with P0.
3. **P1** (chain stage) — the flagship; port chain_tracer, wire Step 4, score vs Vaultwise GT.
4. **P2** (verification panel) — the trust wedge; after P0/P1 are solid.

## Non-goals / honest constraints
- Do NOT replace production's recon/hunters (they win at scale). Add, don't rebuild.
- Do NOT rebuild the pattern layer — stand on it / Semgrep for candidates.
- Chain tracing is n=1 (Vaultwise) — validate on a 2nd polyglot target when one with ground truth exists.
- Move the OpenRouter key to env; no hardcoded paths in ported code.
