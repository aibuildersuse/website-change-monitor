# Website Change Monitor

**Website Change Monitor** watches any list of web pages (or a CSS selector / JSON API response) and tells you exactly what text appeared or disappeared since the last run. Snapshots live in a named key-value store, so scheduled runs compare cleanly. Optional webhook alerts work with Slack, Discord, Zapier, Make and n8n.

> Looking for **SaaS pricing plan/price diffs**? Use [Pricing Page Change Monitor](https://apify.com/aibuildersuse/pricing-page-change-monitor) instead — it understands plan cards and price amounts. This Actor is the general-purpose page watcher.

## What it does

- **Watches any URL** — marketing pages, docs, ToS, blog posts, competitor landing pages, public JSON APIs.
- **Optional CSS selector** so you only diff the region that matters (`main`, `#content`, `.terms`).
- **JSON mode** flattens API responses into `path=value` lines for stable diffs.
- **Auto rendering** — plain HTTP first; headless Chromium only when the page looks empty or JS-gated.
- **Ignore noisy lines** with regexes (timestamps, view counters, copyright years).
- **Webhook** with Slack/Discord-compatible `text`/`content` fields.

## Use cases

- Terms of service / privacy policy change alerts.
- Competitor landing-page or feature-page monitoring.
- Docs and help-center watch for product teams.
- Lightweight JSON API change detection.
- Compliance / brand mention watches on public pages.

## How to use

1. Add the URLs. Optionally add a CSS selector (global or per URL).
2. Run once to save a **baseline**.
3. Schedule (hourly / daily). Later runs output `content_changed` rows with added/removed lines.
4. Optional: add a webhook URL.

## Input

| Field | Description |
| --- | --- |
| `urls` | Pages to watch. Items may include `selector` and `label`. |
| `cssSelector` | Default CSS selector for all pages. |
| `contentMode` | `text` (default) or `json`. |
| `renderMode` | `auto` / `http` / `browser`. |
| `ignoreTextPatterns` | Regexes to ignore when diffing. |
| `maxDiffLines` | Cap added/removed lines in each row (default 40). |
| `includeUnchanged` | Emit `no_change` rows (default on). |
| `webhookUrl`, `webhookHeaders` | Alert destination. |
| `stateStoreName` | Snapshot store (default `website-change-monitor-state`). |
| `maxConcurrency` | Parallel pages (1–10). |
| `proxyConfiguration` | Optional Apify Proxy. |

Example input:

```json
{
  "urls": [
    { "url": "https://example.com/" },
    { "url": "https://news.ycombinator.com/", "selector": "#hnmain" },
    { "url": "https://httpbin.org/json" }
  ],
  "contentMode": "text",
  "renderMode": "auto",
  "ignoreTextPatterns": ["©\\s*\\d{4}"],
  "webhookUrl": "https://hooks.slack.com/services/XXX/YYY/ZZZ"
}
```

## Output

```json
{
  "url": "https://example.com/",
  "checkedAt": "2026-11-06T15:10:00Z",
  "previousCheckedAt": "2026-11-05T15:10:00Z",
  "fetchMethod": "http",
  "changeType": "content_changed",
  "addedCount": 2,
  "removedCount": 1,
  "addedText": ["New headline here", "Updated CTA copy"],
  "removedText": ["Old headline"],
  "lineCount": 18,
  "summary": "Content changed (+2 / -1 lines)"
}
```

### Change types

| `changeType` | Meaning |
| --- | --- |
| `baseline` | First run: snapshot saved. |
| `content_changed` | Text/JSON content differs from the previous snapshot. |
| `no_change` | Checked, identical. |
| `empty_content` | Loaded but no extractable text. |
| `selector_not_found` / `fetch_error` | Selector or load failure. |

## Pricing

Pay per event: **$0.005 per page checked**. Keep default memory at 512 MB for healthy margins.

## Tips

- Scope with a CSS selector to cut noise from nav/footers.
- Use `ignoreTextPatterns` for clocks, A/B banners and counters.
- For heavy SPAs, set `renderMode` to `browser`.
- For SaaS pricing pages, prefer [Pricing Page Change Monitor](https://apify.com/aibuildersuse/pricing-page-change-monitor). For changelog/status feeds, prefer [SaaS Changelog & Status Page Watcher](https://apify.com/aibuildersuse/saas-changelog-status-watcher).
