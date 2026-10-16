# Deal Room Agent

Give it a hotel's official web page and a Hippo deal room page ID. It reads the hotel's site and builds an **Email** or **Cvent** deal room in its colours, fonts, logo and photos, with every merge field placed by code. It then stops for a person to review it. Once approved, it loads the deal room into Hippo (or hands you a loader file), reads the page back to check it, and rolls back automatically if the check fails.

No paid services are needed. It uses Node 20+ (no npm packages), Python 3.10+ and Playwright's headless Chromium. AI is optional, and its output is thrown away unless it can be traced to the hotel's own text.

```
POST /deal-rooms ─► intake ─► capture ─► convert ─► facts ─► copy ─► build ─► review ─┬─► [approve] ─► load ─► verify ─► done
                    URL check  headless   colours,   every    sourced  Email/   review   │                backup   read back,
                    no OTAs    Chromium   fonts,     fact has copy     Cvent    page,    │                + PATCH  auto-rollback
                               + cookie   logos,     a source (+opt.   merge    high-    └─► overrides ─► build ─► review (again)
                               banner     photos,             AI, must fields   severity
                                          venues              be grounded)       gate
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

## What each stage does

| Stage | Output | Stops the job when |
|---|---|---|
| intake | input.json | The URL isn't https, is a booking or travel site (booking.com, expedia and so on), is outside `ALLOWED_HOTEL_DOMAINS`, or the page ID isn't digits. Tracking parameters are removed. |
| capture | scrape.json, capture/page.png | The page returns an error. Headless Chromium accepts the cookie banner, scrolls the whole page and runs `agent/scrape_hotel.js` (the same scraper as the browser snippet). |
| convert | config.json | No usable hotel photos are found. It works out colours by role, font stacks, the header and parent-brand logos, classified photos, and venue and room tables. Only the hotel's own pages are used, not chain-wide ones. |
| facts | facts.json | Never. Each fact is saved with the page it came from and the quote it was taken from. Facts without a source are flagged for review. |
| copy | copy.json | Never. Copy is built only from facts, FAQ answers in structured data and page sentences. The optional AI rewrite is rejected if it adds any number or name that isn't in the hotel's text. |
| build | template.json, loader.js, report.json | Any merge field is invalid for the chosen flow, any colour pair fails WCAG AA, IDs are duplicated, a card is unfilled, a map URL is bad, or text from another hotel is left over. |
| review | review.html, review.json | `mode=review` always waits. `mode=auto` waits only if there are high-severity items (no logo, fewer than 4 photos, facts without a source, copy that isn't grounded, scraper run on the wrong page). |
| load | backup.json, load.json | Hippo rejects the save. A backup is always taken first. Logos already uploaded to the page are kept. |
| verify | verify.json | The page read back from Hippo is missing sections, components or merge fields. With `AUTO_ROLLBACK=1` (the default) the backup is restored. |

## API

Every endpoint except `/health` and `/` needs `x-api-key: <key>` or `Authorization: Bearer <key>`.

| Method | Path | Body / notes |
|---|---|---|
| POST | `/deal-rooms` | `{hotel_url, hippo_page_id?, flow: email\|cvent, mode: review\|auto, load: manual\|api, overrides?, scrape?}` → `202 {id}` |
| GET | `/deal-rooms` · `/deal-rooms/:id` | Job list · stages, error, history |
| GET | `/deal-rooms/:id/logs?from=<byte>` | Plain-text log (the `x-log-size` header gives the next offset) |
| GET | `/deal-rooms/:id/artifacts[/name]` | review.html, template.json, loader.js, report.json, facts.json, copy.json, config.json, scrape.json, backup.json, verify.json, page.png, run.log, result.json |
| POST | `/deal-rooms/:id/overrides` | JSON merged into the config, for example `{"overview":"…","slots":{"gallery":["exterior_1","rooms_1"]}}`, then rebuilt and reviewed again |
| POST | `/deal-rooms/:id/approve` | Runs load and verify |
| POST | `/deal-rooms/:id/rollback` · `/cancel` | Restores the backup · stops a job |

`load=manual` (the default) doesn't need Hippo credentials. After approval, download `loader.js` and paste it into the Console on the deal room **editor** tab. It takes a backup, asks for confirmation, keeps uploaded logos and defines `__hvRestore()`.
`load=api` needs `HIPPO_API_TOKEN` (a service token, preferred) or, as a stopgap, `HIPPO_COOKIE` and `HIPPO_CSRF`.

## Inspecting the Hippo editor API

To create a machine-readable API/structure report, open a disposable Hippo deal-room **editor** page while logged in and run `agent/browser_snippets/inspect_editor_api.js` in DevTools Console. The inspector reads the editor template and passively observes same-origin `/api/` requests; it does not initiate saves or edits. Test one action at a time and label it before performing it, for example:

```js
__hvApiInspector.mark("add image component")
```

When done, run `__hvApiInspector.download()` to download `hippo-editor-api-report.json`; run `__hvApiInspector.stop()` to restore the page's fetch/XHR hooks. The report contains request/response shapes, paths, methods, status codes, field names, component types, and merge-field tokens. It omits request/response values and headers, redacts page IDs, and records only query parameter names. Do not test save, delete, or upload actions on a live production page; use a disposable copy. Review the report before sharing it.

**Sites that block servers:** some sites block headless browsers. In that case, run `agent/scrape_hotel.js` in your own browser on the hotel page and upload the downloaded file as `scrape` (in the UI, use the "scrape file" field). The capture stage is then skipped.

## goose and goose-task-server

- `recipes/deal_room.yaml` is a goose recipe that runs the CLI with fixed arguments. goose reports the result but never writes copy or merge fields.
- `integrations/goose-task-server/tasks.deal-room.example.json` has example allowlist entries (build, approve, recipe). Rename the fields to match your `tasks.json`. Exit code `10` means "waiting for approval".

## Deploy

```bash
docker build -t deal-room-agent .
docker run -d -p 8790:8790 -v dealroom-data:/data --env-file .env deal-room-agent
```

Put it behind your company reverse proxy (HTTPS). Keep `HOST=127.0.0.1` when it runs on a shared host without a container.

## Tests

```bash
npm test                         # 20 unit checks + 15 end-to-end API tests (mock hotel site + mock Hippo + mock LLM)
python3 test/test_loader.py data/jobs/<job>   # loader.js in Chromium against a mocked hippovideo.io
python3 test/test_ui.py 8790                  # review UI smoke test (server + mock site running)
```

The end-to-end tests cover the following:
- Authentication.
- Input checks.
- Rejecting travel-site URLs.
- Real headless capture, including the cookie banner.
- FAQ structured data.
- Sources for every fact.
- The review gate.
- Overrides.
- Approve, save and verify.
- Rollback.
- A bad save being caught and rolled back automatically.
- The Cvent flow.
- Rejecting AI copy that isn't grounded.
- Missing credentials.
- Cancel.

## Safety design

- Merge fields, colours, IDs and the save format come from code (`agent/dealroom_builder.py`, `agent/hippo_fields.py`). AI never places them.
- Python stages are started with an argument list, never through a shell. They only receive the environment variables they need, and this server's API keys are never passed on.
- Only allowlisted files can be downloaded. Job IDs are checked against a strict pattern. `review.html` is served with a sandboxing Content Security Policy.
- Nothing is sent to Hippo before approval, or before the high-severity gate passes in auto mode. Every save is preceded by a backup and followed by a read-back check.

## Not done yet (needs Hippo engineering)

1. **Image and logo upload API.** Photos go in as media merge fields, and the review page shows the scraped photo for each slot. Hippo only shows logos that were uploaded, so upload the hotel logo once on the page; later loads keep it.
2. **Hippo service token** for `load=api`. A session cookie works but expires.
3. **Fonts:** confirm that Hippo's deal room renders Google Fonts.
4. **Venue and room tables:** the Taj/IHCL page layout is supported. Other chains get the facts but not the tables.

## Layout

The generated section flow is modeled on the supplied proposal PDF: overview and contact summary, hotel fit, guest rooms, concessions, meeting space, pricing, AV and destination details, property media, and next steps. Event-specific values remain Hippo merge fields (for example, `${Event Name}` and `${Total Attendees}`); the sample PDF's event values are never copied into a hotel template. Hippo fills those fields from the parsed event response when available.

```
src/server.js  API      src/jobs.js  queue + job store     src/cli.js  CLI     src/ui.html  review UI
agent/stages.py         pipeline stages                    agent/scrape_hotel.js  scraper (server + browser)
agent/scrape_to_config.py  scrape → config                 agent/dealroom_builder.py  config → Hippo template + loader
agent/hippo_fields.py   Email/Cvent merge fields           agent/browser_snippets/  restore, harvest media, find changes
recipes/  goose recipe   integrations/  goose-task-server  test/  unit, e2e, loader, UI tests + fixtures
```
