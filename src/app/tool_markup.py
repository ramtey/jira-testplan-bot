"""Rule AL: tool-call markup never reaches a stored plan field.

SK-3014 (plan 734 v1) stored ``how_to_see_it`` as the string
``'\\n<parameter name="summary">Nothing changes on screen. …'``: the model
wrote the object as a nested tool-call parameter instead of JSON, the API
handed that back as a plain string value, and every renderer — the Pass-to-UAT
form, the Loom description, the Jira comment — showed the envelope to the PM.
``reason`` was lost entirely.

Two halves:

``normalize_plan_fields`` (sync; at parse time and again before save)
    Walks every free-text field. A string carrying a ``<parameter name="x">``
    envelope is split back into the fields it names (the tool-call arguments
    the model meant to send); a string where the schema wants an object is
    rebuilt into that object; any remaining envelope tag (``<parameter``,
    ``</parameter>``, ``<invoke``, ``<function_calls>``, with or without a
    namespace prefix) is stripped, and so is leading/trailing whitespace.
    Returns the fields that are still not usable — empty once the markup is
    gone, or an object recovered from markup that lacks a required field.

``recheck_tool_markup`` (async; the last re-check before a plan is saved)
    Runs the normalizer over the finished body, then re-asks the model for
    each field that failed, validates the answer the same way, and records
    every fix in ``source_provenance["plan_recheck"]["findings"]`` (rule AL).
    A field the re-ask could not repair is left clean but incomplete and
    marked — it is never saved with markup in it.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

RULE = "AL"

# Tool-call envelope tags, with or without a namespace prefix ("antml:" etc.).
_TAG_NAMES = r"(?:parameter|parameters|invoke|function_calls|function_results|tool_use|tool_call|tool_result)"
_TAG_RE = re.compile(r"</?\s*(?:[A-Za-z_][\w-]*:)?" + _TAG_NAMES + r"\b[^>]*>", re.IGNORECASE)
_PARAM_OPEN_RE = re.compile(
    r"<\s*(?:[A-Za-z_][\w-]*:)?parameter\s+name\s*=\s*[\"']([^\"']*)[\"']\s*>", re.IGNORECASE)

# Body keys the model did not write, or that are rebuilt after this pass.
_SKIP_KEYS = frozenset({
    "source_provenance", "progress_remap", "video_walkthrough", "pr_state_refresh",
    "ticket_key", "ticket_keys", "plan_shape", "batch_host_key", "plan_id", "version",
})

# Re-asks per plan. A plan where more than this many fields came back as pure
# markup is a broken generation, and the rest are marked rather than re-asked.
MAX_REASKS = 6


def has_tool_markup(text: Any) -> bool:
    return isinstance(text, str) and bool(_TAG_RE.search(text))


def strip_tool_markup(text: str) -> str:
    """``text`` without any envelope tag and without outer whitespace."""
    return _TAG_RE.sub("", text).strip()


def _split_envelope(text: str) -> tuple[str, dict[str, str]]:
    """``(lead, {name: value})`` for a string carrying ``<parameter name=…>``.

    ``lead`` is the text before the first named parameter. Every value is
    stripped of remaining tags and whitespace.
    """
    parts = _PARAM_OPEN_RE.split(text)
    lead = strip_tool_markup(parts[0])
    named: dict[str, str] = {}
    for name, value in zip(parts[1::2], parts[2::2]):
        value = strip_tool_markup(value)
        if value and not named.get(name):
            named[name] = value
    return lead, named


def _props(schema: dict | None) -> dict:
    return (schema or {}).get("properties") or {}


def _required_strings(schema: dict | None) -> list[str]:
    props = _props(schema)
    return [k for k in (schema or {}).get("required") or []
            if (props.get(k) or {}).get("type") == "string"]


def _object_from_string(text: str, schema: dict) -> dict:
    """An object the schema wants, rebuilt from the string the model sent."""
    lead, named = _split_envelope(text)
    props = _props(schema)
    obj = {k: v for k, v in named.items() if not props or k in props}
    if lead:
        first = next((k for k in _required_strings(schema) if not obj.get(k)), None) \
            or next((k for k, p in props.items() if p.get("type") == "string" and not obj.get(k)), None)
        if first:
            obj[first] = lead
    return obj


class _Walk:
    def __init__(self) -> None:
        self.fixed: list[list] = []      # paths whose markup was removed
        self.failures: list[dict] = []   # paths still unusable
        self.trimmed = 0                 # strings that only lost outer whitespace

    def value(self, value: Any, schema: dict | None, path: list, key: str | None) -> tuple[Any, dict]:
        """``(clean value, extras)``; extras are sibling fields an envelope named."""
        schema = schema or {}
        if isinstance(value, str):
            if schema.get("type") == "object":
                obj = _object_from_string(value, schema)
                self.fixed.append(path)
                cleaned, _ = self.value(obj, schema, path, key)
                self._require(cleaned, schema, path)
                return cleaned, {}
            if not has_tool_markup(value):
                stripped = value.strip()
                if stripped != value:
                    self.trimmed += 1
                return stripped, {}
            self.fixed.append(path)
            lead, named = _split_envelope(value)
            own = named.pop(key, "") if key else ""
            # A string at a list position cannot hand fields to a sibling, so
            # a named value with nowhere to go stays with the text it came in.
            if key is None:
                own = " ".join([lead, *named.values()]).strip()
                named = {}
            else:
                own = " ".join(p for p in (lead, own) if p).strip()
            if not own and value.strip():
                self.failures.append({"path": path, "reason": "nothing but tool-call markup"})
            return own, named
        if isinstance(value, list):
            item_schema = schema.get("items")
            out = []
            for i, item in enumerate(value):
                cleaned, _ = self.value(item, item_schema, path + [i], None)
                out.append(cleaned)
            value[:] = out
            return value, {}
        if isinstance(value, dict):
            props = _props(schema)
            split = False
            extras: dict = {}
            for k in list(value):
                if not path and k in _SKIP_KEYS:
                    continue
                cleaned, named = self.value(value[k], props.get(k), path + [k], k)
                value[k] = cleaned
                split = split or bool(named)
                extras.update(named)
            for k, v in extras.items():
                if (not props or k in props) and not value.get(k):
                    value[k] = v
            # Held to its required fields: an object whose fields an envelope
            # ran together, and every top-level object of the plan — the
            # parse-time pass may already have unwrapped the envelope that
            # lost a field (SK-3014's ``reason``), so by save time there is no
            # markup left to point at it. A case the model simply left thin
            # is another rule's business.
            if (path and split) or len(path) == 1:
                self._require(value, schema, path)
            return value, {}
        return value, {}

    def _require(self, obj: dict, schema: dict, path: list) -> None:
        for k in _required_strings(schema):
            if not (isinstance(obj.get(k), str) and obj[k].strip()):
                if not any(f["path"] == path + [k] for f in self.failures):
                    self.failures.append({"path": path + [k], "reason": "lost inside tool-call markup"})


def _fold_stray_arguments(data: dict, schema: dict | None, walk: "_Walk") -> None:
    """Put back top-level arguments that belong inside an object field.

    The envelope that broke SK-3014 closes ``how_to_see_it`` after
    ``summary`` and sends ``reason`` as a top-level argument of its own (seen
    in the SK-2826 eval, 2026-11-18: non-empty fields ``how_to_see_it, reason,
    uat_complexity``). The plan schema has no top-level ``reason``, so it was
    dropped and the field saved half-empty. A top-level argument the schema
    does not know, whose name is a property of an object field that arrived
    as a string or lacks it, is moved back into that field.
    """
    props = _props(schema)
    stray = [k for k in data if k not in props and k not in _SKIP_KEYS] if props else []
    for field, fschema in props.items():
        value = data.get(field)
        if (fschema or {}).get("type") != "object" or not isinstance(value, (str, dict)):
            continue
        movable = [k for k in stray if k in _props(fschema)
                   and isinstance(data.get(k), str) and data[k].strip()]
        if not movable:
            continue
        if isinstance(value, str):
            if not has_tool_markup(value):
                continue
            value = _object_from_string(value, fschema)
            walk.fixed.append([field])
        for k in movable:
            if not value.get(k):
                value[k] = data.pop(k)
                stray.remove(k)
                walk.fixed.append([field, k])
        data[field] = value


def normalize_plan_fields(data: dict, schema: dict | None = None) -> dict:
    """Clean ``data`` in place. Returns ``{"fixed", "failures", "trimmed"}``."""
    walk = _Walk()
    if isinstance(data, dict):
        _fold_stray_arguments(data, schema, walk)
        walk.value(data, schema or {}, [], None)
    return {"fixed": walk.fixed, "failures": walk.failures, "trimmed": walk.trimmed}


def schema_at(schema: dict | None, path: list) -> dict | None:
    node = schema
    for step in path:
        if node is None:
            return None
        node = node.get("items") if isinstance(step, int) else _props(node).get(step)
    return node


def _get(data: Any, path: list) -> Any:
    for step in path:
        try:
            data = data[step]
        except (KeyError, IndexError, TypeError):
            return None
    return data


def _set(data: Any, path: list, value: Any) -> None:
    for step in path[:-1]:
        data = data[step]
    data[path[-1]] = value


def path_label(path: list) -> str:
    out = ""
    for step in path:
        out += f"[{step}]" if isinstance(step, int) else (f".{step}" if out else str(step))
    return out


def _case_title(body: dict, path: list) -> str | None:
    if len(path) >= 2 and isinstance(path[1], int):
        case = _get(body, path[:2])
        if isinstance(case, dict) and isinstance(case.get("title"), str):
            return case["title"]
    return None


def _reask_context(body: dict, path: list) -> str:
    """What the model needs to rewrite one field: the plan's gist and the
    object the field sits in, both already clean."""
    gist = {
        "ticket": body.get("ticket_key") or body.get("ticket_keys"),
        "uat_complexity": body.get("uat_complexity"),
        "happy_path": [
            {"title": c.get("title"), "expected": c.get("expected")}
            for c in (body.get("happy_path") or [])[:8] if isinstance(c, dict)
        ],
    }
    parent = _get(body, path[:-1]) if len(path) > 1 else None
    text = "PLAN SO FAR:\n" + json.dumps(gist, indent=1)[:6000]
    if isinstance(parent, (dict, list)):
        text += "\n\nTHE OBJECT THIS FIELD BELONGS TO:\n" + json.dumps(parent, indent=1)[:4000]
    return text


def _fallback_schema(current: Any) -> dict:
    return {"type": "object"} if isinstance(current, dict) else {"type": "string"}


async def recheck_tool_markup(llm: Any, body: dict, report: dict, *, schema: dict | None) -> dict:
    """Rule AL before save: clean every field, re-ask for the ones that are
    still unusable, and record what was done in ``report["findings"]``."""
    result = normalize_plan_fields(body, schema)
    findings = report.setdefault("findings", [])
    for path in result["fixed"]:
        findings.append({
            "rule": RULE, "action": "fixed", "section": path[0] if path else None,
            "field": path_label(path), "title": _case_title(body, path),
            "reason": "tool-call markup removed from the field",
        })
    regenerate = getattr(llm, "regenerate_plan_field", None)
    reasked, unresolved = [], []
    for i, failure in enumerate(result["failures"]):
        path = failure["path"]
        field_schema = schema_at(schema, path) or _fallback_schema(_get(body, path))
        value = None
        if regenerate is not None and i < MAX_REASKS:
            try:
                value = await regenerate(path_label(path), field_schema, _reask_context(body, path))
            except Exception:  # a re-ask that cannot run leaves the field marked
                logger.warning("re-ask for %s failed", path_label(path), exc_info=True)
                value = None
        if value is not None:
            probe = {"v": copy.deepcopy(value)}
            check = normalize_plan_fields(probe, {"type": "object", "properties": {"v": field_schema},
                                                  "required": ["v"] if field_schema.get("type") == "string" else []})
            value = probe["v"]
            if check["failures"] or check["fixed"] or value in ("", None, {}, []):
                value = None
        if value is not None:
            _set(body, path, value)
            reasked.append(path_label(path))
            findings.append({
                "rule": RULE, "action": "fixed", "section": path[0], "field": path_label(path),
                "title": _case_title(body, path),
                "reason": f"{failure['reason']}; re-asked the model for the field",
            })
        else:
            unresolved.append(path_label(path))
            findings.append({
                "rule": RULE, "action": "marked", "section": path[0], "field": path_label(path),
                "title": _case_title(body, path),
                "reason": f"`{path_label(path)}` came back as {failure['reason']} and the re-ask "
                          "did not produce a clean value; the field is left without it",
            })
    summary = {"fixed": len(result["fixed"]), "reasked": reasked, "unresolved": unresolved,
               "whitespace_trimmed": result["trimmed"]}
    if result["fixed"] or result["failures"]:
        report["tool_markup"] = summary
        logger.warning("tool-call markup in plan fields: %s", summary)
    return summary
