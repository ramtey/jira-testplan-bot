"""What the feature flags a ticket touches actually serve, per test environment.

A plan that guesses flag state sends testers the wrong way. SK-2687's plan
told them to toggle `inPersonSigning` per user in LaunchDarkly — a write QA is
not allowed to make — while a hand-run plan for the same ticket read the flag
and found integ serves it to everyone (fallthrough true), so the flag-off case
cannot run there at all, and staging serves it only to subscriber 3356.

This module only reads. Configuration names which LaunchDarkly project and
environments belong to which code repo (``LD_FLAG_SOURCES``)::

    {"acme/web-ui": {"project": "web",
                     "envs": {"integ": "integ", "prod": "production"}}}

``envs`` maps the label the plan uses (matching DEPLOY_STATE_SOURCES) to the
LaunchDarkly environment key. ``LAUNCHDARKLY_API_TOKEN`` is a read-only API
access token.

Which flags a ticket touches is found by matching the project's flag keys —
as written, or camelCased the way the React SDK exposes them — against the
changed code and the ticket text. A read that fails is "could not check",
never "off".
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

LD_API = "https://app.launchdarkly.com/api/v2"
MAX_FLAGS = 12
_KEY_CACHE_TTL_S = 600
_key_cache: dict[str, tuple[float, list[str]]] = {}


def camel(key: str) -> str:
    """`is-file-sharing-enabled` -> `isFileSharingEnabled` (React SDK form)."""
    parts = [p for p in re.split(r"[-_.]", key) if p]
    if not parts:
        return key
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _is_multi_word(key: str) -> bool:
    return bool(re.search(r"[-_.]", key) or re.search(r"[a-z][A-Z]", key))


def find_flag_keys(texts: list[str], keys: list[str], limit: int = MAX_FLAGS) -> list[str]:
    """Flag keys referenced in `texts`, in order of first appearance.

    A multi-word key matches as written or camelCased, on identifier
    boundaries. A single-word key (`debug`, `beta`) matches only inside quotes —
    as a bare word it is almost always something else.
    """
    blob = "\n".join(t for t in texts if t)
    found: list[tuple[int, str]] = []
    for key in keys:
        if _is_multi_word(key):
            forms = {key, camel(key)}
            pat = r"(?<![\w-])(?:" + "|".join(re.escape(f) for f in forms) + r")(?![\w-])"
        else:
            pat = r"""["'`]""" + re.escape(key) + r"""["'`]"""
        m = re.search(pat, blob)
        if m:
            found.append((m.start(), key))
    return [k for _, k in sorted(found)][:limit]


class LaunchDarklyReader:
    """The two read calls this module needs. Nothing here writes."""

    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self.token = token
        self.client = client

    async def _get(self, path: str, params=None) -> dict | None:
        headers = {"Authorization": self.token}
        try:
            if self.client:
                r = await self.client.get(f"{LD_API}{path}", params=params, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=20) as c:
                    r = await c.get(f"{LD_API}{path}", params=params, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("LaunchDarkly read %s failed: %s", path, e)
            return None
        if r.status_code != 200:
            logger.warning("LaunchDarkly read %s returned %s", path, r.status_code)
            return None
        return r.json()

    async def flag_keys(self, project: str) -> list[str] | None:
        cached = _key_cache.get(project)
        if cached and time.monotonic() - cached[0] < _KEY_CACHE_TTL_S:
            return cached[1]
        keys: list[str] = []
        offset = 0
        while True:
            page = await self._get(f"/flags/{project}",
                                   {"summary": "true", "limit": 100, "offset": offset})
            if page is None:
                return None
            items = page.get("items") or []
            keys += [i["key"] for i in items if i.get("key")]
            offset += len(items)
            if not items or offset >= page.get("totalCount", 0):
                break
        _key_cache[project] = (time.monotonic(), keys)
        return keys

    async def flag(self, project: str, key: str, env_keys: list[str]) -> dict | None:
        return await self._get(f"/flags/{project}/{key}", [("env", e) for e in env_keys])


def _value(flag: dict, idx) -> str:
    try:
        v = flag["variations"][idx]["value"]
    except (KeyError, IndexError, TypeError):
        return "?"
    return str(v).lower() if isinstance(v, bool) else repr(v)


def _serve(flag: dict, block: dict) -> str:
    """What a fallthrough or rule serves: one variation, or a percentage split."""
    if not block:
        return "?"
    if "variation" in block:
        return _value(flag, block["variation"])
    split = (block.get("rollout") or {}).get("variations") or []
    if split:
        return " / ".join(f"{w['weight'] / 1000:g}% {_value(flag, w['variation'])}" for w in split)
    return "?"


_EMAIL_RE = re.compile(r"^[^@\s]+@([^@\s]+)$")


def _clause_values(values: list) -> str:
    """A rule's values, safe to put in a plan that gets posted to Jira.

    Targeting rules list real people. Internal addresses stay — a test account
    like 123DevBroker@ is exactly what a tester needs to see — and every other
    address becomes a count, so no customer email reaches a prompt or a ticket.
    """
    from .config import settings
    internal = {d.lower().lstrip("@") for d in settings.ld_internal_email_domains or []}
    shown, hidden = [], 0
    for v in dict.fromkeys(map(str, values)):
        m = _EMAIL_RE.match(v)
        if m and m.group(1).lower() not in internal:
            hidden += 1
        else:
            shown.append(v)
    text = ", ".join(shown[:5]) + (", …" if len(shown) > 5 else "")
    if hidden:
        text += f"{', ' if text else ''}{hidden} other address(es)"
    return text


def summarize_env(flag: dict, env: dict) -> str:
    """One line a tester can act on, e.g.
    'ON: subscriberId in [3356] -> true; everyone else -> false'."""
    if not env.get("on"):
        return f"OFF: everyone gets {_value(flag, env.get('offVariation'))}"
    bits = []
    for t in (env.get("targets") or []) + (env.get("contextTargets") or []):
        n = len(t.get("values") or [])
        if n:
            bits.append(f"{n} individually targeted -> {_value(flag, t.get('variation'))}")
    for rule in env.get("rules") or []:
        clauses = " AND ".join(
            f"{c.get('attribute')} {'not ' if c.get('negate') else ''}{c.get('op')} "
            f"[{_clause_values(c.get('values') or [])}]"
            for c in rule.get("clauses") or []
        )
        bits.append(f"{clauses} -> {_serve(flag, rule)}")
    for p in env.get("prerequisites") or []:
        bits.append(f"requires flag {p.get('key')}")
    rest = _serve(flag, env.get("fallthrough") or {})
    bits.append(f"{'everyone else' if bits else 'everyone'} -> {rest}")
    return "ON: " + "; ".join(bits)


async def read_flag_state(reader, texts_by_repo: dict[str, list[str]], sources: dict) -> list[dict]:
    """One entry per (project, flag) found, or one error entry per project.

    Entry: {"project", "key", "name", "envs": {label: line}, "error"}.
    """
    by_project: dict[str, dict] = {}
    for repo, texts in texts_by_repo.items():
        src = sources.get(repo)
        if not src:
            continue
        slot = by_project.setdefault(src["project"], {"envs": src["envs"], "texts": []})
        slot["texts"] += texts

    async def one_project(project: str, slot: dict) -> list[dict]:
        keys = await reader.flag_keys(project)
        if keys is None:
            return [{"project": project, "key": None, "name": None, "envs": {},
                     "error": "could not list this project's flags"}]
        wanted = find_flag_keys(slot["texts"], keys)
        labels = slot["envs"]
        flags = await asyncio.gather(
            *(reader.flag(project, k, list(labels.values())) for k in wanted))
        out = []
        for key, flag in zip(wanted, flags):
            if flag is None:
                out.append({"project": project, "key": key, "name": None, "envs": {},
                            "error": "could not read this flag"})
                continue
            envs = {}
            for label, ld_env in labels.items():
                env = (flag.get("environments") or {}).get(ld_env)
                envs[label] = summarize_env(flag, env) if env else "COULD NOT CHECK"
            out.append({"project": project, "key": key, "name": flag.get("name"),
                        "envs": envs, "error": None})
        return out

    results = await asyncio.gather(*(one_project(p, s) for p, s in by_project.items()))
    return [e for group in results for e in group]


def render_flag_state(entries: list[dict]) -> str:
    if not entries:
        return ""
    lines = ["Read from LaunchDarkly at generation time. Flags change; this is a snapshot."]
    for e in entries:
        if e["key"] is None:
            lines.append(f"- {e['project']}: COULD NOT CHECK ({e['error']}). Do not state "
                         "any flag's value for this project.")
            continue
        if e["error"]:
            lines.append(f"- {e['key']} ({e['project']}): COULD NOT CHECK ({e['error']})")
            continue
        lines.append(f"- {e['key']} ({e['project']}{', ' + e['name'] if e['name'] else ''}):")
        for label, summary in e["envs"].items():
            lines.append(f"    {label}: {summary}")
    return "\n".join(lines)


FLAG_STATE_GUIDANCE = (
    "Flag state above is read-only context. NEVER tell a tester to change a flag "
    "in LaunchDarkly. To test a flag state, choose an environment and a user who "
    "already gets it under the rules shown, and name both. When an environment "
    "serves one value to everyone, the other path cannot be tested there — say "
    "where it can be, or mark the case BLOCKED and name the account it needs. "
    "COULD NOT CHECK means unknown: say so, and do not assume on or off."
)
