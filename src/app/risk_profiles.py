"""Product risk profiles: QA-written product knowledge injected per ticket.

Why this module exists
----------------------
The generator plans from the ticket, its PRs and its comments. What it
cannot see is the product knowledge that lives in testers' heads: which
user types behave differently, which unrelated features keep breaking when
a certain kind of code changes, which environment cannot run which flow.

SK-2620 (Forms single-file sharing) is the worked example. The batch plan
for its seven stories had 44 sound cases and still missed every bug QA
found in UAT — Remind & Resend broken for the owner, the "My Groups"
contact search slow and wrong, Apply Template on a shared file, and the
standalone (no subID) user who is outside every brokerage rule. None of
those were in a ticket, PR or comment. Each was in a tester's head, a Slack
thread, or the bug history of a different project.

A profile is a markdown file a QA team writes for one product. YAML front
matter says which tickets it applies to; the body is handed to the model
verbatim, under a block that says when it overrides the prompt's
"only test what the ticket mentions" rules. Profiles live OUTSIDE this repo
(``RISK_PROFILES_DIR``), because the repo is public and the knowledge in
them is team-specific.

Front matter::

    ---
    name: Forms
    match:
      repos: ["files-ui", "files-api", "forms-"]      # substring of owner/repo
      summary: ["\\\\[Forms\\\\]", "\\\\bnet sheet\\\\b"]   # regex, case-insensitive
      exclude_summary: ["^\\\\[(DS3|DIGI)\\\\]"]           # regex; wins over summary
    ---

A ticket matches when any linked PR's repository contains one of
``repos``, or its summary matches one of ``summary`` and none of
``exclude_summary``. A repo match is not overridden by ``exclude_summary``
— a ticket tagged ``[DS3]`` whose PR changed ``files-api`` changed Forms.

Trigger ids
-----------
The body names its triggers ``T1``, ``T1a`` … and its sections ``§2``. The
model tags every case it takes from a profile ``[<name> risk profile · <id>]``.
:func:`profile_tag_is_known` checks that tag against the loaded profiles, so
quarantine can treat a tagged case as having a stated pass condition —
the profile is QA's spec for it — without letting an invented tag through.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# `T1`, `T1a`, `T10`, `§2` — the ids a profile body defines and a case cites.
_TRIGGER_ID = r"(?:T\d+[a-z]?|§\d+)"
_DEFINED_ID_RE = re.compile(rf"(?m)^#{{2,4}}\s+({_TRIGGER_ID})\b|^\*\*({_TRIGGER_ID})\.|^##\s+(\d+)\.")
_TAG_RE = re.compile(rf"\[([^\]\[]+?) risk profile · ({_TRIGGER_ID})\]")


@dataclass
class RiskProfile:
    name: str
    body: str
    path: Path
    repos: list[str] = field(default_factory=list)
    summary: list[re.Pattern] = field(default_factory=list)
    exclude_summary: list[re.Pattern] = field(default_factory=list)
    ids: set[str] = field(default_factory=set)

    def matches(self, summary: str | None, development_info: dict | None) -> bool:
        for repo in _repositories(development_info):
            if any(r.lower() in repo for r in self.repos):
                return True
        text = summary or ""
        if any(p.search(text) for p in self.exclude_summary):
            return False
        return any(p.search(text) for p in self.summary)


def _repositories(development_info: dict | None) -> list[str]:
    """Lower-cased ``owner/repo`` (or PR URL, when no repository) per PR."""
    out = []
    for pr in (development_info or {}).get("pull_requests") or []:
        if not isinstance(pr, dict):
            continue
        repo = pr.get("repository") or pr.get("url") or ""
        if repo:
            out.append(str(repo).lower())
    return out


def _defined_ids(body: str) -> set[str]:
    ids: set[str] = set()
    for m in _DEFINED_ID_RE.finditer(body):
        if m.group(1) or m.group(2):
            ids.add(m.group(1) or m.group(2))
        elif m.group(3):
            ids.add(f"§{m.group(3)}")
    return ids


def parse_profile(text: str, path: Path) -> RiskProfile | None:
    """Parse one profile file. Returns None (and logs) if it is unusable."""
    if not text.startswith("---"):
        logger.warning("risk profile %s has no front matter; skipped", path)
        return None
    try:
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
    except (ValueError, yaml.YAMLError) as e:
        logger.warning("risk profile %s: unreadable front matter (%s); skipped", path, e)
        return None
    match = meta.get("match") or {}
    name = str(meta.get("name") or path.stem).strip()
    try:
        profile = RiskProfile(
            name=name,
            body=body.strip(),
            path=path,
            repos=[str(r) for r in match.get("repos") or []],
            summary=[re.compile(p, re.I) for p in match.get("summary") or []],
            exclude_summary=[re.compile(p, re.I) for p in match.get("exclude_summary") or []],
        )
    except re.error as e:
        logger.warning("risk profile %s: bad match pattern (%s); skipped", path, e)
        return None
    if not (profile.repos or profile.summary):
        logger.warning("risk profile %s matches nothing (no repos or summary); skipped", path)
        return None
    profile.ids = _defined_ids(profile.body)
    return profile


def load_profiles(directory: str | Path | None) -> list[RiskProfile]:
    """Every ``*.md`` profile in ``directory``. Empty when unset or missing."""
    if not directory:
        return []
    root = Path(directory).expanduser()
    if not root.is_dir():
        logger.warning("RISK_PROFILES_DIR %s is not a directory; no profiles loaded", root)
        return []
    profiles = []
    for path in sorted(root.glob("*.md")):
        profile = parse_profile(path.read_text(encoding="utf-8"), path)
        if profile is not None:
            profiles.append(profile)
    return profiles


def configured_profiles() -> list[RiskProfile]:
    # Read per call, not cached: QA edits a profile between runs and expects
    # the next plan to use it, and a handful of small files costs nothing.
    from .config import settings

    return load_profiles(getattr(settings, "risk_profiles_dir", None))


def match_profiles(
    *,
    summary: str | None,
    development_info: dict | None,
    profiles: list[RiskProfile] | None = None,
) -> list[RiskProfile]:
    pool = configured_profiles() if profiles is None else profiles
    return [p for p in pool if p.matches(summary, development_info)]


def match_profiles_for_batch(tickets: list[dict], profiles: list[RiskProfile] | None = None) -> list[RiskProfile]:
    """Profiles matching ANY ticket in a batch, each once, in load order."""
    pool = configured_profiles() if profiles is None else profiles
    return [
        p for p in pool
        if any(p.matches(t.get("summary"), t.get("development_info")) for t in tickets)
    ]


def profile_tag_is_known(title: str | None, profiles: list[RiskProfile] | None = None) -> bool:
    """Whether a case title carries a tag naming a real profile id.

    ``[Forms risk profile · T1a]`` counts only if a loaded profile is named
    Forms and defines T1a. An invented id is not a pass condition.
    """
    if not title:
        return False
    pool = configured_profiles() if profiles is None else profiles
    by_name = {p.name.lower(): p for p in pool}
    for name, tag_id in _TAG_RE.findall(title):
        profile = by_name.get(name.strip().lower())
        if profile is not None and tag_id in profile.ids:
            return True
    return False


def render_risk_profile_guidance(profiles: list[RiskProfile]) -> str:
    """The prompt block for every matched profile. Empty when none matched."""
    if not profiles:
        return ""
    lines: list[str] = ["", "━" * 69, "📒 PRODUCT RISK PROFILE — QA-WRITTEN PRODUCT KNOWLEDGE", "━" * 69, ""]
    lines.append(
        "This ticket belongs to a product whose QA team wrote down what the "
        "ticket cannot tell you: which user types behave differently, which "
        "other features break when a given kind of code changes, and which "
        "environments cannot run which flows. It was built from that product's "
        "escaped bugs. Treat it as a source, with the same standing as the "
        "ticket: a case taken from it is grounded in it."
    )
    lines.append("")
    lines.append("**How to apply it — read all of this before using the profile:**")
    lines.append(
        "1. **Fire triggers from evidence, not from vocabulary.** Each trigger "
        "(T1a, T2, …) states a signal. Apply a trigger only when that signal is "
        "in the diff, the PR description or the ticket's ACs. A ticket that "
        "merely mentions \"user\" or \"permission\" fires nothing. Most tickets "
        "fire zero or one trigger. If none fire, use only the environment "
        "section and add nothing else from the profile."
    )
    lines.append(
        "2. **A fired trigger adds its own items, not the whole profile.** Expect "
        "2–4 additions per fired trigger. Choose the items in it that the "
        "change could plausibly break."
    )
    lines.append(
        "3. **User-type rows (§2) only when a trigger that names them fires.** "
        "Then pick the 2–4 rows the change could behave differently for, plus "
        "the row the ticket was written for. Never every row."
    )
    lines.append(
        "4. **Where profile items go.** A user-type variant of the behaviour "
        "this ticket changed is an `edge_cases` case. A check on ANOTHER feature "
        "the change might have broken (e.g. Remind & Resend after a sharing "
        "change) is a `regression_checklist` line. A step the profile says an "
        "environment cannot run is still written, with the blocker stated in "
        "its preconditions — never silently dropped and never written as "
        "runnable."
    )
    lines.append(
        "5. **Tag every case and line you take from a profile** by starting its "
        "title (or the checklist line) with `[<profile name> risk profile · <id>]`, "
        "where <id> is the trigger or section it came from — e.g. "
        "`[Forms risk profile · T1a] Remind & Resend still sends as the owner`. "
        "Use only ids the profile defines. An untagged case is judged as "
        "ticket-derived; a tag with an invented id is treated as untagged."
    )
    lines.append(
        "6. **Expected results come from the profile or the spec it cites.** If "
        "the profile marks an expectation as needing a decision, write the case "
        "with the open question in its expected result instead of guessing. "
        "Never assert the opposite of what the profile states as by-design."
    )
    lines.append("")
    lines.append(
        "**These rules override, for profile-tagged items only:** \"ONLY test "
        "what is explicitly mentioned\" (the profile is the mention), the "
        "sibling-enumeration ban (the profile names the siblings), AVOID "
        "REDUNDANCY and PARAMETERIZE (two user types with different correct "
        "outcomes are two cases, never one), and the per-module UI cap (profile "
        "items are outside it). Everything else — grounding of UI element names, "
        "priority ordering, the flag-off legacy rule — still applies."
    )
    for profile in profiles:
        lines.append("")
        lines.append(f"══════════ {profile.name.upper()} RISK PROFILE ══════════")
        lines.append(profile.body)
        lines.append(f"══════════ END {profile.name.upper()} RISK PROFILE ══════════")
    lines.append("")
    return "\n".join(lines)


def holdout(profile: RiskProfile, keys: set[str]) -> RiskProfile:
    """The profile as it would read had ``keys`` never been mined.

    For replay evals only. Every profile line is grounded in cited tickets,
    and a profile mined from a ticket's own bounce scores that ticket by
    reading the answer back. A block citing a held-out key is dropped whole,
    even when it cites other evidence too — the harder question is whether
    the knowledge from everything else would have caught it.

    Blocks: a bullet or table row with its indented continuation lines; a
    paragraph (consecutive plain lines). A dropped paragraph ending in ":"
    takes the list it introduces with it — its bullets came from the same
    source even when they don't repeat the key (Nick's SK-2620 Phase II
    list did not, and leaked into the first Forms eval).
    """
    if not keys:
        return profile
    key_re = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(keys)) + r")\b")
    # A bullet marker needs its space: "**T1a. …**" is a paragraph, not a bullet.
    item_re = re.compile(r"\s*(?:[-*]\s|\d+\.\s|\|)")

    # Group lines into blocks: ("sep", [line]) | ("item", lines) | ("para", lines)
    blocks: list[tuple[str, list[str]]] = []
    for line in profile.body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            blocks.append(("sep", [line]))
        elif item_re.match(line):
            blocks.append(("item", [line]))
        elif line[:1] in (" ", "\t") and blocks and blocks[-1][0] == "item":
            blocks[-1][1].append(line)
        elif blocks and blocks[-1][0] == "para":
            blocks[-1][1].append(line)
        else:
            blocks.append(("para", [line]))

    out: list[str] = []
    dropping_list = False
    for kind, lines in blocks:
        cited = bool(key_re.search(" ".join(lines)))
        if kind == "sep":
            dropping_list = False
            out.extend(lines)
        elif kind == "para":
            dropping_list = cited and lines[-1].rstrip().endswith(":")
            if not cited:
                out.extend(lines)
        elif not (cited or dropping_list):
            out.extend(lines)
    body = "\n".join(out)
    return RiskProfile(
        name=profile.name,
        body=body,
        path=profile.path,
        repos=profile.repos,
        summary=profile.summary,
        exclude_summary=profile.exclude_summary,
        ids=_defined_ids(body),
    )
