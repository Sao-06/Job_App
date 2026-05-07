# Landing-Page Redesign — Scroll-Driven Pipeline Demo

**Date:** 2026-05-06
**Branch:** beta
**Author:** Jonny + Claude (brainstorming session)

## Problem

The current `/` landing page is ~1600 lines of decorative React with eight content sections — Hero, Ticker, Stats, Resume Intelligence, Discovery, Providers, Pipeline, Reviews, CTA — none of which let a visitor *see* the product. Copy reads as AI-generated. Reviews are fabricated personas. Visitors who don't already know what an LLM resume scan is get nothing concrete before being asked to sign up.

## Goal

A visitor who lands on `/` should, within five seconds of scrolling, *see the product run* against a real resume — not read about it.

## Non-Goals

- No logo cloud, no fake testimonials.
- No A/B testing framework, no analytics beyond what already exists.
- No multi-language / no SEO content blocks.
- No interactive resume *upload* on the landing page (deferred — see "Future").

## Scope

Three sections survive: **Hero** → **Scrubber** → **Final CTA**. All other current sections are deleted.

The Scrubber is a `position: sticky` pinned region (~600 vh of scroll travel) where scroll progress drives a real, captured pipeline run frame-by-frame. The visitor scrubs back and forth through phases by scrolling.

Concurrently: the **`mode = 'demo'`** option is removed from the in-app `/app` Settings page. Free tier becomes Ollama-only; the `DemoProvider` class stays as the OllamaProvider's internal-fallback for malformed JSON, but is no longer user-selectable.

## Architecture

```
frontend/
  landing.html           # rewritten — single React page, ~600 lines instead of 1600
  landing/
    demo-run.json        # NEW — frozen real pipeline output, ~30 KB gzipped
    sample-resume.txt    # NEW — source resume the fixture was built from
tools/
  freeze_landing_demo.py # NEW — runs the actual heuristic scanner + curates jobs;
                         #       outputs demo-run.json. Re-run when scoring rules change.
docs/superpowers/specs/
  2026-05-06-landing-redesign-design.md
```

**Component split inside `landing.html`:**

| Component | Role |
|---|---|
| `<Nav>` | Brand bar + sign-in. Kept as-is. |
| `<Hero>` | One headline + one sub-line + primary CTA + ghost CTA. The cosmetic resume card on the right is deleted. |
| `<Scrubber>` | Pinned section. Owns scroll progress (0–1). Reads `demo-run.json`. Dispatches to seven phase sub-components, each a pure function of progress. |
| `<FinalCTA>` | Trimmed copy, same CTA buttons. |
| `<Footer>` | Kept. |

**No animation library.** Pure `IntersectionObserver` + `scroll` listener with `requestAnimationFrame` throttling. One `progress` value (0–1) → one `phaseIndex` (0–6) + intra-phase progress. Each sub-component renders deterministically from those two numbers.

## Scrubber Phase Choreography

| Scroll % | Phase | Visual moment |
|---|---|---|
| 0–14% | 1 — Resume scan | Sample resume PDF lands centred. Skill tokens (`Python`, `Verilog`, `CUDA`, …) lift off the page and cluster. Score climbs 0 → 88. |
| 14–28% | 2 — Discovery | 20 source chips light up in parallel. A counter ticks `0 → 47 jobs found`. Dedupe shimmer collapses to `42 unique`. |
| 28–42% | 3 — Scoring | Top-5 job cards stack with score bars filling left-to-right. NVIDIA ASIC Intern → 92, Apple HW EE → 87, etc. |
| 42–60% | 4 — Tailoring | One LaTeX bullet centre-stage. Before line fades out: *"Helped with research on RF amplifier prototypes."* After line types in: *"Engineered RF amplifier prototypes that improved SNR by 6 dB across 5 test devices."* Diff highlights show what changed. |
| 60–72% | 5 — Submission | Browser-frame mock auto-fills a Greenhouse application; `Submit` button gets a green check. |
| 72–86% | 6 — Tracker | Excel grid materialises row-by-row, status cells colour from grey → green/yellow. |
| 86–100% | 7 — Report | A markdown report types out: top-3 picks, manual queue, next steps. |

**Mobile fallback (≤720 px):** scroll-pinning is unreliable on iOS Safari with a dynamic URL bar. On narrow screens the Scrubber switches to **auto-play once when scrolled into view**, no pinning, no scrubbing. A "↻ Replay" button at the end lets the visitor re-watch.

## Hero Copy

```
Eyebrow:   01 / Career cockpit · session live
Headline:  Your job search, on autopilot.
Sub-line:  Drop a resume. We scan it, score 20 sources against your profile,
           and tailor every application — automatically.
CTA:       [ Try it free → ]   [ I have an account ]
Reassure:  No credit card · Demo mode works without keys
```

(The sub-line is the *only* prose paragraph on the entire landing page after this redesign. Everything else is shown by running.)

## Final CTA

Single line + two buttons. No three-column "What you get" grid.

```
Stop reformatting your résumé at 2 a.m.
[ Create your free account → ]   Already have one? Sign in
```

## Fixture build (`tools/freeze_landing_demo.py`)

Single CLI:

```
python tools/freeze_landing_demo.py --output frontend/landing/demo-run.json
```

The script:

1. Reads `frontend/landing/sample-resume.txt` (a synthetic but realistic resume committed alongside).
2. Calls `pipeline.providers.DemoProvider().extract_profile(...)` — the **actual heuristic scanner**, not a fake. The output is real extracted skills/education/experience.
3. Loads a curated list of 20 real job titles from major engineering employers (committed to the script as a constant — no live scraping).
4. Calls `DemoProvider().score_job(...)` against each — real keyword-coverage scoring.
5. Hand-authors the *tailoring* bullet (Phase 4) and *report* markdown (Phase 7) since those require LLM calls; the resume scan + scoring are the load-bearing "real" data.
6. Writes the resulting `demo-run.json` to disk.

The fixture is committed. Regenerate when scoring rubric changes; CI does NOT regenerate (deterministic file).

## /app SPA cleanup

| Surface | Change |
|---|---|
| `frontend/app.jsx` `SettingsPage` mode picker | Drop the "Demo" radio. Two options remain: Ollama (free) and Anthropic (Pro). |
| `frontend/app.jsx` Plans page comparison | Remove "Demo" row from the free-tier feature list. |
| `app.py` `_default_state()` | Already defaults to `mode='ollama'`. Confirm no path sets `mode='demo'`. |
| `app.py` `update_config` whitelist | Tighten mode validation to `{'ollama','anthropic'}`. Server-side 400 if `mode='demo'` is posted. |
| `pipeline/providers.py` `DemoProvider` class | **Kept.** Still used as `OllamaProvider`'s internal fallback when LLM returns malformed JSON. No longer user-selectable. |

Existing user state with `mode='demo'` (rare; Jonny's account had this) gets transparently coerced to `'ollama'` on next `/api/state` load — one-line guard in `_load_session_state`.

## Testing

- **Unit:** `tools/freeze_landing_demo.py` is run once locally; its output is verified by manual inspection. No CI test for it.
- **Integration:** the existing `tests/integration/test_app_dev.py` covers the SPA mode-picker path; add one assertion that `POST /api/config {mode:'demo'}` now 400s.
- **Manual smoke (golden path):** `python app.py`, hit `/`, scroll. Phases play in order, no jank. Resize to 600 px wide — Scrubber switches to auto-play. `/app` Settings has only Ollama + Anthropic options.

## Risks

| Risk | Mitigation |
|---|---|
| Sticky-scroll feels janky on Safari iOS | Mobile fallback to auto-play (no pinning). Hard limit at 720 px. |
| Visitors who want a feature list bounce | The Scrubber *demonstrates* every feature in 7 visible moments. The CTA reassures with "No credit card · Demo mode works without keys". |
| Fixture goes stale vs. live pipeline | Re-run `freeze_landing_demo.py` each time the scoring rubric or extraction shape changes. Script is committed and reproducible. |
| Existing landing-page direct links to `#sources` / `#how` / `#reviews` anchors break | Add anchor IDs (`#scrubber-discovery`, `#scrubber-pipeline`) to scroll-progress milestones inside the new Scrubber so old fragments still scroll to roughly the right place. |

## Future (deferred)

- True interactive upload-and-scan on landing page (Approach B from brainstorming). Requires a guest auth path + rate-limited public endpoint + abuse handling. Not in this slice.
- A/B test of Scrubber vs. current page once analytics are wired.
