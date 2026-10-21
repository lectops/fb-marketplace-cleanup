# Marketplace Cleanup

A Playwright bot that bulk-archives and deletes stale **Facebook Marketplace**
message threads. It drives a real Chromium window (you log in once), scans your
Marketplace conversations, classifies each one, writes a plan you review, and
only then applies it.

It is built around a **review gate**: the first pass never touches anything. It
produces a plan file. You read it. You run the second pass to execute.

> ⚠️ **Read this before running.**
> - This tool **permanently deletes** Facebook conversations. Deletion is not reversible. Archiving is.
> - It logs into Facebook as *you*, using a real browser profile that stores your session. Automating your account may conflict with Facebook's Terms of Service — use it on your own account, at your own risk.
> - Facebook changes its DOM frequently. When selectors break, things stop working; that's expected for a tool like this. Always do a dry run first.
> - Always review the generated plan before running `--execute`.

---

## How it works

Two scripts, two strategies. Both share `config.toml`, `helpers.py` (Messenger
DOM helpers), and the persistent login profile.

### `cleanup.py` — scan, plan, execute (recommended)

A careful two-pass workflow:

1. **Pass 1 (dry run):** opens each Marketplace thread, reads the recent
   messages and timestamp, classifies it, and writes:
   - `marketplace-cleanup-plan.md` — human-readable table grouped by action
   - `marketplace-cleanup-plan.json` — machine-readable, consumed by pass 2
2. **You review the plan.**
3. **Pass 2 (`--execute`):** archives the `ARCHIVE` threads, then deletes the
   `DELETE` threads in batches, skipping anything marked `LEAVE` or `FLAGGED`.
   Every action is appended to `marketplace-cleanup-log.md`.

Each thread is sorted into one of four buckets:

| Action     | Meaning                                                        |
|------------|----------------------------------------------------------------|
| `DELETE`   | Seller said it's sold/taken, or my inquiry went unanswered and the thread is old. |
| `ARCHIVE`  | Deal completed with clear closure language ("received", "thanks, enjoy"). |
| `LEAVE`    | Recent activity, or ongoing logistics — left untouched.        |
| `FLAGGED`  | Ambiguous — never auto-actioned, surfaced for manual review.   |

Classification logic lives in `classify.py`; the phrases and age thresholds it
uses come entirely from `config.toml` (see [Configuration](#configuration)).

### `sweep.py` — delete everything older than N days (blunt)

A simpler tool that scrolls the Marketplace sidebar and deletes every row older
than a threshold, reading the age straight off each row ("3w", "6mo", "1y"). No
content classification. Supports `--dry-run`.

```bash
python sweep.py --dry-run                 # show what it would delete
python sweep.py --threshold-days 30       # delete rows older than 30 days
python sweep.py --max-actions 50          # safety cap
```

---

## Requirements

- Python 3.11+ (uses the standard-library `tomllib`)
- A Facebook account with Marketplace messages
- A desktop environment — the bot opens a visible Chromium window you log into

## Install

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
# 1. Dry run — scan and write the plan. Nothing is changed.
python cleanup.py

# 2. Open marketplace-cleanup-plan.md and review it carefully.

# 3. Apply the reviewed plan.
python cleanup.py --execute
```

The **first run opens a Chromium window** and asks you to log into Facebook.
The login is saved in `fb-cleanup-profile/` (git-ignored), so later runs reuse
it. If the script can't find the Marketplace filter automatically, switch to it
manually in the window and let the script continue.

Useful `cleanup.py` flags for large inboxes:

| Flag            | Effect                                              |
|-----------------|-----------------------------------------------------|
| `--max N`       | Scan only the first N threads.                      |
| `--from-end N`  | Scan only the N oldest threads.                     |
| `--skip N`      | Skip the first N threads.                           |

## Configuration

Everything tunable lives in [`config.toml`](config.toml). Every key is optional;
anything you omit falls back to a built-in default in `config.py`.

```toml
[classifier]
recent_days = 14       # last message newer than this => LEAVE
gray_zone_days = 30    # between the two => FLAGGED for review; older => eligible to delete

[execute]
batch_size = 5         # delete in batches of this many
batch_pause_seconds = 5

[phrases]
sold = [ "sold na", "no longer available", ... ]
closure = [ "received na", "thanks, enjoy", ... ]
inquiry_no_reply = [ "still available", "how much", ... ]
coordination = [ "meet up", "cod", "gcash", ... ]
```

**Language:** the default phrase banks are tuned for **English + Filipino
(Taglish)** Marketplace chats. If you sell in another language, replace the
phrase lists in `config.toml` with your own — they're matched case-insensitively
as substrings of message text.

## Files

| File                 | Purpose                                            |
|----------------------|----------------------------------------------------|
| `cleanup.py`         | Scan → plan → execute workflow (main entry point). |
| `sweep.py`           | Blunt "delete older than N days" alternative.      |
| `classify.py`        | Decision rules (DELETE / ARCHIVE / LEAVE / flag).  |
| `helpers.py`         | Playwright locators and Messenger interactions.    |
| `config.py`          | Config loader + baked-in defaults.                 |
| `config.toml`        | Your settings (thresholds, pacing, phrase banks).  |

Generated at runtime and **git-ignored** (they contain personal data):
`fb-cleanup-profile/`, `marketplace-cleanup-plan.{md,json}`,
`marketplace-cleanup-log.md`, `marketplace-enum.json`.

## Notes & limitations

- Facebook's DOM is obfuscated and changes often. `helpers.py` deliberately uses
  role/label/aria selectors instead of CSS classes, but breakage is still likely
  over time. If a run does nothing or errors on every thread, the selectors
  probably need updating.
- The classifier is heuristic. When unsure it errs toward `LEAVE`/`FLAGGED`
  rather than deleting — review flagged rows yourself.
- Timestamps are parsed from Messenger's `aria-label`s, which only show relative
  forms (weekday, "yesterday") for recent messages; older ones include the date.

## License

MIT — see [LICENSE](LICENSE).
