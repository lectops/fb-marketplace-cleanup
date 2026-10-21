"""Simple sweep: scroll Marketplace sidebar, delete every row older than N days.

Per-row workflow:
  1. Read row text → extract age suffix ("Xh", "Xd", "Xw", "Xmo", "Xy")
  2. If age >= threshold: click the row's "More options for X · Y" → Delete chat → confirm

Stops when no progress (no old rows visible after consecutive scrolls).
"""
from __future__ import annotations

import argparse
import datetime as dt
import random
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, BrowserContext, Page

import helpers as H
import config as config_mod

# Parse a chat-row's age suffix. Examples seen: "1h", "5h", "1d", "3w", "6mo", "1y".
# Anchored to end-of-string so we don't match dates inside message previews.
_AGE_RE = re.compile(r"(\d+)\s*(mo|min|s|h|d|w|y)\s*$", re.I)


def parse_row_age_days(text: str) -> float | None:
    """Extract the trailing age token in row text and return days. None if none found."""
    if not text:
        return None
    m = _AGE_RE.search(text.strip())
    if not m:
        if "just now" in text.lower():
            return 0.0
        return None
    n_str, unit = m.group(1), m.group(2).lower()
    n = int(n_str)
    if unit in ("s", "min"):
        return 0.0
    if unit == "h":
        return n / 24.0
    if unit == "d":
        return float(n)
    if unit == "w":
        return n * 7.0
    if unit == "mo":
        return n * 30.0
    if unit == "y":
        return n * 365.0
    return None


def log_action(line: str, log_path: Path) -> None:
    with log_path.open("a") as fh:
        fh.write(line + "\n")


def get_visible_rows(page: Page) -> list[dict]:
    """Return [{href, aria, text, title, age_days}] for all visible /marketplace/t/* rows."""
    raw = page.eval_on_selector_all(
        "a[href*='/marketplace/t/']",
        """els => els.filter(e => e.offsetParent !== null && e.getAttribute('aria-label')).map(e => ({
            href: e.getAttribute('href'),
            aria: e.getAttribute('aria-label'),
            text: (e.innerText || '')
        }))""",
    ) or []
    out = []
    for r in raw:
        aria = r.get("aria") or ""
        title = aria.replace("Group chat: ", "", 1).strip()
        age = parse_row_age_days(r.get("text") or "")
        out.append({
            "href": r.get("href") or "",
            "aria": aria,
            "title": title,
            "text": r.get("text") or "",
            "age_days": age,
        })
    return out


def scroll_sidebar_down(page: Page, *, by: int = 600) -> None:
    """Scroll the chat-list sidebar down by approximately `by` pixels."""
    try:
        page.evaluate(f"""
        () => {{
          const scrollers = [...document.querySelectorAll('*')].filter(e => {{
            const cs = getComputedStyle(e);
            return (cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                && e.scrollHeight > e.clientHeight + 50;
          }});
          // Pick the leftmost (sidebar)
          scrollers.sort((a,b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
          if (scrollers[0]) scrollers[0].scrollBy(0, {by});
        }}
        """)
    except Exception:
        page.mouse.wheel(0, by)


def delete_row(page: Page, href: str) -> None:
    """Find the chat row by href, click its 'More options' → Delete chat → confirm.

    Locates by stable href and finds the row's More options button via
    JS evaluation (most reliable across smart-quote / normalization issues).
    Hovers the row first because FB attaches click handlers on hover.
    """
    # 1. Scroll the link into view + hover the row to "activate" it.
    link = page.locator(f"a[href='{href}']").first
    link.scroll_into_view_if_needed(timeout=5000)
    try:
        link.hover(timeout=3000)
    except Exception:
        pass
    time.sleep(0.3 + random.random() * 0.3)

    # 2. Find the row's More options button via JS — walk up from the link
    #    until we find a parent that contains a 'More options for' descendant,
    #    then return that descendant. Use elementFromPoint as a sanity check.
    btn_handle = page.evaluate_handle(f"""
    () => {{
      const link = document.querySelector("a[href='{href}']");
      if (!link) return null;
      let el = link;
      while (el && el.parentElement) {{
        el = el.parentElement;
        const btn = el.querySelector("[aria-label^='More options for']");
        if (btn) return btn;
        if (el.tagName === 'BODY') break;
      }}
      return null;
    }}
    """)
    if not btn_handle.evaluate("el => !!el"):
        raise RuntimeError(f"More options button not found for href={href}")
    el = btn_handle.as_element()
    if el is None:
        raise RuntimeError(f"More options button handle not an element for href={href}")

    el.scroll_into_view_if_needed(timeout=3000)
    el.click(timeout=8000)
    time.sleep(0.5 + random.random() * 0.4)
    page.get_by_role("menuitem", name="Delete chat").first.click(timeout=8000)
    time.sleep(0.5 + random.random() * 0.4)
    confirm = page.get_by_role("button", name=re.compile(r"^Delete chat$", re.I)).first
    confirm.wait_for(state="visible", timeout=5000)
    confirm.click(timeout=8000)
    try:
        page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        pass


def main() -> int:
    cfg = config_mod.load()

    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold-days", type=float, default=cfg.sweep_threshold_days,
                    help=f"action rows older than this (config default {cfg.sweep_threshold_days:.0f})")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be deleted, don't act")
    ap.add_argument("--max-actions", type=int, default=cfg.sweep_max_actions,
                    help=f"safety cap on total deletes (config default {cfg.sweep_max_actions})")
    args = ap.parse_args()

    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg.log_path

    with sync_playwright() as pw:
        ctx: BrowserContext = pw.chromium.launch_persistent_context(
            user_data_dir=str(cfg.profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        try:
            page.goto(cfg.entry_url, wait_until="commit", timeout=30000)
            time.sleep(3.5)
            if not H.is_logged_in(page):
                H.wait_for_login(page)
            H.open_marketplace_folder(page)
            time.sleep(2.0)

            log_action(f"\n## Sweep {dt.datetime.now().isoformat()} (threshold={args.threshold_days}d, dry_run={args.dry_run})\n", log_path)

            actions = 0
            no_progress = 0
            seen_skipped_titles: set[str] = set()  # avoid logging skips repeatedly

            while actions < args.max_actions and no_progress < 15:
                rows = get_visible_rows(page)
                if not rows:
                    scroll_sidebar_down(page, by=800)
                    time.sleep(1.0)
                    no_progress += 1
                    continue

                # Old rows: age known and >= threshold; in dry-run skip already-listed
                old_rows = [
                    r for r in rows
                    if r["age_days"] is not None and r["age_days"] >= args.threshold_days
                    and not (args.dry_run and r["title"] in seen_skipped_titles)
                ]
                if not old_rows:
                    # Skip recent ones; scroll past them
                    sample = [(r["title"][:40], r["age_days"]) for r in rows[-3:]]
                    print(f"  [scroll] no old rows yet; last 3: {sample}")
                    scroll_sidebar_down(page, by=800)
                    time.sleep(0.9)
                    no_progress += 1
                    continue

                # Action the first old row (topmost old in viewport).
                r = old_rows[0]
                title = r["title"]
                age = r["age_days"]
                ts = dt.datetime.now().isoformat(timespec="seconds")

                if args.dry_run:
                    print(f"  [DRY] would delete: {title[:60]}  (age={age:.1f}d)")
                    log_action(f"- {ts} | DRY-DELETE | {title[:80]} | age={age:.1f}d", log_path)
                    seen_skipped_titles.add(title)
                    # Scroll a bit so the same row isn't re-listed forever in dry mode
                    scroll_sidebar_down(page, by=80)
                    time.sleep(0.4)
                    actions += 1
                    no_progress = 0
                    continue

                try:
                    delete_row(page, r["href"])
                    actions += 1
                    no_progress = 0
                    print(f"  [{actions}] deleted: {title[:60]}  (age={age:.1f}d)")
                    log_action(f"- {ts} | DELETE | {title[:80]} | age={age:.1f}d | ok", log_path)
                except Exception as e:  # noqa: BLE001
                    print(f"  ERROR deleting '{title[:50]}': {type(e).__name__}: {str(e)[:100]}")
                    log_action(f"- {ts} | DELETE | {title[:80]} | age={age:.1f}d | ERROR: {type(e).__name__}: {str(e)[:120]}", log_path)
                    # Nudge scroll to refresh state
                    scroll_sidebar_down(page, by=120)
                    no_progress += 1

                # Human-ish pause
                time.sleep(1.0 + random.random() * 2.0)

                # Every 5 actions take a longer breather
                if actions % 5 == 0 and not args.dry_run:
                    print("  …5s cooldown (Ctrl+C to abort)…")
                    time.sleep(5)

            print(f"\nDone. Total actions: {actions}")
            print(f"Log: {log_path}")
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
