"""Locator + interaction helpers for Facebook Messenger.

Centralized so when FB changes their DOM we fix it in one place.
We deliberately avoid class selectors — FB classes are obfuscated and rotate.
Everything here uses role/label/text/aria.
"""

from __future__ import annotations

import random
import re
import time
from contextlib import contextmanager
from typing import Callable, Iterable, Optional

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PWTimeout,
)

# ----- pacing -----

def human_pause(low: float = 1.0, high: float = 3.0) -> None:
    """Randomized delay between major actions."""
    time.sleep(random.uniform(low, high))


def tiny_pause(low: float = 0.2, high: float = 0.6) -> None:
    time.sleep(random.uniform(low, high))


# ----- retry / resilience -----

def retry(
    fn: Callable,
    *,
    attempts: int = 4,
    base_delay: float = 0.6,
    label: str = "op",
):
    """Retry with exponential backoff. Returns fn() result or re-raises."""
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if i == attempts - 1:
                break
            delay = base_delay * (2 ** i) + random.uniform(0, 0.3)
            print(f"  [retry] {label} failed ({type(e).__name__}: {str(e)[:80]}), "
                  f"retrying in {delay:.1f}s (attempt {i+2}/{attempts})")
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


def safe_click(loc: Locator, *, label: str = "click", timeout: int = 5000) -> None:
    """Scroll into view then click, with retry on stale/intercept."""
    def _do():
        loc.scroll_into_view_if_needed(timeout=timeout)
        loc.click(timeout=timeout)
    retry(_do, label=label)


def wait_idle(page: Page, timeout: int = 8000) -> None:
    """Wait for network to settle. Best-effort — FB streams a lot so we don't fail hard."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeout:
        pass


# ----- auth / nav -----

def is_logged_in(page: Page) -> bool:
    """Heuristic: are we logged into Facebook/Messenger?

    Strongest signal: the URL is *not* a login/checkpoint URL AND we're not
    looking at an email/password form. We treat any non-auth FB page as
    logged in — FB's UI varies too much across rollouts to require a
    specific element.
    """
    url = page.url or ""
    low = url.lower()
    if not low.startswith(("http://", "https://")):
        return False
    if any(x in low for x in ("/login", "/checkpoint", "/recover", "/two_step", "login.php")):
        return False
    # If a password input is on the page, we're on an auth screen.
    try:
        if page.locator("input[type='password']").first.is_visible(timeout=800):
            return False
    except Exception:
        pass
    # Otherwise: any messenger.com or facebook.com page after that filter
    # counts as logged in.
    return ("messenger.com" in low) or ("facebook.com" in low)


def wait_for_login(page: Page, *, poll_timeout_s: int = 600) -> None:
    """Pause until user logs in.

    If stdin is a TTY, waits for Enter. Otherwise polls page state every 3s
    until login is detected (so this works when launched from a non-interactive
    subprocess too).
    """
    import sys as _sys
    print()
    print("=" * 60)
    print("LOGIN REQUIRED")
    print("=" * 60)
    print("A Chromium window is open. Please:")
    print("  1. Log into Facebook in that window.")
    print("  2. Wait until you can see Messenger.")
    print("=" * 60)
    if _sys.stdin and _sys.stdin.isatty():
        print("Press Enter here when logged in...")
        try:
            input()
        except EOFError:
            pass
        wait_idle(page)
        return

    # Non-interactive: poll the page until login is detected.
    print("(non-interactive stdin — will auto-detect login by polling)")
    print(f"Polling every 3s, up to {poll_timeout_s}s")
    waited = 0
    while waited < poll_timeout_s:
        if is_logged_in(page):
            print(f"  detected login after {waited}s")
            wait_idle(page)
            return
        time.sleep(3)
        waited += 3
        if waited % 30 == 0:
            print(f"  still waiting for login… ({waited}s)")
    raise RuntimeError(f"login not detected after {poll_timeout_s}s")


# ----- Marketplace folder -----

def open_marketplace_folder(page: Page) -> bool:
    """Switch to Marketplace chats filter.

    Messenger.com exposes Marketplace as a category in the left chat-list header.
    Different layouts:
      - A horizontal pill/tab labeled "Marketplace"
      - A dropdown "Inbox" → menu item "Marketplace"
      - On messenger.com/marketplace/ direct URL
    Returns True if we land on the Marketplace view.
    """
    # Try direct URL first — fastest and most reliable.
    try:
        page.goto("https://www.messenger.com/marketplace/", wait_until="domcontentloaded", timeout=20000)
        wait_idle(page)
        human_pause(1.0, 2.0)
        # If FB redirected us back to /t/... that's fine — Marketplace filter is still active in the sidebar.
        if "/marketplace" in (page.url or "") or _marketplace_pill_visible(page):
            return True
    except Exception as e:  # noqa: BLE001
        print(f"  [marketplace] direct URL nav failed: {e}")

    # Fallback: click the pill/tab if it exists.
    for name in ("Marketplace", "marketplace"):
        try:
            tab = page.get_by_role("tab", name=re.compile(name, re.I)).first
            if tab.is_visible(timeout=1500):
                safe_click(tab, label="marketplace tab")
                wait_idle(page)
                return True
        except Exception:
            pass
        try:
            link = page.get_by_role("link", name=re.compile(name, re.I)).first
            if link.is_visible(timeout=1500):
                safe_click(link, label="marketplace link")
                wait_idle(page)
                return True
        except Exception:
            pass
        try:
            btn = page.get_by_role("button", name=re.compile(name, re.I)).first
            if btn.is_visible(timeout=1500):
                safe_click(btn, label="marketplace button")
                wait_idle(page)
                return True
        except Exception:
            pass
    return False


def _marketplace_pill_visible(page: Page) -> bool:
    try:
        return page.get_by_text(re.compile(r"^Marketplace$", re.I)).first.is_visible(timeout=1500)
    except Exception:
        return False


# ----- thread enumeration -----

def list_thread_ids(page: Page, *, max_scroll: int = 120, stagnant_limit: int = 8) -> list[str]:
    """Enumerate all Marketplace thread IDs by scrolling the chat list deeply.

    Messenger virtualizes the chat list and lazy-loads on scroll-near-bottom.
    Strategy:
      - Find the actual scrollable container (must have scrollHeight > clientHeight)
      - Each iteration: scroll its full height (end), wait, harvest /t/<id> hrefs
      - Stop when no new IDs for `stagnant_limit` consecutive cycles
    """
    seen: dict[str, None] = {}

    # Find the scroller. Walk candidates with scrollHeight > clientHeight.
    scroller_handle = page.evaluate_handle("""
        () => {
          // Prefer the leftmost vertically-scrollable region inside [role=navigation]
          // or [role=grid]. Otherwise, find any scrollable container in the left half.
          const candidates = [...document.querySelectorAll(
            '[role=navigation], [role=grid], [role=navigation] *, [role=grid] *'
          )].filter(e => {
            const cs = getComputedStyle(e);
            return (cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                && e.scrollHeight > e.clientHeight + 50;
          });
          // Sort by scrollHeight desc → biggest scrollable is likely the chat list
          candidates.sort((a, b) => b.scrollHeight - a.scrollHeight);
          return candidates[0] || null;
        }
    """)

    has_scroller = False
    try:
        has_scroller = scroller_handle.evaluate("el => !!el")
    except Exception:
        has_scroller = False

    stagnant = 0
    last_count = 0
    prev_scrolltop = -1

    for i in range(max_scroll):
        # Harvest before scrolling so we don't miss anything.
        try:
            hrefs = page.eval_on_selector_all(
                "a[href*='/t/']",
                "els => els.map(e => e.getAttribute('href'))",
            )
        except Exception:
            hrefs = []
        for h in hrefs or []:
            m = re.search(r"/t/(\d+)", h or "")
            if m:
                seen.setdefault(m.group(1), None)

        # Scroll
        cur_scrolltop = -1
        if has_scroller:
            try:
                cur_scrolltop = scroller_handle.evaluate(
                    "el => { el.scrollTop = el.scrollHeight; return el.scrollTop; }"
                )
            except Exception:
                page.mouse.wheel(0, 900)
        else:
            page.mouse.wheel(0, 900)

        # Wait for lazy load — longer than before
        time.sleep(0.8 + random.uniform(0, 0.5))

        if len(seen) == last_count and (cur_scrolltop == prev_scrolltop and cur_scrolltop >= 0):
            # No new threads AND scroller didn't move = end of list
            stagnant += 1
            if stagnant >= stagnant_limit:
                break
        else:
            stagnant = 0
            last_count = len(seen)
            prev_scrolltop = cur_scrolltop

    return list(seen.keys())


# ----- open & read a thread -----

def open_thread(page: Page, thread_id: str) -> None:
    """Navigate to a thread, tolerant of FB's lazy/streaming load.

    `wait_until="commit"` returns as soon as the navigation has been committed
    by the network stack, *before* DOM hydration. We then poll for either the
    conversation H2 or a message label — whichever indicates the thread is
    actually rendered. Retries once on timeout.
    """
    url = f"https://www.messenger.com/t/{thread_id}"
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            page.goto(url, wait_until="commit", timeout=45000)
            # Then wait for any sign the thread rendered.
            try:
                page.wait_for_selector(
                    "[role='main'] h2, [role='main'] [aria-label^='Enter, Message sent']",
                    timeout=12000,
                    state="attached",
                )
            except PWTimeout:
                # Empty thread or very slow — keep going; downstream handles missing data.
                pass
            human_pause(1.2, 2.4)
            return
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt == 0:
                print(f"  [retry] goto {thread_id} failed ({type(e).__name__}); retrying after pause")
                human_pause(3.0, 5.0)
                continue
            raise last_exc


# Pattern: "Enter, Message sent <date+time> by <sender>: <content>"
# date+time forms we see:
#   "10:50"                        — today
#   "4 May 2026, 14:45"            — past day, day-month-year
# sender is literally "You" or the counterparty's first name (often).
_MSG_ARIA_RE = re.compile(
    r"^Enter, Message sent (?P<dt>.+?) by (?P<sender>[^:]+?)(?:: (?P<content>.+))?$",
    re.DOTALL,
)

# Aria labels FB inserts that aren't real messages.
_SYSTEM_PATTERNS = (
    re.compile(r"\bstarted this chat\b", re.I),
    re.compile(r"\bis waiting for your response\b", re.I),
    re.compile(r"Meta may use technology to review Marketplace", re.I),
    re.compile(r"^You sent an attachment\.?$", re.I),
)


def read_thread_header(page: Page) -> dict:
    """Pull counterparty + item from the H2 inside role='main'.

    Format observed on messenger.com:
      h2.aria-label == "Conversation titled <Name> · <Item Title>"
    """
    info: dict = {"counterparty": None, "item": None}
    try:
        # Wait for the H2 to appear — FB hydrates async.
        page.wait_for_selector("[role='main'] h2", timeout=8000, state="attached")
    except PWTimeout:
        return info

    try:
        h2 = page.locator("[role='main'] h2").first
        title = h2.get_attribute("aria-label") or ""
        if not title:
            try:
                title = (h2.inner_text(timeout=1500) or "")
            except Exception:
                title = ""
        title = title.replace("Conversation titled ", "", 1).strip()
        # Split on a stand-alone middle-dot " · " (U+00B7) with spaces around it.
        parts = re.split(r"\s·\s", title, maxsplit=1)
        info["counterparty"] = (parts[0] or "").strip() or None
        if len(parts) > 1:
            info["item"] = (parts[1] or "").strip() or None
    except Exception:
        pass
    return info


def read_last_messages(page: Page, n: int = 10) -> list[dict]:
    """Return up to n most-recent real messages, oldest→newest.

    Strategy: harvest every element with aria-label starting "Enter, Message sent…"
    inside role='main'. Each label encodes timestamp + sender + content
    unambiguously. We filter FB's auto-injected "is waiting for your response",
    "started this chat", and Meta-disclosure labels.

    Each returned dict: {text, sender ('me'|'them'), ts_text}
    """
    # Wait for at least one message label.
    try:
        page.wait_for_selector(
            "[role='main'] [aria-label^='Enter, Message sent']",
            timeout=8000,
            state="attached",
        )
    except PWTimeout:
        return []

    # Tiny extra wait — hydration sometimes streams in more.
    tiny_pause(0.4, 0.8)

    try:
        labels: list[str] = page.eval_on_selector_all(
            "[role='main'] [aria-label^='Enter, Message sent']",
            "els => els.map(e => e.getAttribute('aria-label'))",
        ) or []
    except Exception:
        labels = []

    msgs: list[dict] = []
    seen: set[str] = set()  # dedupe identical aria-labels
    for raw in labels:
        if not raw or raw in seen:
            continue
        seen.add(raw)
        m = _MSG_ARIA_RE.match(raw)
        if not m:
            continue
        dt_str = (m.group("dt") or "").strip()
        sender_name = (m.group("sender") or "").strip()
        content = (m.group("content") or "").strip()

        # Filter system / FB-injected
        skip = False
        for pat in _SYSTEM_PATTERNS:
            if pat.search(content) or pat.search(raw):
                skip = True
                break
        if skip:
            continue

        sender = "me" if sender_name.lower() == "you" else "them"
        # Non-text (sticker/attachment) → empty content. Keep as a marker.
        if not content:
            content = "(non-text)"
        msgs.append({"text": content, "sender": sender, "ts_text": dt_str})

    return msgs[-n:]


# ----- archive / delete actions -----

def scroll_chat_list_to_bottom(page: Page, *, max_iters: int = 200) -> int:
    """Scroll the left chat-list sidebar all the way down. Returns final scrollTop.

    Used before bulk-deleting old threads so the oldest rows are rendered.
    Stops after 5 stagnant iterations (no scroll position change).
    """
    sh = page.evaluate_handle("""
        () => {
          const cs_ok = e => {
            const cs = getComputedStyle(e);
            return (cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                && e.scrollHeight > e.clientHeight + 50;
          };
          const cands = [...document.querySelectorAll(
            '[role=navigation], [role=grid], [role=navigation] *, [role=grid] *'
          )].filter(cs_ok);
          cands.sort((a,b) => b.scrollHeight - a.scrollHeight);
          return cands[0] || null;
        }
    """)
    if not sh.evaluate("el => !!el"):
        return -1

    prev = -1
    stagnant = 0
    final = 0
    for _ in range(max_iters):
        try:
            final = sh.evaluate(
                "el => { el.scrollTop = el.scrollHeight; return el.scrollTop; }"
            )
        except Exception:
            break
        time.sleep(0.45 + random.uniform(0, 0.25))
        if final == prev:
            stagnant += 1
            if stagnant >= 5:
                break
        else:
            stagnant = 0
            prev = final
    return final


def _scroll_row_into_view(page: Page, thread_id: str, *, max_scrolls: int = 30) -> Locator:
    """Ensure the chat-list row for thread_id is in view; return the row link.

    Scrolls the chat list (not the main page) if needed.
    """
    target = page.locator(f"a[href*='/t/{thread_id}']").first
    for _ in range(max_scrolls):
        try:
            if target.count() > 0 and target.is_visible(timeout=600):
                target.scroll_into_view_if_needed(timeout=2000)
                return target
        except Exception:
            pass
        # Scroll the chat list — find the navigation/grid container
        scrolled = False
        for role in ("grid", "navigation"):
            try:
                cont = page.get_by_role(role).first
                if cont.is_visible(timeout=600):
                    cont.evaluate("el => el.scrollBy(0, el.clientHeight * 0.8)")
                    scrolled = True
                    break
            except Exception:
                continue
        if not scrolled:
            page.mouse.wheel(0, 700)
        tiny_pause(0.3, 0.7)
    # Last try
    target.scroll_into_view_if_needed(timeout=4000)
    return target


def _act_on_row(
    page: Page,
    *,
    thread_id: str,
    counterparty: str,
    action: str,  # "archive" | "delete"
) -> None:
    """Click the row's 'More options for X · …' menu and apply action.

    Requires that the sidebar has been pre-scrolled so the row is already
    rendered (see scroll_chat_list_to_bottom). On miss, we re-scroll once.
    """
    cp = (counterparty or "").strip()
    if not cp:
        raise RuntimeError("counterparty missing — cannot find row menu button")

    pattern = re.compile(rf"^More options for {re.escape(cp)}( ·.*)?$", re.I)

    def _find_btn():
        return page.get_by_role("button", name=pattern).first

    btn = _find_btn()
    if not btn.is_visible(timeout=1500):
        # Row not loaded — scroll sidebar to bottom and retry.
        scroll_chat_list_to_bottom(page)
        tiny_pause(0.4, 0.8)
        btn = _find_btn()
        if not btn.is_visible(timeout=2000):
            raise RuntimeError(f"row 'More options for {cp}' not found after rescroll")

    btn.scroll_into_view_if_needed(timeout=4000)
    safe_click(btn, label=f"row more-options [{cp}]")
    tiny_pause(0.6, 1.1)

    if action == "archive":
        menu_re = re.compile(r"^Archive chat$", re.I)
        modal_re = re.compile(r"^(Archive|OK|Confirm)$", re.I)
    elif action == "delete":
        menu_re = re.compile(r"^Delete chat$", re.I)
        modal_re = re.compile(r"^(Delete chat|Delete)$", re.I)
    else:
        raise ValueError(f"unknown action: {action}")

    item = page.get_by_role("menuitem", name=menu_re).first
    safe_click(item, label=f"menuitem {action}")
    tiny_pause(0.6, 1.1)

    # Modal confirmation (delete always has one; archive sometimes does)
    try:
        confirm = page.get_by_role("button", name=modal_re).first
        if confirm.is_visible(timeout=4000 if action == "delete" else 1800):
            safe_click(confirm, label=f"confirm {action}")
    except Exception:
        if action == "delete":
            raise
    wait_idle(page, timeout=4000)


def archive_thread_from_list(page: Page, *, thread_id: str, counterparty: str) -> None:
    _act_on_row(page, thread_id=thread_id, counterparty=counterparty, action="archive")


def delete_thread_from_list(page: Page, *, thread_id: str, counterparty: str) -> None:
    _act_on_row(page, thread_id=thread_id, counterparty=counterparty, action="delete")


@contextmanager
def dismiss_dialogs(page: Page):
    """Auto-dismiss unexpected JS dialogs while inside the block."""
    def _handler(d):
        try:
            d.dismiss()
        except Exception:
            pass
    page.on("dialog", _handler)
    try:
        yield
    finally:
        try:
            page.remove_listener("dialog", _handler)
        except Exception:
            pass
