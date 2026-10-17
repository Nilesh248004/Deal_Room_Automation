# Deal Room Automation: implementation plan

Source: *DEAL-ROOM AUTOMATION: End-to-End Hotel eProposal Generation & Delivery* (technical planning report v1.0).
This document is the repository-backed assessment the report asks for in Phase 0 (§3.3), the gap analysis against it, and the scope of each phase.

**Principle carried over from the report:** orchestrate the existing deal-room system; don't replace it. Automate the standard path, isolate exceptions, and never bypass a critical check to raise the automation rate.

## 1. What the repository already is

A dependency-free Node 20+ orchestrator (`src/`) that runs a Python pipeline (`agent/`) one stage at a time, with files per job under `data/jobs/<id>/`. There is no database, no queue service and no UI framework, and none was added.

```
intake → capture → convert → facts → copy → build → review ─┬─► [approve] → load → verify (auto-rollback)
                                                            └─► overrides → build → review
```

The "Hippo deal room" is the Hippo Video editor template (`template_type 143`). The agent reads it with `GET /api/video_page/templates/:id/editor` and writes it with `PATCH /api/video_page/pages` (`agent/stages.py`, class `Hippo`). The existing renderer is Hippo's own editor/preview, and the existing template builder is `agent/dealroom_builder.py`. Both are reused as-is.

## 2. Capability matrix (PDF §3.2 / §3.3)

| Capability | Class | Current implementation | Evidence | Action |
|---|---|---|---|---|
| Deal-room templates | Existing | One 13-section Hippo template (Overview … Next Steps), stable section IDs, brand palette + WCAG checks | `agent/dealroom_builder.py` `build()`, `SECTION_IDS`, `validate()` | Reuse. **Extend** with a section plan so sections can be included, omitted and ordered per hotel |
| Merge fields | Existing | 116 Email/Cvent fields captured from Hippo's editor; the builder asserts every token is in the schema; Email→Cvent conversion | `agent/hippo_fields.py`, `dealroom_builder.mf()`, `stages.to_cvent()` | Reuse. **Build** a mapping report (source, confidence, required/optional, fallback) |
| Hotel profiles | Build | Only per-job `config.json` / `facts.json`. Nothing persists across jobs, no versions, no approval state | `agent/stages.py` `stage_facts` | **Build** versioned profile store with field-level evidence |
| Website extraction | Existing | Headless Chromium runs the same scraper as the browser snippet; cookie banner, scroll, same-site sub-pages, JSON-LD | `agent/scrape_hotel.js`, `stages.stage_capture` | Reuse. **Extend**: robots.txt, redirect validation, private-network blocking, bounded retry |
| Source verification | Extend | OTA deny-list, https-only, optional `ALLOWED_HOTEL_DOMAINS`, tracking-param strip | `stages.stage_intake` | **Extend**: credentials in URL, literal private IPs, odd ports, redirect target checks |
| Normalization / provenance | Extend | Facts carry `source_url` + quote; copy is grounded against the hotel's own text | `stages.stage_facts`, `ungrounded()` | **Extend**: status per field (extracted / normalized / inferred / manually corrected / verified / approved) and confidence |
| Validation | Extend | Structural checks in `validate()` (tokens, ids, contrast, map URL); review gate on high-severity warnings | `dealroom_builder.validate`, `stages.stage_review` | **Extend**: profile validation levels 1–3, plan validation (level 4), QA outcome (level 5) |
| Customization | Build | None: every hotel gets all 13 sections in one order | `dealroom_builder.build` | **Build** template families, deterministic rule engine, branding tokens, per-hotel saved choices |
| Asset management | Extend | Photos are scraped URLs placed as media merge fields. No upload API, no hashing, no rights status | `dealroom_builder.image_asset`, README "Not done yet" | **Extend**: reachability / type / size / dimension checks and content hashes in QA. Upload stays **blocked** on Hippo |
| PDF generation | Not present | The template has a dynamic "RFP Response PDF" document slot only | `dealroom_builder` (Proposal PDF section) | **Blocked**: needs a Hippo export API |
| Share links | Not present | None | n/a | **Blocked**: needs Hippo share-link API |
| Email / CRM delivery | Not present | Only an outbound status webhook (`WEBHOOK_URL`) | `src/server.js` `webhook()` | **Blocked**: no verified Hippo/CRM contract. Delivery is reported as `not_configured`, never as sent |
| Background jobs | Existing | In-process queue, per-job files, restart recovery, stage timeouts, cancel | `src/jobs.js` | Reuse. **Extend**: bounded retry with backoff, resume from failed stage |
| Exception queue | Extend | `job.error` string and `review.json` `high_severity` list | `src/jobs.js`, `stages.stage_review` | **Build** structured exception records, list/resolve API, retry |
| Idempotency | Build | Every `POST /deal-rooms` makes a new job | `src/jobs.js` `create()` | **Build** idempotency key |
| AuthN / AuthZ | Existing | API keys (timing-safe), named callers, allow-listed artifacts, strict job-id pattern | `src/server.js` | Reuse |
| Audit / logs | Extend | `run.log`, `job.history` | `src/jobs.js` | **Extend**: append-only `audit.jsonl`, attempt counts, failure reasons |
| Monitoring | Build | `/health` only | `src/server.js` | **Build** `/metrics` |
| Tests | Existing | Unit checks + e2e against a mock hotel site, mock Hippo, mock LLM | `test/` | Reuse. Extend for each new capability and the PDF's cases A–H |
| Deployment | Existing | Dockerfile (Playwright image + Node 22) | `Dockerfile` | Reuse |
| Cvent RFP reader | Existing, unwired | Parses a Cvent RFP and says which sections it needs. Not called by the pipeline; imports `pypdf`, which is not in `requirements.txt` | `agent/rfp.py` | Out of scope here. The customization input accepts its `needs()` output (`rfp_needs`) |

## 3. Decisions

1. **Stay on the existing stack.** Node orchestrator + Python stages + files. The PDF says not to finalise technology before the assessment, and the assessment found nothing that needs a database or broker at pilot scale. Hotel profiles are versioned JSON under `data/hotels/<hotel_id>/`, written atomically, matching the job-folder convention.
2. **A section plan, not a second template engine.** `dealroom_builder.build()` keeps rendering sections exactly as before. A deterministic plan decides which sections it renders and in what order (§4.3: orchestrate, don't duplicate). With no plan the builder behaves as it did.
3. **Hotel-owned facts are baked in; event and commercial values stay Hippo merge fields.** Rates, dates and concessions are never produced by this system (§7.3, §8.3). The mapping report lists them as "supplied by Hippo at send time".
4. **Inferred never masquerades as verified (§5.2).** Each profile field carries a status. Generated copy is `inferred`; an override is `manually_corrected`; only an explicit approval promotes a profile.
5. **Failures are data.** Every failed stage writes a structured error (`code`, `category`, `retryable`, `missing_fields`). Transient ones retry with bounded exponential backoff; the rest open an exception.

## 4. Pipeline after this change

```
intake ─ capture ─ convert ─ facts ─ copy ─ profile ─ customize ─ map ─ build ─ qa ─ review ─┬─► [approve] ─ load ─ verify
 source    robots,  scrape→   source   sourced  versioned  family +    mapping   plan-      PASS /   gate   │
 checks    SSRF,    config    +quote   copy     profile,   section     report,   driven     REVIEW /        │
           retry                                 L1–L3     plan        blocking  render     FAIL            └─► overrides → profile → … → review
                                                 validation                                    │
                                                                                          fail / FAIL → exception queue → retry from failed stage
```

Job states are not renamed (API compatibility). The report's states map as follows:

| PDF state | Here |
|---|---|
| RECEIVED | `queued` |
| SOURCE_VERIFICATION … RENDER_QA | the stage names (`intake` … `qa`), `status: running` |
| PUBLICATION_READY / REVIEW_REQUIRED | `awaiting_approval` (with the QA outcome and review items) |
| DELIVERY / COMPLETED | `load`, `verify`, then `succeeded` |
| REVIEW_REQUIRED (blocking) / FAILED | `failed` plus an open exception |

## 5. Phase scope

| Phase (PDF §18) | This change | Deferred / blocked |
|---|---|---|
| 0 Discovery | This document | Hippo editor API report (`inspect_editor_api.js`) for upload / share / PDF endpoints, needs a logged-in session |
| 1 Hotel profile | Versioned store, field evidence, validation levels 1–3, per-hotel saved customization | Manual field approval UI beyond the existing overrides box |
| 2 Extraction | robots.txt, SSRF and redirect checks, retry | Extractors for chains other than the supported layouts (README item 4) |
| 3 Mapping and templates | Mapping report, five template families as presets, rule engine, branding tokens, plan-driven build | New visual variants (only "where there is a demonstrated need") |
| 4 QA and exceptions | QA outcome, asset checks, exception queue, retry, idempotency | Real-browser render of the saved Hippo page (needs a Hippo preview URL the agent can open) |
| 5 Delivery | Delivery reported honestly as `not_configured` | PDF, share link, email/CRM, open/view tracking: **blocked on Hippo APIs** |
| 6 Pilot | n/a | 10–20 real hotels with a real Hippo account; targets in PDF §2.3 are for the pilot to measure |
| 7 Optimisation | `/metrics` gives the raw numbers | Dashboards, alert thresholds (set during the pilot) |

## 6. Release acceptance (PDF §17.4) after this change

| Criterion | Status |
|---|---|
| Source verification path implemented | Done |
| Required hotel data stored with provenance | Done |
| Actual merge-field definitions used | Done (already; now reported) |
| Required fields cannot silently pass when invalid | Done (profile validation + mapping + QA) |
| Custom section rules deterministic and tested | Done |
| Existing deal-room functionality preserved | Done. With no plan the builder emits byte-identical templates to before (checked against the original builder), and the original unit and e2e tests pass. One e2e test changed: the `mode=auto` test now supplies approved asset rights, because auto mode is deliberately stricter (see §7) |
| Critical rendering failures block publication | Partly: structural QA blocks; a real Hippo render check is deferred |
| Exception handling and retry work | Done |
| Permissions tested | Done for the API; Hippo-side permissions unchanged |
| Delivery outcomes recorded | Recorded as `not_configured`; real delivery blocked |
| End-to-end tests for supported templates | Done against the mock hotel site and mock Hippo |
| Pilot outputs reviewed | Not started: needs the pilot |

## 7. Behaviour that changed on purpose

| Change | Why | Escape hatch |
|---|---|---|
| `mode=auto` stops until the hotel's brand-asset rights are approved by an admin | Public images are not automatically free to republish (§15). "Automatic sending only for explicitly authorised workflows" (§14.3) | `ASSET_RIGHTS_POLICY=warn` |
| `robots.txt` is enforced | §6.3 "respect robots policies" | `ROBOTS_POLICY=warn` / upload a browser scrape |
| URLs with credentials, private addresses and odd ports are rejected | §6.3 SSRF rules | none (`ALLOW_PRIVATE_HOSTS` exists for tests only) |
| Unusable photos are dropped; the job stops only if none is usable | §10.2 asset validation, Case E | `ASSET_CHECK_POLICY=warn/off` |
| Optional prose intros left out instead of failing the build when empty | §8.3 "optional field: omit the block", no fabricated placeholders | n/a |
| API keys can carry a role (`name:key:role`); a key with no role is `operator`, which is everything a key could do before | §15 least privilege | n/a |
