"""Shared fetch / text-extract / diff helpers for monitor Actors.

Canonical copy: /workspace/apify-actors/common/monitor_core.py
Synced into each Actor package by sync_common.sh.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)
DEFAULT_HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
}
NOISE_TAGS = ('script', 'style', 'noscript', 'svg', 'template', 'iframe', 'canvas', 'picture', 'video', 'audio', 'head')
MAX_LINES = 4000
MAX_LINE_LEN = 400


def normalize_ws(text: str) -> str:
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def short_hash(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:n]


@dataclass
class FetchResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    body: str | None = None
    content_type: str | None = None
    method: str = 'http'
    error: str | None = None
    attempts: int = 0

    @property
    def html(self) -> str | None:
        return self.body


async def fetch_http(client: httpx.AsyncClient, url: str, retries: int = 2, timeout: float = 30.0) -> FetchResult:
    res = FetchResult(url=url, method='http')
    for attempt in range(retries + 1):
        res.attempts = attempt + 1
        try:
            r = await client.get(url, headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True)
            res.status, res.final_url = r.status_code, str(r.url)
            res.content_type = (r.headers.get('content-type') or '').split(';')[0].strip().lower()
            if r.status_code == 200 and r.text:
                res.body, res.error = r.text, None
                return res
            res.error = f'HTTP {r.status_code}'
            if r.status_code in (401, 403, 404, 410):
                return res
        except (httpx.HTTPError, asyncio.TimeoutError) as e:  # noqa: PERF203
            res.error = f'{type(e).__name__}: {e or repr(e)}'[:300]
        await asyncio.sleep(1.5 * (attempt + 1))
    return res


class BrowserFetcher:
    """Lazily launched headless Chromium for JS-rendered pages."""

    def __init__(self, headless: bool = True, max_pages: int = 1) -> None:
        self._pw = None
        self._browser = None
        self._headless = headless
        self._launch_lock = asyncio.Lock()
        self._pages = asyncio.Semaphore(max(1, max_pages))

    async def _ensure(self) -> None:
        async with self._launch_lock:
            await self._launch()

    async def _launch(self) -> None:
        if self._browser is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=self._headless,
                args=['--disable-gpu', '--disable-dev-shm-usage', '--no-sandbox'],
            )

    async def fetch(self, url: str, retries: int = 1, timeout_ms: int = 45000) -> FetchResult:
        async with self._pages:
            return await self._fetch(url, retries, timeout_ms)

    async def _fetch(self, url: str, retries: int, timeout_ms: int) -> FetchResult:
        res = FetchResult(url=url, method='browser')
        for attempt in range(retries + 1):
            res.attempts = attempt + 1
            context = None
            try:
                await self._ensure()
                context = await self._browser.new_context(
                    user_agent=USER_AGENT, locale='en-US', viewport={'width': 1440, 'height': 2000})
                page = await context.new_page()

                async def _block(route):
                    if route.request.resource_type in ('image', 'media', 'font'):
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route('**/*', _block)
                resp = await page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
                res.status = resp.status if resp else None
                try:
                    await page.wait_for_load_state('networkidle', timeout=12000)
                except Exception:  # noqa: BLE001
                    pass
                for _ in range(4):
                    await page.mouse.wheel(0, 1400)
                    await page.wait_for_timeout(300)
                await page.wait_for_timeout(500)
                res.final_url = page.url
                res.body = await page.content()
                res.content_type = 'text/html'
                res.error = None if res.body else 'empty page'
                if res.body and (res.status is None or res.status < 400):
                    return res
                res.error = f'HTTP {res.status}'
            except Exception as e:  # noqa: BLE001
                res.error = f'{type(e).__name__}: {e or repr(e)}'[:300]
            finally:
                if context is not None:
                    await context.close()
            await asyncio.sleep(2)
        return res

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
        self._browser = self._pw = None


def soup_from(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, 'lxml')


def apply_selector(soup: BeautifulSoup, selector: str | None) -> tuple[Tag | BeautifulSoup, bool]:
    if not selector:
        return soup, True
    try:
        node = soup.select_one(selector)
    except Exception:  # noqa: BLE001 - invalid CSS
        return soup, False
    if node is None:
        return soup, False
    return node, True


def visible_text_lines(root: Tag | BeautifulSoup, max_lines: int = MAX_LINES) -> list[str]:
    for tag in root.find_all(NOISE_TAGS):
        tag.decompose()
    lines: list[str] = []
    seen: set[str] = set()
    for el in root.find_all(string=True):
        parent = el.parent
        if parent is None or parent.name in NOISE_TAGS:
            continue
        text = normalize_ws(str(el))
        if len(text) < 2:
            continue
        if text in seen:
            continue
        seen.add(text)
        lines.append(text[:MAX_LINE_LEN])
        if len(lines) >= max_lines:
            break
    return lines


def extract_page(html: str, selector: str | None = None) -> dict[str, Any]:
    soup = soup_from(html)
    title_tag = soup.find('title')
    title = normalize_ws(title_tag.get_text()) if title_tag else None
    root, matched = apply_selector(soup, selector)
    lines = visible_text_lines(root)
    text = '\n'.join(lines)
    return {
        'title': title,
        'selectorMatched': matched,
        'textLines': lines,
        'text': text[:200000],
        'textHash': short_hash(text),
        'lineCount': len(lines),
    }


def try_parse_json(body: str) -> Any | None:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None


def flatten_json(obj: Any, prefix: str = '', out: list[str] | None = None, max_items: int = 3000) -> list[str]:
    if out is None:
        out = []
    if len(out) >= max_items:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f'{prefix}.{k}' if prefix else str(k)
            flatten_json(v, key, out, max_items)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:200]):
            flatten_json(v, f'{prefix}[{i}]', out, max_items)
    else:
        out.append(f'{prefix}={obj}'[:MAX_LINE_LEN])
    return out


DATE_RE = re.compile(
    r'\b(?:'
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}'
    r'|\d{4}-\d{2}-\d{2}'
    r'|\d{1,2}/\d{1,2}/\d{2,4}'
    r')\b',
    re.I,
)
STATUS_WORDS = re.compile(
    r'\b(operational|degraded|outage|incident|maintenance|resolved|investigating|'
    r'major|minor|partial|down|uptime|disruption|scheduled)\b',
    re.I,
)
CHANGELOG_WORDS = re.compile(
    r'\b(added|fixed|improved|updated|released|launched|deprecated|removed|changed|'
    r'new|feature|bug|security|announcement|changelog|release notes?)\b',
    re.I,
)


def _heading_blocks(root: Tag | BeautifulSoup) -> list[dict[str, Any]]:
    """Split page into heading + following text blocks (changelog/status style)."""
    blocks: list[dict[str, Any]] = []
    headings = root.find_all(['h1', 'h2', 'h3', 'h4', 'article', 'li'])
    # Prefer real headings when present
    real = root.find_all(['h1', 'h2', 'h3', 'h4'])
    nodes = real if len(real) >= 2 else headings
    for h in nodes[:80]:
        title = normalize_ws(h.get_text(' ', strip=True))
        if len(title) < 3 or len(title) > 220:
            continue
        bits: list[str] = []
        for sib in h.next_siblings:
            if getattr(sib, 'name', None) in ('h1', 'h2', 'h3', 'h4'):
                break
            if isinstance(sib, Tag):
                if sib.name in NOISE_TAGS:
                    continue
                t = normalize_ws(sib.get_text(' ', strip=True))
                if t:
                    bits.append(t[:500])
            if len(bits) >= 6:
                break
        body = ' '.join(bits)[:1200]
        full = f'{title}\n{body}'.strip()
        date_m = DATE_RE.search(full)
        kind = 'status' if STATUS_WORDS.search(full) else ('changelog' if CHANGELOG_WORDS.search(full) else 'entry')
        blocks.append({
            'title': title,
            'date': date_m.group(0) if date_m else None,
            'kind': kind,
            'summary': body[:400] if body else title,
            'textHash': short_hash(full),
            'text': full[:1500],
        })
    # Dedupe by title
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for b in blocks:
        k = b['title'].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(b)
    return uniq[:60]


def extract_changelog_status(html: str, selector: str | None = None, page_kind: str = 'auto') -> dict[str, Any]:
    soup = soup_from(html)
    title_tag = soup.find('title')
    title = normalize_ws(title_tag.get_text()) if title_tag else None
    root, matched = apply_selector(soup, selector)
    events = _heading_blocks(root)
    if page_kind in ('changelog', 'status'):
        for e in events:
            if e['kind'] == 'entry':
                e['kind'] = page_kind
    lines = visible_text_lines(root)
    text = '\n'.join(lines)
    return {
        'title': title,
        'selectorMatched': matched,
        'events': events,
        'textLines': lines,
        'text': text[:200000],
        'textHash': short_hash(text),
        'lineCount': len(lines),
        'eventCount': len(events),
    }


def line_diff(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    old_set, new_set = set(old), set(new)
    added = [ln for ln in dict.fromkeys(new) if ln not in old_set]
    removed = [ln for ln in dict.fromkeys(old) if ln not in new_set]
    return added, removed


def event_key(ev: dict[str, Any]) -> str:
    return short_hash((ev.get('title') or '') + '|' + (ev.get('date') or ''))


async def fetch_page(
    url: str,
    *,
    client: httpx.AsyncClient,
    browser: BrowserFetcher | None,
    mode: str = 'auto',
) -> FetchResult:
    """Fetch with optional browser fallback when HTTP body looks empty/blocked."""
    fr = await fetch_http(client, url)
    if mode == 'http':
        return fr
    if mode == 'browser':
        if browser is None:
            fr.error = 'browser mode requested but browser not available'
            return fr
        return await browser.fetch(url)
    # auto
    need_browser = False
    if fr.error or not fr.body:
        need_browser = True
    elif fr.body and len(fr.body) < 800:
        need_browser = True
    elif fr.body:
        low = fr.body.lower()
        if any(x in low for x in ('cf-browser-verification', 'just a moment', 'enable javascript', 'captcha')):
            need_browser = True
        elif fr.content_type and 'json' not in fr.content_type and '<body' in low and len(visible_text_lines(soup_from(fr.body))) < 5:
            need_browser = True
    if need_browser and browser is not None:
        br = await browser.fetch(url)
        if br.body and (not fr.body or len(br.body) > len(fr.body)):
            return br
    return fr
