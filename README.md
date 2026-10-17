# Deal Room Agent

Give it a hotel's official web page and a Hippo deal room page ID. It reads the hotel's site, keeps a **versioned profile of that hotel with the source of every fact**, plans which sections this hotel's deal room should carry, and builds an **Email** or **Cvent** deal room in the hotel's colours, fonts, logo and photos, with every merge field placed by code. It checks the result (quality assurance: PASS / REVIEW / FAIL), then stops for a person to review it. Once approved, it loads the deal room into Hippo (or hands you a loader file), reads the page back to check it, and rolls back automatically if the check fails.

No paid services are needed. It uses Node 20+ (no npm packages), Python 3.10+ and Playwright's headless Chromium. AI is optional, and its output is thrown away unless it can be traced to the hotel's own text.

The design follows `DEAL-ROOM AUTOMATION` (the technical planning report). What the repository already did, what this adds and what is still blocked are in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

```
POST /deal-rooms ─► intake ─► capture ─► convert ─► facts ─► copy ─► profile ─► plan ─► assets ─► build ─► map ─► qa ─► review ─┬─► [approve] ─► publish_gate ─► load ─► verify ─► done
                    URL +      robots,    colours,   every    sourced  versioned   family +  every     plan-    mapping  PASS/   review   │                            backup    read back,
                    safety     SSRF,      fonts,     fact has copy     hotel       sections  photo     driven   report   REVIEW/ page,    │                            + PATCH   auto-rollback
                    checks     retries    logos,     a source          profile,    + omitted fetched    render            FAIL    high-    └─► overrides ─► profile ─► … ─► review (again)
                                          photos                       L1-L3       + unmet   once                                  severity
                                                                       validation  requests                                       gate        failures ─► exception queue ─► retry from the failed stage
```

## Quick start (local)

```bash
pip install -r requirements.txt && python3 -m playwright install chromium
cp .env.example .env            # set API_KEYS=ops:<long random key>
node src/server.js              # http://127.0.0.1:8790  (built-in review UI at /)
```

Or skip the server and use the command line:

```bash
node src/cli.js run --url "https://www.tajhotels.com/en-in/hotels/jai-mahal-palace-jaipur" --page 1605252
# … prints each stage, then "AWAITING_APPROVAL" with the path of review.html
node src/cli.js approve <job id>
```

Save a hotel's scrape into the project's `hotel_scraps/` folder (one file per hotel, named by its id, for example `hotel_scraps/dalpchf-hilton-dallas-park-cities-cfeb285c.json`):

```bash
node src/cli.js scrape --url "https://www.tajhotels.com/en-in/hotels/jai-mahal-palace-jaipur"
node src/cli.js run --url "https://www.tajhotels.com/en-in/hotels/jai-mahal-palace-jaipur" --page 1605252 --scrape hotel_scraps/<hotel id>.json
```

It runs the same safety checks and headless capture as `run` (robots.txt, private addresses, retries on temporary errors) and saves only the scrape. Scraping a hotel again replaces its file. For a site that blocks servers (Hilton, for example), scrape the page in your own browser (`agent/scrape_hotel.js`, see the top of the file), then save the downloaded file into the folder under its clean name:

```bash
node src/cli.js scrape --url "https://www.hilton.com/en/hotels/dalpchf-hilton-dallas-park-cities/" --from latest     # newest hotel-scrape-*.json in ~/Downloads
node src/cli.js scrape --url "<hotel page>" --from "~/Downloads/hotel-scrape-….json"                                  # or name the file
```

The file is checked first: it must be a scrape, made on the same site as the URL. The folder is in `.gitignore`; set `HOTEL_SCRAPS_DIR` to keep scrapes somewhere else, and `DOWNLOADS_DIR` if your browser saves downloads somewhere other than `~/Downloads`.

A hotel-specific run, using the same plan the report describes (an event-focused proposal, meeting space first, no tour video):

```bash
echo '{"focus": "event", "exclude_sections": ["Concessions"], "include_tour_videos": false}' > event.json
node src/cli.js run --url "https://www.tajhotels.com/en-in/hotels/jai-mahal-palace-jaipur" --page 1605252 --customization event.json --opportunity OPP-1042
```

## What each stage does

| Stage | Output | Stops the job when |
|---|---|---|
| intake | input.json | The URL isn't https, has a user name or password in it, points at a private or reserved address (`localhost`, `10.x`, `169.254.169.254` …) or a non-standard port, is a booking or travel site (booking.com, expedia and so on), is outside `ALLOWED_HOTEL_DOMAINS`, the page ID isn't digits, or the customization is invalid. Tracking parameters are removed. The hotel gets a stable `hotel_id`. |
| capture | scrape.json, capture/page.png | `robots.txt` disallows the page (`ROBOTS_POLICY`), the site redirects to a different host, the page refuses automated access (401/403: never bypassed), or returns an error. Network failures, 429 and 5xx are retried with backoff first. Headless Chromium accepts the cookie banner, scrolls the whole page and runs `agent/scrape_hotel.js` (the same scraper as the browser snippet). Its own requests to private networks, and to pages `robots.txt` disallows, are blocked. |
| convert | config.json | No usable hotel photos are found. It works out colours by role, font stacks, the header and parent-brand logos, classified photos, and venue and room tables. Only the hotel's own pages are used, not chain-wide ones. |
| facts | facts.json | Never. Each fact is saved with the page it came from and the quote it was taken from. Facts without a source are flagged for review. |
| copy | copy.json | Never. Copy is built only from facts, FAQ answers in structured data and page sentences. The optional AI rewrite is rejected if it adds any number or name that isn't in the hotel's text. A block with nothing sourced behind it (a dining intro, parking, nearby) is **left out and listed in `left_out`**, never filled with filler wording. |
| profile | customization.json, profile.json, profile_validation.json | A required field (name, official URL, address, city) is missing, or the captured page is from a different host than `hotel_url`. Saves the hotel profile as a **new version only if its content changed**. |
| plan | plan.json | The customization asks for something this hotel's data can't support (for example the meeting-space family with no published venue capacities), or a section can't be populated and `on_unavailable` is `block`. |
| assets | assets.json | Every photo is fetched once and checked (reachable, really an image, size, dimensions, sha256). Unusable and duplicate photos are dropped. Stops only when **no** photo can be verified. |
| build | template.json, report.json, loader.js | Any merge field is invalid for the chosen flow, any colour pair fails WCAG AA, IDs are duplicated, a card is unfilled, a map URL is bad, or text from another hotel is left over. Only the planned sections are rendered, in plan order. |
| map | mapping.json | A required hotel value has no value, or a merge field isn't in the flow's schema. |
| qa | qa.json | QA outcome is **FAIL**: see below. |
| review | review.html, review.json | `mode=review` always waits. `mode=auto` waits if there are high-severity items (no logo, fewer than 4 photos, facts without a source, copy that isn't grounded, an unconfirmed source, unmet customization requests, **unverified brand-asset rights**, a QA REVIEW …). |
| publish_gate | publication.json | Re-checks that QA hasn't failed, the profile is valid and the template matches the plan. Then marks the hotel profile version **approved** and records which job generated what. |
| load | backup.json, load.json | Hippo rejects the save. A backup is always taken first. Logos already uploaded to the page are kept. |
| verify | verify.json | The page read back from Hippo is missing sections, components or merge fields. With `AUTO_ROLLBACK=1` (the default) the backup is restored. |

## Making a deal room hotel-specific

Three layers, applied in this order (later wins): the hotel's **saved customization**, the **request's** `customization`, and a reviewer's `overrides.customization`. The result is validated, hashed (`customization_version`) and recorded on every job. The plan it produces is deterministic: the same hotel data and customization always give the same sections in the same order, and `plan.json` explains each decision.

| Field | Meaning |
|---|---|
| `template_family` | `standard` (the reference proposal), `premium` (gallery first), `corporate_event`, `resort_leisure`, `meeting_space` (venue table leads; needs published venue capacities). Presets over the **same renderer**, not new templates. |
| `focus` | `event` → corporate_event, `meetings` → meeting_space (falls back to corporate_event without venue data), `leisure` → resort_leisure, `rooms` → standard. Ignored when `template_family` is given. |
| `preferred_sections` | Moved up, in this order, right after the Overview. Next Steps stays last. Accepts the section names and common aliases ("Suites", "Meetings", "F&B" …). |
| `exclude_sections` | Left out. The Overview and Next Steps can't be excluded. |
| `include_tour_videos` / `include_gallery` | `false` removes the video slot / the gallery photos (both off removes the section). `include_tour_videos: true` with no approved video asset is reported as an unmet request, never faked. |
| `include_testimonials` | Reported as unmet: testimonials are never extracted or invented. |
| `emphasize_rooms` | Rooms (by exact name) listed first in the room table. Rooms are never merged or renamed. |
| `cta_buttons` | A subset and order of `schedule_site_visit`, `request_revision`, `download_pdf`. |
| `branding` | `primary`, `accent`, `dark`, `tint`, `text` (hex) and `heading_font`, `body_font` (a supported web font). Only supported design tokens are accepted, and WCAG contrast is still enforced. |
| `on_unavailable` | `omit` (default): a section that exists only to show hotel content (Why This Hotel Fits, Location) and has none is left out and the reason recorded. `block`: that becomes an exception instead. |
| `rfp_needs` | The output of `agent/rfp.py`'s `needs()`: sections the RFP doesn't ask for are switched off. |
| `asset_rights` | `{approved: true, note}`: the hotel's logo and photos are cleared for use. **Admin key only**; who and when are recorded. Without it, `mode=auto` never publishes on its own. |
| `special_notes` | Free text for the reviewer. Shown on the review page, **never interpreted**: a guess must not change a proposal. |

Unknown fields, unknown section names, bad colours and unsupported fonts are rejected with a 400 before a job starts.

Make a customization the hotel's default with `PUT /hotels/:id/customization`, or send `"save_customization": true` with the job. From then on `mode=auto` can run unattended for that hotel.

### Hotel profile and provenance

`data/hotels/<hotel_id>/` holds `profile.json` (latest), `versions/vN.json` (every version), `index.json`, `customization.json`, `generations.json` and `audit.jsonl`. Every value in a profile has evidence: source URL, extraction method, confidence and a **provenance**:

| Provenance | Meaning |
|---|---|
| `officially_verified` | Found in the official page's text or structured data (the quote is in `facts.json`) |
| `extracted` | Taken from the site, exact location not confirmed (lower confidence) |
| `normalized` | Derived from extracted values (venue table, amenity list) |
| `inferred` | Generated wording (rules or AI). Passed the grounding check but is **never presented as a verified fact** |
| `manually_corrected` | A reviewer's override |

Approval is separate: nothing is "approved for proposal use" until a person approves the job (or `mode=auto` is authorised for the hotel). The profile holds the hotel's own data: the scrape, generated copy, a reviewer's corrections and the hotel's **saved** customization. A one-off per-request choice (branding, asset rights) is applied to that deal room only and never changes the stored profile. A table row a reviewer added or changed is `manually_corrected` with no page behind it. A rejected profile (for example a scrape from another host) is shown in its job but never stored. Re-running an unchanged hotel keeps its version; changed content makes a new one, and approving an older version never un-approves a newer one. A generation is recorded only after the page was saved and read back (or handed over as a loader). `GET /hotels/:id` shows `regeneration_required` when a deal room was generated from an older version than the one now approved. It never regenerates by itself.

Validation has three levels (structure, plausibility, source). A required field never passes just because it isn't empty: a capture from another host than `hotel_url` is critical, an unconfirmed official source and low-confidence required fields need a person.

### Quality assurance

`qa.json` ends in **PASS**, **REVIEW** (a person must decide; `mode=auto` stops) or **FAIL** (publication blocked, job fails with an exception).

* FAIL: critical profile errors, mapping errors, template not matching the plan, a malformed or unknown merge field, a blank block, an image in the deal room that wasn't verified, an unsafe link (only `https:`, `mailto:` and merge-field links pass), script, frame or event-handler markup in any text, a CTA that doesn't go where the plan says, another hotel's name in the text.
* REVIEW: unconfirmed official source, low-confidence required field, conflicting or implausible values, unmet customization requests (including a focus the hotel's data can't support), **prices in hotel-owned text** in any common form (`€250`, `INR 8,000 a night`, `Rs. 5,000`; rates come from an approved opportunity, never from the hotel's page), unverified brand-asset rights, a photo swapped in for a missing one or dropped over a temporary error, wide tables.
* **Not checked, and reported as such:** how Hippo renders the saved page on a phone and a desktop, PDF export and share links. The agent has no Hippo preview URL or export API to open.

Hotel facts are written into the template; event and commercial values (`${Guest Room Rate}`, `${Event Dates}` …) stay Hippo merge fields that Hippo fills per opportunity. `mapping.json` lists every one, classified as commercial / event / contact / proposal, with who supplies it.

### Failures, retries and exceptions

Every failed stage records a code, a category (the report's exception categories), whether it is retryable and which fields are missing (`stage_error.json`). Transient failures (network errors, 429, 5xx, a stage timeout on a network stage) are **retried with exponential backoff** (`RETRY_MAX_ATTEMPTS`, `RETRY_BASE_MS`, `RETRY_MAX_MS`, honouring `Retry-After`). Permanent ones aren't. When retries run out, or the failure is permanent, the job fails and an **exception** opens:

* `GET /exceptions`, `/exceptions/:id`: category, severity, failed stage, error code, missing fields, source references, retry eligibility, assigned reviewer, resolution notes and status, timestamps.
* `POST /exceptions/:id/assign` · `/resolve` · `/retry`, and `POST /deal-rooms/:id/overrides` to correct data. The job **resumes from the failed stage**; a failed load never rebuilds a valid proposal. An exception closes itself when its stage later passes.
* In `mode=auto` a stop for review also opens an exception (automation couldn't proceed safely). In `mode=review` a person looks at every job anyway, so the items are on the review page and the queue stays for real failures.
* Set `WEBHOOK_URL` to be told about each status change and each new exception.

Identical requests don't create duplicates: the same request returns the existing job (`200`, `"deduplicated": true`) while it is active or for `IDEMPOTENCY_WINDOW_HOURS` after it succeeded (`"force": true` runs again). `Idempotency-Key` works the usual way (same key + same request returns the same job; same key + different request is a 422). Only one job at a time may write a given Hippo page.

## API

Every endpoint except `/health` and `/` needs `x-api-key: <key>` or `Authorization: Bearer <key>`. Keys have a role: `API_KEYS=ops:key1,bot:key2:viewer,lead:key3:admin`. **viewer** reads. **operator** (the default for a key with no role, and everything a key could do before roles existed) creates, approves, edits, retries, cancels and rolls back. **admin** can also approve brand-asset rights.

| Method | Path | Body / notes |
|---|---|---|
| POST | `/deal-rooms` | `{hotel_url, hippo_page_id?, flow: email\|cvent, mode: review\|auto, load: manual\|api, opportunity_id?, customization?, save_customization?, idempotency_key?, force?, overrides?, scrape?}` → `202 {id}` (new) or `200 {id, deduplicated: true}` |
| GET | `/deal-rooms` · `/deal-rooms/:id` | Job list · stages (with attempts), QA outcome, hotel and profile version, error, history |
| GET | `/deal-rooms/:id/logs?from=<byte>` | Plain-text log (the `x-log-size` header gives the next offset) |
| GET | `/deal-rooms/:id/audit` | Who did what, when (job events, stage outcomes, retries, approvals, exceptions) |
| GET | `/deal-rooms/:id/artifacts[/name]` | review.html, template.json, loader.js, plan.json, qa.json, mapping.json, assets.json, profile.json, profile_validation.json, customization.json, publication.json, report.json, facts.json, copy.json, config.json, scrape.json, backup.json, verify.json, page.png, run.log, result.json, audit.jsonl |
| POST | `/deal-rooms/:id/overrides` | JSON merged into the config, for example `{"overview":"…","slots":{"gallery":["exterior_1","rooms_1"]},"customization":{"include_gallery":false}}`, then rebuilt and reviewed again |
| POST | `/deal-rooms/:id/approve` | `{notes?}` Runs the publish gate, load and verify |
| POST | `/deal-rooms/:id/retry` | Resume a failed job from the stage that failed |
| POST | `/deal-rooms/:id/rollback` · `/cancel` | Restores the backup · stops a job |
| GET | `/hotels` · `/hotels/:id` | Stored hotels · profile versions, saved customization, generations, `regeneration_required` |
| GET | `/hotels/:id/profile[?version=N]` · `/versions` · `/audit` | A profile · all versions · the hotel's audit trail |
| PUT | `/hotels/:id/customization` | Replace the hotel's saved customization (validated) |
| GET/POST | `/exceptions` … | See above |
| GET | `/metrics` | Queue depth, per-stage success and latency, failure codes, open exceptions and resolution time, automation rate (jobs completed without manual intervention), duplicates and retries |

`load=manual` (the default) doesn't need Hippo credentials. After approval, download `loader.js` and paste it into the Console on the deal room **editor** tab. It takes a backup, asks for confirmation, keeps uploaded logos and defines `__hvRestore()`.
`load=api` needs `HIPPO_API_TOKEN` (a service token, preferred) or, as a stopgap, `HIPPO_COOKIE` and `HIPPO_CSRF`.

### Behaviour that differs from earlier versions

* `mode=auto` now stops when the hotel's logo and photos have no approved usage rights (an admin sets `asset_rights` once per hotel). Set `ASSET_RIGHTS_POLICY=warn` to get the old behaviour back, with a warning.
* `robots.txt` is respected (`ROBOTS_POLICY=enforce`). For a site that disallows automation, run the browser scraper yourself and upload the file as `scrape`, or set `ROBOTS_POLICY=warn` if you have permission.
* A section whose hotel-owned prose has nothing sourced behind it is now left out of the block rather than failing the build (intros are optional).

## Photos in Hippo

A deal room built from a hotel's website shows **no photos in Hippo until they are uploaded there**: each photo slot is a media merge field ("Hotel Fits Image 1") and Hippo has nothing to show for it. Until Hippo gives us an upload API, the flow is:

```bash
node src/cli.js photos <job id>                     # saves every photo the deal room uses, named by photo key (exterior_1.jpg, rooms_2.png ...), plus manifest.json
```

1. In the Hippo deal-room editor, upload those files into a carousel and click **Save**. Keep the file names: they are how the photos are matched.
2. In the same editor tab run `agent/browser_snippets/harvest_media.js`. It downloads `hippo-media-<page>.json`.
3. ```bash
   node src/cli.js media <job id> ~/Downloads/hippo-media-<page>.json
   ```
   Each uploaded image is matched to its photo by file name and the deal room is rebuilt, checked and held for approval again. Photos you haven't uploaded stay merge fields; the command lists them. An uploaded image whose name isn't a photo key is ignored and reported. Then approve and load as usual.

The hero banner uses the first photo, so upload that one first. The API takes the same overrides: `POST /deal-rooms/:id/overrides` with `{"media": {"exterior_1": {"hippo": {"asset_id": 123, "token": "..."}}}}`.

## Inspecting the Hippo editor API

To create a machine-readable API/structure report, open a disposable Hippo deal-room **editor** page while logged in and run `agent/browser_snippets/inspect_editor_api.js` in DevTools Console. The inspector reads the editor template and passively observes same-origin `/api/` requests; it does not initiate saves or edits. Test one action at a time and label it before performing it, for example:

```js
__hvApiInspector.mark("add image component")
```

When done, run `__hvApiInspector.download()` to download `hippo-editor-api-report.json`; run `__hvApiInspector.stop()` to restore the page's fetch/XHR hooks. The report contains request/response shapes, paths, methods, status codes, field names, component types, and merge-field tokens. It omits request/response values and headers, redacts page IDs, and records only query parameter names. Do not test save, delete, or upload actions on a live production page; use a disposable copy. Review the report before sharing it.

**Sites that block servers:** some sites block headless browsers. In that case, run `agent/scrape_hotel.js` in your own browser on the hotel page, save the downloaded file with `node src/cli.js scrape --url <hotel page> --from latest`, and pass it as `--scrape hotel_scraps/<hotel id>.json` (in the UI, use the "scrape file" field). The capture stage is then skipped.

## goose and goose-task-server

- `recipes/deal_room.yaml` is a goose recipe that runs the CLI with fixed arguments. goose reports the result but never writes copy or merge fields.
- `integrations/goose-task-server/tasks.deal-room.example.json` has example allowlist entries (build, approve, recipe). Rename the fields to match your `tasks.json`. Exit code `10` means "waiting for approval".

## Configuration

See `.env.example`. The ones that matter for hotel-specific automation:

| Variable | Default | Meaning |
|---|---|---|
| `API_KEYS` | none | `name:key[:role]`, comma separated |
| `ROBOTS_POLICY` | `enforce` | `enforce`, `warn` or `off` |
| `ASSET_CHECK_POLICY` | `enforce` | `enforce` drops unusable photos; `warn` records problems only (CDNs that refuse servers); `off` skips the check and QA then asks for review |
| `ASSET_RIGHTS_POLICY` | `require` | `require` or `warn` |
| `ASSET_CACHE_HOURS` | `24` | Reuse a verified photo's result this long instead of downloading it again |
| `RETRY_MAX_ATTEMPTS` · `RETRY_BASE_MS` · `RETRY_MAX_MS` | `3` · `2000` · `60000` | Transient-failure retries |
| `MAX_MANUAL_RETRIES` | `5` | `POST /retry` limit per job |
| `IDEMPOTENCY_WINDOW_HOURS` | `24` | How long a succeeded job answers an identical request |
| `RETENTION_DAYS` | `0` (keep) | Remove finished jobs (never active ones, ones with an open exception, or the hotel store) older than this; `node src/cli.js purge --days N --dry-run` |
| `ALLOW_PRIVATE_HOSTS` | unset | **Tests only.** Turns the private-network checks off |

## Deploy

```bash
docker build -t deal-room-agent .
docker run -d -p 8790:8790 -v dealroom-data:/data --env-file .env deal-room-agent
```

Put it behind your company reverse proxy (HTTPS). Keep `HOST=127.0.0.1` when it runs on a shared host without a container. The data volume holds jobs, the hotel store and the exception queue: back it up.

## Tests

```bash
npm test                         # unit checks (stages, engine) + end-to-end API tests (mock hotel site + mock Hippo + mock LLM)
npm run test:unit                # only the fast unit checks
python3 test/test_loader.py data/jobs/<job>   # loader.js in Chromium against a mocked hippovideo.io
python3 test/test_ui.py 8790                  # review UI smoke test (server + mock site running)
```

The end-to-end tests cover the following:
- Authentication, roles, and the rule that only admins approve asset rights.
- Input checks, rejecting travel-site URLs, URLs with credentials, and `robots.txt`-disallowed sites.
- Real headless capture, including the cookie banner, FAQ structured data and sources for every fact.
- The report's cases: A standard hotel, B premium with a tour video, C event-focused, D missing required information (blocked, exception names the fields, a correction resumes it), E broken images, F/G transient Hippo failures (retry with backoff; an exhausted load resumes without rebuilding or duplicating), H repeated requests.
- Profile versioning, approval, saved per-hotel customization, `regeneration_required`.
- Exceptions (assign, resolve, retry, auto-close), the audit trail, metrics, retention.
- The review gate, overrides, approve, save and verify, rollback, a bad save caught and rolled back automatically, the Cvent flow, rejecting AI copy that isn't grounded, missing credentials, cancel and restart recovery.

## Safety design

- Merge fields, colours, IDs and the save format come from code (`agent/dealroom_builder.py`, `agent/hippo_fields.py`). AI never places them. Free-text notes never change a plan.
- Every outbound request the pipeline makes itself (robots.txt, photo checks) validates the URL and every redirect hop, refuses private and reserved addresses, and has a total deadline. Chromium's requests are **filtered** the same way (public hosts only, http(s) on standard ports, an unresolvable name is refused, service workers blocked), but Chromium resolves names itself, so this is a filter and not a guarantee against a hostile DNS server: **also deny private ranges at the network edge** (container egress policy) in production. Redirects to another host fail the job.
- Python stages are started with an argument list, never through a shell. They only receive the environment variables they need, and this server's API keys are never passed on.
- Only allowlisted files can be downloaded. Job IDs and hotel IDs are checked against a strict pattern. `review.html` is served with a sandboxing Content Security Policy and carries its own no-script policy.
- A hotel's name or any other text from its website is never pasted into code: `loader.js` carries it as one escaped data literal, venue-table cells and the review page are HTML-escaped, and QA fails any raw script, frame, event-handler or `javascript:` markup. A brand colour that isn't `#RRGGBB` is refused.
- Request bodies drop `__proto__` keys, the merge that applies overrides ignores `__proto__`/`constructor`/`prototype`, and a malformed request target gets a 400 instead of taking the process down.
- Each stage gets only the credentials it needs (the browser that opens hotel pages never sees the Hippo token). The server refuses to start with a short or placeholder API key.
- Nothing is sent to Hippo before approval, or before the high-severity gate and QA pass in auto mode. Every save is preceded by a backup and followed by a read-back check.
- Hotel-owned text can't carry prices, another hotel's name fails QA, and unverified brand-asset rights stop `mode=auto`.
- Logs and the audit trail hold ids, stage names and outcomes only: no secrets and no recipient details.

## Known limits

* The unauthenticated `GET /deal-rooms/:id/media/:key` route (it lets the sandboxed review page show photos) is protected only by the job id. It serves raster images only, streams with a hard size cap and a locked-down response, but anyone who learns a job id can use it as a limited image fetcher. Put the API behind your network controls.
* A job left in `awaiting_approval` holds its Hippo page until it is approved or cancelled; there is no timeout.
* Only the first venue-table layout (Taj/IHCL) is parsed. For chain-wide country paths the scraper's own-pages filter is still the chain's country prefix, although the hotel's **identity** uses its own page.
* The hotel store uses file locks inside the Python stages; the API writes `customization.json` atomically but without that lock (last writer wins).

## Not done yet (needs Hippo engineering)

1. **Delivery.** PDF export, share-link creation and checks, email/CRM sending and open/view tracking (report §14) need Hippo or CRM APIs that aren't verified. A job's `result.json` says `delivery: not_configured`; nothing is ever reported as sent.
2. **Rendered-page QA.** Checking the saved page on mobile and desktop needs a Hippo preview URL the agent can open.
3. **Image and logo upload API.** Until a photo is uploaded to Hippo its slot is a media merge field with nothing behind it, so Hippo shows a placeholder (the review page shows the scraped photo, Hippo doesn't). There is no upload API we can call yet, so uploading is a manual step: see [Photos in Hippo](#photos-in-hippo). Hippo also only shows logos that were uploaded, so upload the hotel logo once on the page; later loads keep it.
4. **Hippo service token** for `load=api`. A session cookie works but expires.
5. **Fonts:** confirm that Hippo's deal room renders Google Fonts.
6. **Venue and room tables:** the Taj/IHCL page layout is supported. Other chains get the facts but not the tables, so the `meeting_space` family isn't available for them.
7. **The pilot** (10–20 real hotels, measured against the report's targets) hasn't been run.

## Layout

```
src/server.js  API      src/jobs.js  queue, retries, idempotency, metrics    src/cli.js  CLI     src/ui.html  review UI
src/exceptions.js  exception queue   src/hotels.js  hotel store view        src/audit.js  audit trail
agent/stages.py         pipeline stages                    agent/scrape_hotel.js  scraper (server + browser)
agent/scrape_to_config.py  scrape → config                 agent/dealroom_builder.py  config + plan → Hippo template + loader
agent/hotel_profile.py  hotel identity, versioned store, provenance, validation      agent/customize.py  customization schema, template families, section plan
agent/assets.py  photo validation, dedupe, cache           agent/mapping.py  merge-field mapping report       agent/qa.py  PASS / REVIEW / FAIL
agent/netsafe.py  SSRF guard, safe fetch, robots.txt       agent/common.py  shared helpers                    agent/rfp.py  Cvent RFP reader (not wired in)
agent/hippo_fields.py   Email/Cvent merge fields           agent/browser_snippets/  restore, harvest media, find changes
recipes/  goose recipe   integrations/  goose-task-server   docs/  implementation plan and capability matrix   test/  unit, e2e, loader, UI tests + fixtures
```
