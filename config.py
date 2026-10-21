"""Configuration for the Marketplace cleanup tool.

Loads `config.toml` from the project root. Every value has a baked-in default
(below), so the tool runs out of the box; `config.toml` only needs to specify
the keys you want to override. Edit `config.toml` to change the entry point,
the classifier thresholds, batch pacing, or the language phrase banks.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"


# ----- default phrase banks -----
# Matched case-insensitively as substrings of message text. Defaults are tuned
# for English + Filipino (Taglish) Marketplace chats. Override in config.toml
# to supply your own language(s).

DEFAULT_SOLD_PHRASES = [
    "sold na", "sold po", "sold n po", "sold out", "nabenta na",
    "taken na", "taken po", "may bumili na", "naibenta na",
    "na-reserve", "nareserve", "reserved na", "naka-reserve",
    "no longer available", "not available na",
    "sold to someone", "given na", "ibinigay na",
]

DEFAULT_CLOSURE_PHRASES = [
    "received na", "natanggap ko na", "narecieve ko na", "narecieved",
    "delivered na", "delivered, salamat", "delivered po",
    "ayos na", "ok na po, salamat", "ok na, thanks", "okay na po salamat",
    "thank you, enjoy", "thanks, enjoy", "enjoy the item",
    "rated 5 stars", "5-star rating", "rated you 5",
    "payment received", "received payment", "received the payment",
    "natanggap ko na ang bayad", "na-receive ko na ang bayad",
    "transaction complete", "deal closed",
    "salamat po sa", "thanks po sa transaction",
]

DEFAULT_INQUIRY_NO_REPLY_PHRASES = [
    "available pa po ba", "available pa po", "available pa ba",
    "still available", "still avail", "is this still available",
    "how much po", "magkano po", "magkano na lang",
    "interested po", "interested ako", "po interested",
    "pa-reserve", "pwedeng tingnan",
]

DEFAULT_COORDINATION_PHRASES = [
    "meetup", "meet up", "magkita", "saan tayo",
    "cod", "c.o.d", "cash on delivery", "j&t", "lalamove",
    "gcash", "g-cash", "send mo sa", "paypal",
    "ship", "lbc", "ninja van", "shopee xpress",
    "pickup", "pick up", "pick-up",
    "pa-reserve", "reserve mo",
]


@dataclass
class Config:
    # ----- browser / auth -----
    # Any Facebook/Messenger page works; it's only used to confirm login.
    entry_url: str = "https://www.facebook.com/messages"
    profile_dir: Path = Path("./fb-cleanup-profile")

    # ----- output files -----
    plan_path: Path = Path("./marketplace-cleanup-plan.md")
    log_path: Path = Path("./marketplace-cleanup-log.md")
    enum_cache: Path = Path("./marketplace-enum.json")

    # ----- classifier thresholds (days) -----
    recent_days: float = 14.0      # last message within this window => LEAVE
    gray_zone_days: float = 30.0   # between recent and this => flag for review

    # ----- execute pacing -----
    batch_size: int = 5
    batch_pause_seconds: float = 5.0

    # ----- sweep.py defaults -----
    sweep_threshold_days: float = 14.0
    sweep_max_actions: int = 300

    # ----- phrase banks -----
    sold_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_SOLD_PHRASES))
    closure_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_CLOSURE_PHRASES))
    inquiry_no_reply_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_INQUIRY_NO_REPLY_PHRASES))
    coordination_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_COORDINATION_PHRASES))


def _resolve(path_str: str, base: Path) -> Path:
    p = Path(path_str).expanduser()
    return p if p.is_absolute() else (base / p)


def load(path: Path = CONFIG_PATH) -> Config:
    """Load config.toml over the defaults. Missing file or keys fall back."""
    cfg = Config()
    if not path.exists():
        return cfg

    data = tomllib.loads(path.read_text())
    base = path.parent

    browser = data.get("browser", {})
    cfg.entry_url = browser.get("entry_url", cfg.entry_url)
    if "profile_dir" in browser:
        cfg.profile_dir = _resolve(browser["profile_dir"], base)

    output = data.get("output", {})
    if "plan_path" in output:
        cfg.plan_path = _resolve(output["plan_path"], base)
    if "log_path" in output:
        cfg.log_path = _resolve(output["log_path"], base)
    if "enum_cache" in output:
        cfg.enum_cache = _resolve(output["enum_cache"], base)

    classifier = data.get("classifier", {})
    cfg.recent_days = float(classifier.get("recent_days", cfg.recent_days))
    cfg.gray_zone_days = float(classifier.get("gray_zone_days", cfg.gray_zone_days))

    execute = data.get("execute", {})
    cfg.batch_size = int(execute.get("batch_size", cfg.batch_size))
    cfg.batch_pause_seconds = float(execute.get("batch_pause_seconds", cfg.batch_pause_seconds))

    sweep = data.get("sweep", {})
    cfg.sweep_threshold_days = float(sweep.get("threshold_days", cfg.sweep_threshold_days))
    cfg.sweep_max_actions = int(sweep.get("max_actions", cfg.sweep_max_actions))

    phrases = data.get("phrases", {})
    if "sold" in phrases:
        cfg.sold_phrases = [str(p).lower() for p in phrases["sold"]]
    if "closure" in phrases:
        cfg.closure_phrases = [str(p).lower() for p in phrases["closure"]]
    if "inquiry_no_reply" in phrases:
        cfg.inquiry_no_reply_phrases = [str(p).lower() for p in phrases["inquiry_no_reply"]]
    if "coordination" in phrases:
        cfg.coordination_phrases = [str(p).lower() for p in phrases["coordination"]]

    return cfg


# Sanity check at import-time-free call sites: ensure all dataclass fields are
# covered by load() above if the schema grows.
_KNOWN_FIELDS = {f.name for f in fields(Config)}
