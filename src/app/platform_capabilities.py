"""Rule N: what the app can run on, read from the project, and steps it cannot.

A plan for an iOS ticket can ask a tester to "rotate the device to landscape"
on an app whose iPhone build is portrait-only, or to "check the layout on
iPad" for an app that is iPhone-only. Such a step cannot pass or fail; it
only costs a tester a cycle working out why. What the app supports is
written down in the repo:

* **Device family:** ``TARGETED_DEVICE_FAMILY`` in the app target's build
  settings (``1`` iPhone, ``2`` iPad), or ``UIDeviceFamily`` in Info.plist.
* **Orientations:** ``UISupportedInterfaceOrientations`` in the app's
  Info.plist, per device with the ``~iphone`` / ``~ipad`` suffixes, or the
  ``INFOPLIST_KEY_UISupportedInterfaceOrientations[_iPhone|_iPad]`` build
  settings when Xcode generates the plist. A view controller can only narrow
  these. ``application(_:supportedInterfaceOrientationsFor:)`` in the app
  delegate replaces them, so when it is there the plist decides nothing.
* **Capabilities:** the keys in the target's ``.entitlements`` file
  (``aps-environment`` for push, ``com.apple.developer.applesignin``, ...).

The same app can differ per device: the team's iOS app is portrait-only on
iPhone and supports every orientation on iPad. A landscape step is then not
applicable on iPhone, and is a valid step on iPad, not a mistake to delete.

Like every other check here this **marks in place** and never moves or drops
a case: the section counts are the progress key. "Could not read" stays
distinct from "does not support": nothing is marked from a file that was not
read.
"""

from __future__ import annotations

import plistlib
import posixpath
import re
from typing import Any, Iterable

CASE_SECTIONS = ("happy_path", "edge_cases", "integration_tests", "security_negative_tests")

_ORIENTATIONS = {
    "UIInterfaceOrientationPortrait": "portrait",
    "UIInterfaceOrientationPortraitUpsideDown": "portrait upside down",
    "UIInterfaceOrientationLandscapeLeft": "landscape left",
    "UIInterfaceOrientationLandscapeRight": "landscape right",
}
_FAMILY = {"1": "iPhone", "2": "iPad", "3": "Apple TV", "4": "Apple Watch", "6": "Mac (Catalyst)",
           "7": "Apple Vision"}

#: Entitlement keys and what a step must say to need them.
ENTITLEMENT_NEEDS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("aps-environment", "push notifications",
     re.compile(r"\bpush notification|\bremote notification|\bAPNs\b", re.I)),
    ("com.apple.developer.applesignin", "Sign in with Apple", re.compile(r"\bSign in with Apple\b", re.I)),
    ("com.apple.developer.in-app-payments", "Apple Pay", re.compile(r"\bApple Pay\b", re.I)),
    ("com.apple.developer.healthkit", "HealthKit", re.compile(r"\bHealthKit\b|\bApple Health\b", re.I)),
    ("com.apple.developer.nfc.readersession.formats", "NFC tag reading", re.compile(r"\bNFC\b", re.I)),
    ("com.apple.developer.associated-domains", "universal links",
     re.compile(r"\buniversal link|\bassociated domain", re.I)),
    # No iCloud entry: picking a file from iCloud Drive through the Files
    # picker needs no entitlement, and a pattern cannot tell that from sync.
)


# ---------------------------------------------------------------------------
# Reading the project
# ---------------------------------------------------------------------------

def _is_app_source(path: str) -> bool:
    low = path.lower()
    return not re.search(r"(^|/)(pods|carthage|\.build|vendor|third[_-]?party)/|tests?/|uitests|"
                         r"extension|widget|watch|intent|clip", low)


def capability_files(tree: Iterable[str]) -> list[str]:
    """Info.plists, entitlements and app delegates worth reading (not the pbxproj,
    which ``build_targets.version_files`` already reads)."""
    tree = list(tree)
    plists = [p for p in tree if posixpath.basename(p) == "Info.plist" and _is_app_source(p)]
    ents = [p for p in tree if p.endswith(".entitlements") and _is_app_source(p)]
    delegates = [p for p in tree if posixpath.basename(p) in ("AppDelegate.swift", "AppDelegate.m")
                 and _is_app_source(p)]
    return sorted(plists, key=len)[:6] + sorted(ents, key=len)[:4] + sorted(delegates, key=len)[:2]


_BLOCK = re.compile(r"buildSettings = \{(.*?)\n\s*\};", re.S)


def _setting(block: str, key: str) -> str | None:
    m = re.search(rf"(?m)^\s*\"?{re.escape(key)}\"?\s*=\s*(\"(?:[^\"\\]|\\.)*\"|[^;]*);", block)
    return m.group(1).strip().strip('"') if m else None


def app_target_settings(pbxproj: str) -> list[dict]:
    """Build-settings blocks of the shipping app target (every configuration).

    A test bundle has ``TEST_HOST`` or ``BUNDLE_LOADER``; an extension or
    framework has ``SKIP_INSTALL = YES``; project-level blocks have no
    bundle identifier. What remains is the app, once per configuration.
    """
    out = []
    for m in _BLOCK.finditer(pbxproj or ""):
        b = m.group(1)
        if _setting(b, "TEST_HOST") or _setting(b, "BUNDLE_LOADER"):
            continue
        if (_setting(b, "SKIP_INSTALL") or "").upper() == "YES":
            continue
        if not _setting(b, "PRODUCT_BUNDLE_IDENTIFIER"):
            continue
        if (_setting(b, "WRAPPER_EXTENSION") or "app") not in ("app",):
            continue
        out.append({k: _setting(b, k) for k in (
            "PRODUCT_BUNDLE_IDENTIFIER", "TARGETED_DEVICE_FAMILY", "INFOPLIST_FILE",
            "CODE_SIGN_ENTITLEMENTS", "SUPPORTS_MACCATALYST",
            "INFOPLIST_KEY_UISupportedInterfaceOrientations",
            "INFOPLIST_KEY_UISupportedInterfaceOrientations_iPhone",
            "INFOPLIST_KEY_UISupportedInterfaceOrientations_iPad",
            "INFOPLIST_KEY_UIRequiresFullScreen")})
    return out


def _load_plist(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        data = plistlib.loads(text.encode("utf-8") if isinstance(text, str) else text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _orients(values: Iterable[str] | str | None) -> list[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        values = values.split()
    out = [_ORIENTATIONS[v] for v in values if v in _ORIENTATIONS]
    return out or None


def _one(values: list[str | None]) -> str | None:
    """The value every configuration agrees on; with disagreement, the union."""
    vals = [v for v in values if v]
    if not vals:
        return None
    if len(set(vals)) == 1:
        return vals[0]
    return ",".join(sorted({x.strip() for v in vals for x in v.split(",") if x.strip()}))


def parse_ios_capabilities(pbx_path: str | None, pbxproj: str | None,
                           files: dict[str, str]) -> dict | None:
    """What the app target supports, each value with the file it came from.

    ::

        {"devices": ["iPhone", "iPad"] | None, "devices_source": str,
         "orientations": {"iPhone": [...], "iPad": [...]}, "orientations_source": {device: str},
         "orientations_overridden_in": path | None,
         "entitlements": [keys] | None, "entitlements_source": path | None,
         "required_capabilities": [...], "unread": [paths]}

    None when there is no app target to read. A value that could not be
    read is None, never an empty list.
    """
    blocks = app_target_settings(pbxproj or "")
    if not blocks and not files:
        return None
    unread: list[str] = []
    cap: dict[str, Any] = {"devices": None, "devices_source": None, "orientations": {},
                           "orientations_source": {}, "orientations_overridden_in": None,
                           "entitlements": None, "entitlements_source": None,
                           "required_capabilities": [], "unread": unread}

    fam = _one([b.get("TARGETED_DEVICE_FAMILY") for b in blocks])
    if fam:
        cap["devices"] = [_FAMILY.get(f.strip(), f"family {f.strip()}") for f in fam.split(",") if f.strip()]
        cap["devices_source"] = f'{pbx_path} TARGETED_DEVICE_FAMILY = "{fam}"'

    plist_path = _one([b.get("INFOPLIST_FILE") for b in blocks])
    if plist_path and "," in plist_path:
        plist_path = None  # configurations point at different plists: read none as authoritative
    if not plist_path:
        candidates = [p for p in files if posixpath.basename(p) == "Info.plist"]
        plist_path = sorted(candidates, key=len)[0] if len(candidates) == 1 else None
    plist = None
    if plist_path:
        plist = _load_plist(files.get(plist_path))
        if plist is None:
            unread.append(plist_path)
    if plist and not cap["devices"] and plist.get("UIDeviceFamily"):
        cap["devices"] = [_FAMILY.get(str(f), f"family {f}") for f in plist["UIDeviceFamily"]]
        cap["devices_source"] = f"{plist_path} UIDeviceFamily"

    generic = suffix = None
    for device, sfx, setting in (("iPhone", "~iphone", "INFOPLIST_KEY_UISupportedInterfaceOrientations_iPhone"),
                                 ("iPad", "~ipad", "INFOPLIST_KEY_UISupportedInterfaceOrientations_iPad")):
        if plist is not None and plist.get(f"UISupportedInterfaceOrientations{sfx}"):
            suffix = _orients(plist[f"UISupportedInterfaceOrientations{sfx}"])
            src = f"{plist_path} UISupportedInterfaceOrientations{sfx}"
        elif plist is not None and plist.get("UISupportedInterfaceOrientations"):
            suffix = _orients(plist["UISupportedInterfaceOrientations"])
            src = f"{plist_path} UISupportedInterfaceOrientations"
        elif _one([b.get(setting) for b in blocks]):
            suffix = _orients(_one([b.get(setting) for b in blocks]).replace(",", " "))
            src = f"{pbx_path} {setting}"
        elif (generic := _one([b.get("INFOPLIST_KEY_UISupportedInterfaceOrientations") for b in blocks])):
            suffix = _orients(generic.replace(",", " "))
            src = f"{pbx_path} INFOPLIST_KEY_UISupportedInterfaceOrientations"
        else:
            continue
        if suffix:
            cap["orientations"][device] = suffix
            cap["orientations_source"][device] = src
    if plist is not None:
        cap["required_capabilities"] = [str(c) for c in (plist.get("UIRequiredDeviceCapabilities") or [])
                                        if isinstance(c, str)]

    for path, text in files.items():
        if posixpath.basename(path).startswith("AppDelegate") and text and \
                re.search(r"supportedInterfaceOrientationsFor|supportedInterfaceOrientationsForWindow", text):
            cap["orientations_overridden_in"] = path

    ent_path = _one([b.get("CODE_SIGN_ENTITLEMENTS") for b in blocks])
    if ent_path and "," not in ent_path:
        ent = _load_plist(files.get(ent_path))
        if ent is None:
            unread.append(ent_path)
        else:
            cap["entitlements"] = sorted(ent)
            cap["entitlements_source"] = ent_path
    return cap


# ---------------------------------------------------------------------------
# The fact the generator reads
# ---------------------------------------------------------------------------

def _orient_text(o: list[str]) -> str:
    if set(o) == set(_ORIENTATIONS.values()):
        return "every orientation"
    if o == ["portrait"]:
        return "portrait only"
    return ", ".join(o)


def supports_landscape(cap: dict, device: str) -> bool | None:
    """None when it cannot be told from the project."""
    if cap.get("orientations_overridden_in"):
        return None
    o = (cap.get("orientations") or {}).get(device)
    if o is None:
        return None
    return any(x.startswith("landscape") for x in o)


def render_capabilities(cap: dict | None) -> list[str]:
    """``PLATFORM FACT`` lines for the prompt; empty when nothing was read."""
    if not cap:
        return []
    lines = []
    if cap.get("devices"):
        lines.append(f"The app runs on {', '.join(cap['devices'])} ({cap['devices_source']})")
    if cap.get("orientations_overridden_in"):
        lines.append(f"The app's supported orientations are set in code ({cap['orientations_overridden_in']} "
                     "implements supportedInterfaceOrientationsFor), so Info.plist does not decide "
                     "them: treat orientation as unverified")
    else:
        for device, o in (cap.get("orientations") or {}).items():
            if not cap.get("devices") or device in cap["devices"]:
                lines.append(f"On {device} the app supports {_orient_text(o)} ({cap['orientations_source'][device]})")
    if cap.get("entitlements") is not None:
        lines.append(f"The app's entitlements: {', '.join(cap['entitlements']) or 'none'} ({cap['entitlements_source']})")
    if cap.get("required_capabilities"):
        lines.append(f"The app requires device capabilities: {', '.join(cap['required_capabilities'])}")
    for p in cap.get("unread") or []:
        lines.append(f"Could not read {p}, so nothing it would say about the platform is known")
    return lines


# ---------------------------------------------------------------------------
# Marking steps the app cannot run
# ---------------------------------------------------------------------------

_LANDSCAPE = re.compile(
    r"\blandscape\b|\brotat(?:e|es|ed|ing|ion)\b[^.;]{0,40}\b(?:device|phone|iphone|ipad|simulator|screen|"
    r"orientation)\b|\b(?:device|phone|iphone|ipad|simulator|screen)\b[^.;]{0,30}\brotat(?:e|es|ed|ing|ion)\b"
    r"|\bRotate (?:Left|Right)\b|⌘\s*[←→]|\bCmd\s*\+?\s*(?:Left|Right)(?:\s*Arrow)?\b",
    re.I)
_IPAD = re.compile(r"\biPad(?:OS)?\b", re.I)
_IPHONE = re.compile(r"\biPhone\b", re.I)
#: Text that already says the thing is out ("not applicable: landscape …").
_ALREADY = re.compile(r"\bnot applicable\b|\bN/A\b|\bnot supported\b|\bunsupported\b|\bportrait[- ]only\b|"
                      r"\bdoes(?:n't| not) support\b|\b(?:rule N)\b", re.I)

NA_MARKER = "Not applicable: {reason} (rule N):"
ONLY_ON_MARKER = "Only on {device}: {reason} (rule N):"


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;!?])\s+", text or "") if s.strip()]


def verdict(text: str, cap: dict, case_devices: set[str]) -> tuple[str, str] | None:
    """``("na" | "only_ipad" | "only_iphone", reason)`` for one piece of text, or None.

    ``case_devices`` are the devices the whole case names, so a step saying
    "rotate to landscape" in a case titled "… on iPad" is judged for iPad.
    """
    if not text or _ALREADY.search(text):
        return None
    devices = cap.get("devices")
    named = {d for d, rx in (("iPad", _IPAD), ("iPhone", _IPHONE)) if rx.search(text)} or set(case_devices)
    # Device family first: a step on a device the app does not ship for.
    if devices:
        for d in sorted(named):
            if d not in devices:
                return "na", (f"the app does not target {d} ({cap['devices_source']}); "
                              f"it runs on {', '.join(devices)}")
    if not _LANDSCAPE.search(text):
        return None
    shipped = [d for d in ("iPhone", "iPad") if not devices or d in devices]
    on = {d: supports_landscape(cap, d) for d in shipped}
    src = cap.get("orientations_source") or {}
    targets = [d for d in shipped if d in named] or shipped
    if any(on[d] is None for d in targets):
        return None  # could not tell for a device this step may run on
    if all(on[d] for d in targets):
        return None
    no = [d for d in targets if not on[d]]
    yes = [d for d in targets if on[d]]
    reason = "; ".join(f"{d} is portrait-only ({src[d]})" if cap["orientations"][d] == ["portrait"]
                       else f"{d} supports only {_orient_text(cap['orientations'][d])} ({src[d]})"
                       for d in no)
    if yes:
        return f"only_{yes[0].lower()}", f"{reason}; landscape exists only on {', '.join(yes)}"
    return "na", reason


def _case_devices(case: dict) -> set[str]:
    text = " ".join(str(case.get(k) or "") for k in ("title", "preconditions", "environment"))
    return {d for d, rx in (("iPad", _IPAD), ("iPhone", _IPHONE)) if rx.search(text)}


def _prefix(text: str, marker: str) -> str:
    return f"{marker} {text}" if text else marker


def _ios_caps(targets: list[dict]) -> dict | None:
    for t in targets or []:
        if t.get("platform") == "ios" and t.get("capabilities"):
            return t["capabilities"]
    return None


def flag_unsupported_steps(test_plan: Any, targets: list[dict]) -> list[dict]:
    """Mark, in place, cases and steps that need something the app does not support.

    * The case's title or preconditions need it: the expected result starts
      with "Not applicable: <reason> (rule N):" and ``not_applicable`` is set.
    * One step needs it: that step is prefixed instead, so its number holds.
    * Landscape where only one device has it: "Only on iPad: …", which
      tells the tester where to run it rather than calling it N/A.
    * An entitlement-backed feature (push, Sign in with Apple …) the
      entitlements file read does not grant: "Not applicable".

    Only mobile-surface cases on a ticket with an iOS repo are looked at.
    """
    cap = _ios_caps(targets)
    if not cap:
        return []
    out = []
    for section in CASE_SECTIONS:
        for i, case in enumerate(getattr(test_plan, section, None) or []):
            if not isinstance(case, dict) or case.get("surface") not in (None, "", "mobile", "manual_only"):
                continue
            devs = _case_devices(case)
            head = " ".join(str(case.get(k) or "") for k in ("title", "preconditions"))
            v = verdict(head, cap, devs) or _entitlement_verdict(head, cap)
            cid = f"{section}:{i}"
            if v:
                kind, reason = v
                marker = (NA_MARKER.format(reason=reason) if kind == "na"
                          else ONLY_ON_MARKER.format(device="iPad" if kind == "only_ipad" else "iPhone",
                                                     reason=reason))
                if marker not in (case.get("expected") or ""):
                    case["expected"] = _prefix(case.get("expected") or "", marker)
                if kind == "na":
                    case["not_applicable"] = {"reason": reason, "rule": "N"}
                out.append({"case_id": cid, "kind": kind, "reason": reason, "where": "case"})
                continue
            steps = case.get("steps") or []
            for n, step in enumerate(steps):
                if not isinstance(step, str):
                    continue
                sv = verdict(step, cap, devs) or _entitlement_verdict(step, cap)
                if not sv:
                    continue
                kind, reason = sv
                marker = (NA_MARKER.format(reason=reason) if kind == "na"
                          else ONLY_ON_MARKER.format(device="iPad" if kind == "only_ipad" else "iPhone",
                                                     reason=reason))
                steps[n] = _prefix(step, marker)
                out.append({"case_id": cid, "kind": kind, "reason": reason, "where": f"step {n + 1}"})
    lines = getattr(test_plan, "regression_checklist", None) or []
    for i, line in enumerate(lines):
        if not isinstance(line, str):
            continue
        v = verdict(line, cap, set()) or _entitlement_verdict(line, cap)
        if v:
            kind, reason = v
            marker = (NA_MARKER.format(reason=reason) if kind == "na"
                      else ONLY_ON_MARKER.format(device="iPad" if kind == "only_ipad" else "iPhone",
                                                 reason=reason))
            lines[i] = _prefix(line, marker)
            out.append({"case_id": f"regression_checklist:{i}", "kind": kind, "reason": reason,
                        "where": "line"})
    return out


def _entitlement_verdict(text: str, cap: dict) -> tuple[str, str] | None:
    if cap.get("entitlements") is None or not text or _ALREADY.search(text):
        return None
    for key, what, rx in ENTITLEMENT_NEEDS:
        if rx.search(text) and key not in cap["entitlements"]:
            return "na", (f"the app has no {key} entitlement ({cap['entitlements_source']}), "
                          f"so it cannot use {what}")
    return None
