"""Website Change Monitor.

Watches any URL (or CSS/JSON selector region) for content changes, diffs against the
previous run's snapshot, and outputs structured added/removed lines. Not specialized
for SaaS pricing cards — use Pricing Page Change Monitor for those.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from apify import Actor, Event

from .monitor_core import (
    BrowserFetcher,
    extract_page,
    fetch_page,
    flatten_json,
    line_diff,
    short_hash,
    try_parse_json,
)

PAGE_EVENT = 'page-checked'
SNAPSHOT_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def snapshot_key(url: str, selector: str | None, content_mode: str) -> str:
    return 'snap-' + hashlib.sha1(f'{url}\n{selector or ""}\n{content_mode}'.encode()).hexdigest()[:20]


def normalize_url_input(item: Any) -> tuple[str, str | None, str | None] | None:
    if isinstance(item, str):
        url, selector, label = item.strip(), None, None
    elif isinstance(item, dict):
        url = str(item.get('url') or '').strip()
        selector = item.get('selector') or item.get('cssSelector') or None
        user_data = item.get('userData') if isinstance(item.get('userData'), dict) else {}
        label = item.get('label') or user_data.get('label')
    else:
        return None
    if not re.match(r'^https?://', url, re.I):
        return None
    sel = selector.strip() if isinstance(selector, str) and selector.strip() else None
    return url, sel, label


def filter_lines(lines: list[str], ignore: list[re.Pattern[str]]) -> list[str]:
    return [ln for ln in lines if not any(p.search(ln) for p in ignore)]


def extract_content(fr: Any, selector: str | None, content_mode: str) -> dict[str, Any]:
    body = fr.body or ''
    mode = content_mode if content_mode != 'html_text' else 'text'
    if mode == 'json' or (mode == 'text' and fr.content_type and 'json' in fr.content_type):
        parsed = try_parse_json(body)
        if parsed is not None:
            lines = flatten_json(parsed)
            text = '\n'.join(lines)
            return {
                'title': None,
                'selectorMatched': True,
                'textLines': lines,
                'text': text[:200000],
                'textHash': short_hash(text),
                'lineCount': len(lines),
                'contentMode': 'json',
            }
        if content_mode == 'json':
            return {
                'title': None, 'selectorMatched': True, 'textLines': [], 'text': '',
                'textHash': short_hash(''), 'lineCount': 0, 'contentMode': 'json',
                'error': 'Response is not valid JSON',
            }
    ext = extract_page(body, selector=selector)
    ext['contentMode'] = 'text'
    return ext


async def send_webhook(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as c:
        last = None
        for attempt in range(3):
            try:
                r = await c.post(url, json=payload, headers={'Content-Type': 'application/json', **headers})
                last = {'status': r.status_code, 'ok': 200 <= r.status_code < 300}
                if last['ok']:
                    return last
            except httpx.HTTPError as e:
                last = {'status': None, 'ok': False, 'error': f'{type(e).__name__}: {e}'[:200]}
            await asyncio.sleep(2 * (attempt + 1))
        return last or {'ok': False}


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        raw_urls = inp.get('urls') or inp.get('startUrls') or []
        default_selector = (inp.get('cssSelector') or '').strip() or None
        content_mode = inp.get('contentMode') or 'text'
        render_mode = inp.get('renderMode') or 'auto'
        store_name = (inp.get('stateStoreName') or 'website-change-monitor-state').strip()
        include_unchanged = bool(inp.get('includeUnchanged', True))
        max_diff = max(5, min(int(inp.get('maxDiffLines') or 40), 200))
        webhook_url = (inp.get('webhookUrl') or '').strip() or None
        webhook_on_baseline = bool(inp.get('webhookOnBaseline', False))
        webhook_headers = inp.get('webhookHeaders') or {}
        max_concurrency = max(1, min(int(inp.get('maxConcurrency') or 3), 10))
        ignore: list[re.Pattern[str]] = []
        for pat in inp.get('ignoreTextPatterns') or []:
            try:
                ignore.append(re.compile(pat, re.I))
            except re.error:
                Actor.log.warning(f'Ignoring invalid regex: {pat!r}')

        targets: list[tuple[str, str | None, str | None]] = []
        seen: set[tuple[str, str | None]] = set()
        for item in raw_urls:
            norm = normalize_url_input(item)
            if norm is None:
                Actor.log.warning(f'Skipping invalid URL entry: {item!r}')
                continue
            url, sel, label = norm
            sel = sel or default_selector
            if (url, sel) in seen:
                continue
            seen.add((url, sel))
            targets.append((url, sel, label))
        if not targets:
            await Actor.fail(status_message='No valid URLs in input. Add at least one http(s) URL to "urls".')
            return
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,63}', store_name):
            await Actor.fail(status_message='stateStoreName may contain only letters, digits and hyphens.')
            return

        store = await Actor.open_key_value_store(name=store_name)
        proxy_url = None
        if inp.get('proxyConfiguration'):
            pc = await Actor.create_proxy_configuration(actor_proxy_input=inp['proxyConfiguration'])
            proxy_url = await pc.new_url() if pc else None

        memory_mb = int(os.environ.get('ACTOR_MEMORY_MBYTES') or 1024)
        browser_tabs = max(1, min(max_concurrency, memory_mb // 1024))
        browser = BrowserFetcher(max_pages=browser_tabs) if render_mode in ('auto', 'browser') else None
        aborting = False

        async def on_abort(*_: Any) -> None:
            nonlocal aborting
            aborting = True
            Actor.log.warning('Run is aborting; stopping after pages in progress.')

        Actor.on(Event.ABORTING, on_abort)

        sem = asyncio.Semaphore(max_concurrency)
        charging = Actor.get_charging_manager()
        in_flight = 0

        def budget_allows_another() -> bool:
            left = charging.calculate_max_event_charge_count_within_limit(PAGE_EVENT)
            return left is None or left - in_flight > 0

        all_changes: list[dict[str, Any]] = []
        summary = {
            'pagesRequested': len(targets), 'pagesChecked': 0, 'pagesFailed': 0,
            'pagesChanged': 0, 'baselines': 0, 'changes': 0,
        }
        budget_hit = False
        run_started = now_iso()

        async with httpx.AsyncClient(proxy=proxy_url, http2=False) as client:

            async def process(url: str, selector: str | None, label: str | None) -> None:
                nonlocal budget_hit, in_flight
                if aborting or budget_hit:
                    return
                async with sem:
                    if aborting or budget_hit:
                        return
                    if not budget_allows_another():
                        budget_hit = True
                        Actor.log.warning('Maximum cost per run reached; skipping remaining pages.')
                        return
                    in_flight += 1
                    try:
                        await check_page(url, selector, label)
                    finally:
                        in_flight -= 1

            async def check_page(url: str, selector: str | None, label: str | None) -> None:
                nonlocal budget_hit
                Actor.log.info(f'Checking {url}' + (f' (selector {selector})' if selector else ''))
                checked_at = now_iso()
                base = {
                    'url': url, 'label': label, 'selector': selector,
                    'contentMode': content_mode, 'checkedAt': checked_at,
                }
                try:
                    fr = await fetch_page(url, client=client, browser=browser, mode=render_mode)
                    if not fr.body:
                        raise RuntimeError(fr.error or 'empty response')
                    ext = extract_content(fr, selector, content_mode)
                    if ext.get('error'):
                        raise RuntimeError(ext['error'])
                except Exception as e:  # noqa: BLE001
                    fr, ext = None, None
                    err = f'{type(e).__name__}: {e}'[:300]
                else:
                    err = None

                key = snapshot_key(url, selector, content_mode)
                old = await store.get_value(key)
                rows: list[dict[str, Any]] = []
                if ext is None:
                    summary['pagesFailed'] += 1
                    rows.append({
                        **base, 'changeType': 'fetch_error', 'error': err,
                        'previousCheckedAt': (old or {}).get('checkedAt'),
                        'summary': f'Could not load page: {err}',
                    })
                    await Actor.push_data(rows)
                    return
                if selector and not ext.get('selectorMatched', True):
                    summary['pagesFailed'] += 1
                    rows.append({
                        **base, 'changeType': 'selector_not_found', 'pageTitle': ext.get('title'),
                        'fetchMethod': fr.method,
                        'summary': f'CSS selector {selector!r} matched nothing on the page',
                    })
                    await Actor.push_data(rows)
                    return

                lines = filter_lines(ext.get('textLines') or [], ignore)
                if not lines:
                    summary['pagesFailed'] += 1
                    rows.append({
                        **base, 'changeType': 'empty_content', 'pageTitle': ext.get('title'),
                        'fetchMethod': fr.method,
                        'previousCheckedAt': (old or {}).get('checkedAt'),
                        'summary': 'Page loaded but no text content was extracted',
                    })
                    await Actor.push_data(rows)
                    # Still charge — user asked us to check the page.
                    res = await Actor.charge(event_name=PAGE_EVENT)
                    if res.event_charge_limit_reached:
                        budget_hit = True
                    return

                snap = {
                    'version': SNAPSHOT_VERSION,
                    'url': url,
                    'selector': selector,
                    'contentMode': content_mode,
                    'checkedAt': checked_at,
                    'fetchMethod': fr.method,
                    'title': ext.get('title'),
                    'textLines': lines[:4000],
                    'textHash': short_hash('\n'.join(lines)),
                    'lineCount': len(lines),
                }
                common = {
                    **base, 'pageTitle': ext.get('title'), 'fetchMethod': fr.method,
                    'lineCount': len(lines), 'textHash': snap['textHash'],
                    'previousCheckedAt': (old or {}).get('checkedAt'), 'snapshotKey': key,
                }

                if not old or old.get('version') != SNAPSHOT_VERSION:
                    summary['baselines'] += 1
                    rows.append({
                        **common, 'changeType': 'baseline',
                        'summary': f'Baseline saved ({len(lines)} lines)',
                    })
                    await store.set_value(key, snap)
                else:
                    if old.get('textHash') != snap['textHash']:
                        a, r = line_diff(old.get('textLines', []), snap['textLines'])
                        summary['pagesChanged'] += 1
                        summary['changes'] += 1
                        rows.append({
                            **common, 'changeType': 'content_changed',
                            'addedText': a[:max_diff], 'removedText': r[:max_diff],
                            'addedCount': len(a), 'removedCount': len(r),
                            'summary': f'Content changed (+{len(a)} / -{len(r)} lines)',
                        })
                        all_changes.append(rows[-1])
                    elif include_unchanged:
                        rows.append({
                            **common, 'changeType': 'no_change',
                            'summary': f'No change since {old.get("checkedAt")} ({len(lines)} lines)',
                        })
                    await store.set_value(key, snap)

                summary['pagesChecked'] += 1
                if rows and rows[0]['changeType'] == 'baseline' and webhook_on_baseline:
                    all_changes.append(rows[0])
                if rows:
                    await Actor.push_data(rows)
                res = await Actor.charge(event_name=PAGE_EVENT)
                if res.event_charge_limit_reached:
                    budget_hit = True
                    Actor.log.warning('Maximum cost per run reached; skipping remaining pages.')

            try:
                await asyncio.gather(*(process(u, s, l) for u, s, l in targets))
            finally:
                if browser is not None:
                    await browser.close()

        webhook_result = None
        if webhook_url and all_changes:
            payload = {
                'event': 'website_changes_detected',
                'runStartedAt': run_started,
                'runId': Actor.configuration.actor_run_id,
                'changeCount': len(all_changes),
                'text': 'Website changes detected:\n' + '\n'.join(
                    f"- {c['url']}: {c['summary']}" for c in all_changes[:30]),
                'changes': all_changes,
            }
            payload['content'] = payload['text'][:1900]
            webhook_result = await send_webhook(
                webhook_url, payload, {str(k): str(v) for k, v in webhook_headers.items()})
            Actor.log.info(f'Webhook result: {webhook_result}')

        summary['webhook'] = webhook_result
        summary['finishedAt'] = now_iso()
        await Actor.set_value('OUTPUT', summary)
        msg = (
            f"Checked {summary['pagesChecked']}/{summary['pagesRequested']} pages: "
            f"{summary['changes']} changes, {summary['baselines']} new baselines, "
            f"{summary['pagesFailed']} problems"
        )
        Actor.log.info(msg)
        await Actor.set_status_message(msg, is_terminal=True)
