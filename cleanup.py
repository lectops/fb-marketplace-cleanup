"""Facebook Marketplace conversation cleanup.

Usage:
  python cleanup.py            # dry run — produces the plan file
  python cleanup.py --execute  # applies the plan, appends to the log file

Pass 1 (dry run) writes a plan; you review it, then run again with --execute.
Behavior, thresholds, language, and file locations are set in config.toml.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import sys
from typing import Optional

from playwright.sync_api import sync_playwright, BrowserContext, Page

import helpers as H
import classify as C
import config as config_mod
from config import Config


# ---------- data ----------

@dataclasses.dataclass
class ThreadPlan:
    thread_id: str
    counterparty: str
    item: Optional[str]
    last_msg_text: str
    last_msg_ts_text: Optional[str]
    last_msg_ts_iso: Optional[str]
    last_sender: Optional[str]
    msg_count: int
    action: str          # DELETE | ARCHIVE | LEAVE
    flagged: bool
    reasoning: str


# ---------- pass 1: scan + plan ----------

def scan(page: Page, cfg: Config, *, max_threads: Optional[int] = None, from_end: Optional[int] = None, skip: int = 0) -> list[ThreadPlan]:
    print()
    print("=" * 60)
    print("PASS 1 — scanning Marketplace conversations")
    print("=" * 60)

    # Land on the configured entry URL first to make sure we're authed.
    print(f"Navigating to entry URL: {cfg.entry_url}")
    page.goto(cfg.entry_url, wait_until="domcontentloaded", timeout=25000)
    H.wait_idle(page)

    if not H.is_logged_in(page):
        H.wait_for_login(page)
        # After login, reload entry URL.
        page.goto(cfg.entry_url, wait_until="domcontentloaded", timeout=25000)
        H.wait_idle(page)

    # Switch to Marketplace folder.
    print("Switching to Marketplace folder…")
    ok = H.open_marketplace_folder(page)
    if not ok:
        print()
        print("!!! Could not find the Marketplace folder automatically.")
        print("    Please open the Marketplace filter manually in the browser.")
        if sys.stdin and sys.stdin.isatty():
            input("    Press Enter when ready (Ctrl+C to abort). ")
        else:
            print("    Waiting 90s for you to do it (non-interactive stdin)…")
            import time as _t
            _t.sleep(90)
    H.wait_idle(page)
    H.human_pause(1.0, 2.0)

    print("Enumerating thread IDs (scrolling the chat list)…")
    all_ids = H.list_thread_ids(page)
    print(f"  found {len(all_ids)} thread(s) total")

    # Persist full enumeration before slicing.
    cfg.enum_cache.write_text(json.dumps({"ids": all_ids, "ts": dt.datetime.now().isoformat()}, indent=2))

    if from_end:
        ids = all_ids[-from_end:]
        print(f"  scanning the OLDEST {len(ids)} (--from-end {from_end})")
    elif skip or max_threads:
        start = skip
        end = (skip + max_threads) if max_threads else None
        ids = all_ids[start:end]
        print(f"  scanning slice [{start}:{end}] = {len(ids)} threads")
    else:
        ids = all_ids

    plans: list[ThreadPlan] = []
    now = dt.datetime.now()
    for i, tid in enumerate(ids, 1):
        print(f"\n[{i}/{len(ids)}] thread {tid}")
        try:
            H.open_thread(page, tid)
            hdr = H.read_thread_header(page)
            msgs = H.read_last_messages(page, n=10)
            if not msgs:
                # One more chance after a small wait — DOM hydration delay.
                H.tiny_pause(1.0, 1.6)
                msgs = H.read_last_messages(page, n=10)

            # Find timestamp of the most recent non-system message.
            last_ts_text = None
            for m in reversed(msgs):
                if m.get("ts_text"):
                    last_ts_text = m["ts_text"]
                    break
            last_ts = C.parse_ts(last_ts_text, now=now) if last_ts_text else None

            real = [m for m in msgs if m.get("sender") in ("me", "them")]
            last_text = real[-1]["text"] if real else ""
            last_sender = real[-1]["sender"] if real else None

            decision = C.classify(messages=msgs, last_msg_ts=last_ts, cfg=cfg, now=now)

            plan = ThreadPlan(
                thread_id=tid,
                counterparty=(hdr.get("counterparty") or "(unknown)")[:80],
                item=(hdr.get("item") or None),
                last_msg_text=last_text[:240],
                last_msg_ts_text=last_ts_text,
                last_msg_ts_iso=last_ts.isoformat() if last_ts else None,
                last_sender=last_sender,
                msg_count=len(real),
                action=decision.action,
                flagged=decision.flagged,
                reasoning=decision.reasoning,
            )
            plans.append(plan)
            tag = "⚑ " if decision.flagged else "  "
            print(f"  {tag}{decision.action:<7} — {plan.counterparty} — {decision.reasoning}")
            # Incremental save every 25 threads to survive crashes.
            if i % 25 == 0:
                try:
                    write_plan(plans, cfg)
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR on thread {tid}: {type(e).__name__}: {e}")
            plans.append(ThreadPlan(
                thread_id=tid,
                counterparty="(error)",
                item=None,
                last_msg_text="",
                last_msg_ts_text=None,
                last_msg_ts_iso=None,
                last_sender=None,
                msg_count=0,
                action="LEAVE",
                flagged=True,
                reasoning=f"scan error: {type(e).__name__}: {str(e)[:120]}",
            ))
        H.human_pause(1.0, 2.0)

    return plans


def truncate(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ").replace("|", "\\|").strip()
    return s if len(s) <= n else (s[: n - 1] + "…")


def write_plan(plans: list[ThreadPlan], cfg: Config) -> Path:
    by = {"DELETE": [], "ARCHIVE": [], "LEAVE": [], "FLAGGED": []}
    for p in plans:
        if p.flagged:
            by["FLAGGED"].append(p)
        else:
            by[p.action].append(p)

    lines: list[str] = []
    lines.append(f"# Marketplace cleanup plan — {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append(f"Total: {len(plans)}  |  "
                 f"DELETE: {len(by['DELETE'])}  |  "
                 f"ARCHIVE: {len(by['ARCHIVE'])}  |  "
                 f"LEAVE: {len(by['LEAVE'])}  |  "
                 f"FLAGGED: {len(by['FLAGGED'])}")
    lines.append("")
    lines.append("Review this plan, then run `python cleanup.py --execute`. The script will:")
    lines.append("  1. Archive all ARCHIVE rows.")
    lines.append(f"  2. Delete DELETE rows in batches of {cfg.batch_size} "
                 f"with a {cfg.batch_pause_seconds:.0f}s pause between batches.")
    lines.append("  3. Skip everything in LEAVE and FLAGGED.")
    lines.append("")

    for group in ("DELETE", "ARCHIVE", "LEAVE", "FLAGGED"):
        rows = by[group]
        lines.append(f"## {group} ({len(rows)})")
        lines.append("")
        if not rows:
            lines.append("_none_")
            lines.append("")
            continue
        lines.append("| # | Counterparty | Item | Last msg date | Last snippet | Reasoning | Thread ID |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, p in enumerate(rows, 1):
            lines.append(
                "| {i} | {cp} | {item} | {ts} | {snip} | {why} | `{tid}` |".format(
                    i=i,
                    cp=truncate(p.counterparty, 40),
                    item=truncate(p.item or "", 30),
                    ts=truncate(p.last_msg_ts_text or "", 24),
                    snip=truncate(p.last_msg_text, 80),
                    why=truncate(p.reasoning, 80),
                    tid=p.thread_id,
                )
            )
        lines.append("")

    cfg.plan_path.write_text("\n".join(lines))
    # Also dump JSON for execute pass.
    cfg.plan_path.with_suffix(".json").write_text(
        json.dumps([dataclasses.asdict(p) for p in plans], indent=2)
    )
    return cfg.plan_path


# ---------- pass 2: execute ----------

def load_plan(cfg: Config) -> list[ThreadPlan]:
    js = cfg.plan_path.with_suffix(".json")
    if not js.exists():
        sys.exit(f"plan JSON not found at {js} — run a dry run first")
    raw = json.loads(js.read_text())
    return [ThreadPlan(**r) for r in raw]


def log_action(line: str, cfg: Config) -> None:
    with cfg.log_path.open("a") as f:
        f.write(line + "\n")


def execute(page: Page, plans: list[ThreadPlan], cfg: Config) -> dict:
    print()
    print("=" * 60)
    print("PASS 2 — executing")
    print("=" * 60)

    if not cfg.log_path.exists():
        cfg.log_path.write_text("")
    log_action(f"\n## Run {dt.datetime.now().isoformat()}\n", cfg)

    # Need to be logged in.
    page.goto(cfg.entry_url, wait_until="domcontentloaded", timeout=25000)
    H.wait_idle(page)
    if not H.is_logged_in(page):
        H.wait_for_login(page)
    H.open_marketplace_folder(page)
    H.human_pause(1.0, 1.5)

    counts = {"archived": 0, "deleted": 0, "skipped": 0, "errors": 0}

    archives = [p for p in plans if p.action == "ARCHIVE" and not p.flagged]
    deletes = [p for p in plans if p.action == "DELETE" and not p.flagged]

    # Pre-load the chat list so deep/old rows are rendered. Then process
    # deletes oldest-first (which keeps targets near the visible bottom).
    if archives or deletes:
        print("Pre-scrolling chat list to bottom to load all rows…")
        final = H.scroll_chat_list_to_bottom(page)
        print(f"  scroll bottom = {final}")
        H.human_pause(0.8, 1.5)
    # Reverse so oldest is first (scan order has oldest at the end with --from-end).
    deletes = list(reversed(deletes))
    archives = list(reversed(archives))

    # ---- Archives first (reversible) ----
    print(f"\nArchiving {len(archives)} thread(s)…")
    for i, p in enumerate(archives, 1):
        ts = dt.datetime.now().isoformat(timespec="seconds")
        try:
            H.archive_thread_from_list(page, thread_id=p.thread_id, counterparty=p.counterparty)
            counts["archived"] += 1
            print(f"  [{i}/{len(archives)}] archived: {p.counterparty}")
            log_action(f"- {ts} | ARCHIVE | {p.counterparty} ({p.thread_id}) | ok", cfg)
        except Exception as e:  # noqa: BLE001
            counts["errors"] += 1
            print(f"  [{i}/{len(archives)}] ERROR archiving {p.counterparty}: {e}")
            log_action(f"- {ts} | ARCHIVE | {p.counterparty} ({p.thread_id}) | ERROR: {type(e).__name__}: {str(e)[:120]}", cfg)
        H.human_pause(1.0, 2.5)

    # ---- Deletes in batches ----
    batch_size = max(1, cfg.batch_size)
    print(f"\nDeleting {len(deletes)} thread(s) in batches of {batch_size}…")
    for batch_start in range(0, len(deletes), batch_size):
        batch = deletes[batch_start : batch_start + batch_size]
        for j, p in enumerate(batch, 1):
            idx = batch_start + j
            ts = dt.datetime.now().isoformat(timespec="seconds")
            try:
                H.delete_thread_from_list(page, thread_id=p.thread_id, counterparty=p.counterparty)
                counts["deleted"] += 1
                print(f"  [{idx}/{len(deletes)}] deleted: {p.counterparty}")
                log_action(f"- {ts} | DELETE  | {p.counterparty} ({p.thread_id}) | ok", cfg)
            except Exception as e:  # noqa: BLE001
                counts["errors"] += 1
                print(f"  [{idx}/{len(deletes)}] ERROR deleting {p.counterparty}: {e}")
                log_action(f"- {ts} | DELETE  | {p.counterparty} ({p.thread_id}) | ERROR: {type(e).__name__}: {str(e)[:120]}", cfg)
            H.human_pause(1.0, 2.5)
        if batch_start + batch_size < len(deletes):
            print(f"  …{cfg.batch_pause_seconds:.0f}-second pause (Ctrl+C to abort)…")
            try:
                import time
                time.sleep(cfg.batch_pause_seconds)
            except KeyboardInterrupt:
                print("\nAborted by user.")
                break

    counts["skipped"] = sum(1 for p in plans if p.action == "LEAVE" or p.flagged)
    return counts


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="apply the plan (default: dry run)")
    ap.add_argument("--max", type=int, default=None,
                    help="limit number of threads (debug)")
    ap.add_argument("--from-end", type=int, default=None,
                    help="scan only the N oldest threads (last N in enum order)")
    ap.add_argument("--skip", type=int, default=0,
                    help="skip the first N threads in enum order")
    args = ap.parse_args()

    cfg = config_mod.load()
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx: BrowserContext = pw.chromium.launch_persistent_context(
            user_data_dir=str(cfg.profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        try:
            if not args.execute:
                plans = scan(page, cfg, max_threads=args.max, from_end=args.from_end, skip=args.skip)
                path = write_plan(plans, cfg)
                print()
                print("=" * 60)
                print("Pass 1 complete. Plan written to:")
                print(f"  {path}")
                print("=" * 60)
                print("Review the plan, then run:")
                print("  python cleanup.py --execute")
            else:
                plans = load_plan(cfg)
                counts = execute(page, plans, cfg)
                print()
                print("=" * 60)
                print("Summary")
                print("=" * 60)
                print(f"  Total reviewed: {len(plans)}")
                print(f"  Archived:       {counts['archived']}")
                print(f"  Deleted:        {counts['deleted']}")
                print(f"  Left/Flagged:   {counts['skipped']}")
                print(f"  Errors:         {counts['errors']}")
                print(f"  Log file:       {cfg.log_path}")
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
