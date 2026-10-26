"""What the cited code literally says: UI strings, rule conditions, client token scopes.

Why this exists
---------------
A UAT run on SK-2719 (plan 1) found three kinds of claim the plan marked as
grounded in code that the code does not make:

1. **A label taken from the ticket.** Steps told the tester to tap
   'Sign In Person' and cited ``FromActionItem.swift``, which defines the row as
   ``NSLocalizedString("TransactionAction.signInPersonButtonTitle",
   value: "Sign In-Person", …)``.
2. **A rule summarised from its guard clause.** Two cases restated the in-person
   picker's eligibility rule as "status non-null AND isActive AND group non-null
   AND role 'Signer'", verified against ``activeSigningRecipients``. The function
   also requires ``status == "InProgress"`` and then keeps only the recipients in
   the lowest ``group.order``.
3. **A backend case with the wrong token.** A case that reproduces the iOS app's
   call said "Bearer <agent token>". The app's token carries
   a signing-service user-id scope (its Okta config); the token the runner could
   get did not, and the downstream returned 401.

The root cause of the first two was the prompt, not the model's reading: on
that PR every changed file's diff was cut to 600 characters (``diff_budget``
spreads 16,000 characters over 33 files), so neither ``"Sign In-Person"`` nor
the ``InProgress`` and ``.min()`` lines reached the generator. It filled the
gaps from the ticket and the guard clause it did see, and still said "verified".
The third was a fact nobody gave it: the client's auth config is not in any diff.

So this module reads the changed files whole, at the PR's head commit, and
derives three compact fact lists the generator is shown and the verification
pass (``grounding_verification``) checks against:

* **UI strings** — every user-visible string literal in the changed code, with
  ``file:line``, from NSLocalizedString values, ``.strings``/``.xcstrings``,
  R.swift comments, Android ``strings.xml``, JSX/TSX text and attributes, i18n
  JSON, and SwiftUI/Compose text calls. A step quotes one of these or says it
  could not.
* **Rule conditions** — the whole body of each changed function that filters,
  guards or picks a minimum, with the same-file helpers it calls, line-numbered,
  and the list of conditions in it. A restated rule must name all of them.
* **Client token scopes** — the scopes and audience each client app's OAuth/OIDC
  config requests, so a backend case that mirrors a client call can say which
  token it needs.

Everything except ``read_source_index`` is pure and language-agnostic by
regular expression, which is a deliberate trade: a parser per language would be
exact for the languages it knows and silent for the rest. Limits are written
next to each extractor.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field

# --- files -------------------------------------------------------------------

#: Extensions whose text is code or a UI resource worth reading whole.
SOURCE_EXTENSIONS = (
    ".swift", ".m", ".mm", ".kt", ".kts", ".java", ".ts", ".tsx", ".js", ".jsx",
    ".mjs", ".cjs", ".vue", ".svelte", ".py", ".cs", ".go", ".rb", ".php", ".dart",
    ".strings", ".xcstrings", ".stringsdict", ".xml", ".json", ".plist",
)

#: Changed files read whole per run, and the byte cap per file. Generated files
#: (R.generated.swift, lockfiles) are what the cap exists for.
MAX_CHANGED_FILES = 60
MAX_FILE_BYTES = 400_000

#: Default-locale localization resources. Only the default locale: a step is
#: written in the language the tester runs the app in, and that is the base.
_LOCALE_RESOURCE_RE = re.compile(
    r"(?:^|/)(?:(?:en|Base)\.lproj/[^/]+\.strings"
    r"|[^/]+\.xcstrings"
    r"|res/values/strings\.xml"
    r"|(?:locales?|i18n|lang|langs|translations|messages|intl)/(?:en(?:[-_][A-Za-z]{2})?)(?:/[^/]+)?\.json"
    r"|(?:locales?|i18n|lang|translations|messages)/[^/]*\ben(?:[-_][A-Za-z]{2})?\.json)$"
)
MAX_RESOURCE_FILES = 8

#: Client OAuth/OIDC config files, by name. Read only in client repos.
_AUTH_CONFIG_NAME_RE = re.compile(
    r"(?i)(okta|oidc|oauth|auth0|msal|appauth|auth[-_.]?config|authconfig|cognito|keycloak|identity[-_]?config)"
)
_AUTH_CONFIG_EXT = (".plist", ".json", ".xml", ".ts", ".js", ".kt", ".swift", ".yaml", ".yml",
                    ".env", ".properties", ".gradle", ".xcconfig")
_SKIP_DIRS_RE = re.compile(r"(?:^|/)(?:node_modules|Pods|vendor|build|dist|\.git|test|tests|__tests__|spec|e2e|fixtures?)/")
MAX_AUTH_FILES = 10

CLIENT_PLATFORMS = ("ios", "android", "web")


def is_source_path(path: str) -> bool:
    return isinstance(path, str) and path.lower().endswith(SOURCE_EXTENSIONS)


def is_locale_resource(path: str) -> bool:
    return bool(_LOCALE_RESOURCE_RE.search(path or ""))


def is_auth_config(path: str) -> bool:
    if not path or _SKIP_DIRS_RE.search("/" + path):
        return False
    base = posixpath.basename(path)
    return base.lower().endswith(_AUTH_CONFIG_EXT) and bool(_AUTH_CONFIG_NAME_RE.search(base))


def _ext(path: str) -> str:
    return posixpath.splitext(path or "")[1].lower()


#: Languages where '…' is a string, not a character.
_SINGLE_QUOTE_STRINGS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
                         ".py", ".rb", ".php", ".dart", ".yaml", ".yml")
_BACKTICK_STRINGS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")

_DQ_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
_SQ_RE = re.compile(r"'((?:[^'\\\n]|\\.)*)'")
_BT_RE = re.compile(r"`((?:[^`\\\n$]|\\.)*)`")


def _literal_res(path: str) -> list[re.Pattern]:
    ext = _ext(path)
    res = [_DQ_RE]
    if ext in _SINGLE_QUOTE_STRINGS:
        res.append(_SQ_RE)
    if ext in _BACKTICK_STRINGS:
        res.append(_BT_RE)
    return res


def _unescape(s: str) -> str:
    # \uXXXX first: 'Starting\u2026' is "Starting…" on screen, not "Startingu2026".
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def string_literals(path: str, text: str) -> list[tuple[str, int]]:
    """Every single-line string literal in ``text`` as ``(value, line)``."""
    out: list[tuple[str, int]] = []
    res = _literal_res(path)
    for n, line in enumerate((text or "").splitlines(), 1):
        for rx in res:
            for m in rx.finditer(line):
                out.append((_unescape(m.group(1)), n))
    return out


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# --- UI strings --------------------------------------------------------------


@dataclass(frozen=True)
class UIString:
    text: str
    path: str
    line: int
    key: str | None = None
    repo: str | None = None

    def where(self) -> str:
        return f"{posixpath.basename(self.path)}:{self.line}"

    def as_dict(self) -> dict:
        return {"text": self.text, "repo": self.repo, "path": self.path, "line": self.line, "key": self.key}


# Swift: NSLocalizedString("key", value: "Shown", comment: "") — the value is
# what ships; without `value:` the first argument is both key and text.
_NSLOC_RE = re.compile(
    r'NSLocalizedString\(\s*"((?:[^"\\]|\\.)*)"(?P<rest>[^)]*?)\)', re.S)
_NSLOC_VALUE_RE = re.compile(r'value\s*:\s*"((?:[^"\\]|\\.)*)"')
_SWIFT_LOCALIZED_RE = re.compile(r'String\(\s*localized\s*:\s*"((?:[^"\\]|\\.)*)"')
# SwiftUI / Compose / UIKit calls whose first string argument is shown.
_UI_CALL_RE = re.compile(
    r'\b(?:Text|Button|Label|Toggle|Link|Section|Tab|Alert|Toast|SnackBar|Snackbar|'
    r'navigationTitle|navigationBarTitle|setTitle|setText|makeText|setMessage|setPositiveButton|'
    r'setNegativeButton|accessibilityLabel|placeholder)\s*\(\s*(?:[\w.]+\s*,\s*)?"((?:[^"\\]|\\.)*)"')
_UI_ASSIGN_RE = re.compile(
    r'\b(?:title|text|message|placeholder|subtitle|header|heading|label|buttonTitle|emptyText|'
    r'emptyMessage|errorMessage|accessibilityLabel)\s*[:=]\s*"((?:[^"\\]|\\.)*)"')
# R.swift: "/// en translation: Shown" or "/// Value: Shown" above the resource.
_RSWIFT_RE = re.compile(r"///\s*(?:[a-z]{2}(?:-[A-Z]{2})? translation|Value)\s*:\s*(.+?)\s*$", re.M)
_RSWIFT_KEY_RE = re.compile(r'key:\s*"((?:[^"\\]|\\.)*)"')
# .strings: "key" = "Shown";
_DOT_STRINGS_RE = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;', re.M)
# strings.xml: <string name="key">Shown</string>
_XML_STRING_RE = re.compile(r'<string\s+name="([^"]+)"[^>]*>(.*?)</string>', re.S)
# JSX/TSX/Vue text between tags, and the attributes that are shown.
_JSX_TEXT_RE = re.compile(r">\s*([^<>{}\n]*[A-Za-z][^<>{}\n]*?)\s*<")
# A `{…}` expression that is a child of an element: right after a tag's `>`
# or right before the next `<`, with no nested braces.
_JSX_EXPR_CHILD_RE = re.compile(r">\s*\{([^{}]*)\}|\{([^{}]*)\}\s*(?=<)")
_JSX_EXPR_LITERAL_RE = re.compile(r"(['\"`])((?:(?!\1)[^\\\n]|\\.)+)\1")
_JSX_ATTR_RE = re.compile(
    r'\b(?:title|label|placeholder|aria-label|alt|text|header|heading|message|buttonText|'
    r'confirmText|cancelText|emptyText|tooltip|description|helperText)\s*=\s*\{?\s*(["\'`])((?:(?!\1)[^\\\n]|\\.)*)\1')


def _xml_text(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    return (s.replace("\\'", "'").replace('\\"', '"').replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")).strip()


def _flatten_json(obj, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _flatten_json(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, str):
        out.append((prefix, obj))
    return out


def _json_value_line(text: str, value: str) -> int:
    i = text.find(json.dumps(value, ensure_ascii=False)[1:-1])
    return _line_of(text, i) if i >= 0 else 1


def resource_strings(path: str, text: str) -> list[UIString]:
    """Key → shown text from a localization resource file."""
    out: list[UIString] = []
    ext = _ext(path)
    if ext == ".strings":
        for m in _DOT_STRINGS_RE.finditer(text):
            out.append(UIString(_unescape(m.group(2)), path, _line_of(text, m.start()), _unescape(m.group(1))))
    elif ext == ".xml":
        for m in _XML_STRING_RE.finditer(text):
            out.append(UIString(_xml_text(m.group(2)), path, _line_of(text, m.start()), m.group(1)))
    elif ext == ".xcstrings":
        try:
            catalog = json.loads(text)
        except ValueError:
            return out
        for key, entry in (catalog.get("strings") or {}).items():
            locs = (entry or {}).get("localizations") or {}
            unit = ((locs.get(catalog.get("sourceLanguage") or "en") or locs.get("en") or {})
                    .get("stringUnit") or {})
            shown = unit.get("value") or key
            out.append(UIString(shown, path, _json_value_line(text, key), key))
    elif ext == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            return out
        for key, value in _flatten_json(data):
            out.append(UIString(value, path, _json_value_line(text, value), key))
    return out


def ui_strings(path: str, text: str) -> list[UIString]:
    """User-visible string literals defined in a source or resource file.

    Narrower than ``string_literals``: this is the table the generator is
    shown, so it keeps what a tester can see — localized values, text handed to
    a UI call or a title/label property, JSX text and shown attributes —
    rather than every key, URL and log line. Limit: text assembled at runtime
    (interpolation, concatenation) is listed as its literal pieces.
    """
    text = text or ""
    if is_locale_resource(path) or _ext(path) in (".strings", ".xcstrings"):
        return resource_strings(path, text)
    out: list[UIString] = []
    seen: set[tuple[str, int]] = set()

    def add(value: str, offset: int, key: str | None = None) -> None:
        value = _unescape(value).strip()
        line = _line_of(text, offset)
        if not value or not re.search(r"[A-Za-z]", value) or (value, line) in seen:
            return
        seen.add((value, line))
        out.append(UIString(value, path, line, key))

    for m in _NSLOC_RE.finditer(text):
        v = _NSLOC_VALUE_RE.search(m.group("rest"))
        if v:
            add(v.group(1), m.start(), m.group(1))
        else:
            add(m.group(1), m.start(), m.group(1))
    for rx in (_SWIFT_LOCALIZED_RE, _UI_CALL_RE, _UI_ASSIGN_RE):
        for m in rx.finditer(text):
            add(m.group(1), m.start(1))
    for m in _RSWIFT_RE.finditer(text):
        after = text[m.end(): m.end() + 400]
        k = _RSWIFT_KEY_RE.search(after)
        add(m.group(1), m.start(1), k.group(1) if k else None)
    if _ext(path) in (".tsx", ".jsx", ".vue", ".svelte", ".html"):
        for m in _JSX_TEXT_RE.finditer(text):
            if not re.search(r"[;=(){}]|=>|&&", m.group(1)):
                add(m.group(1), m.start(1))
        for m in _JSX_ATTR_RE.finditer(text):
            add(m.group(2), m.start(2))
        # Text rendered from an expression between tags:
        # `>{busy ? 'Starting…' : 'Sign In Person'}</Link>`. Only literals that
        # read as words (a capital or a space), so class names and keys in the
        # same braces stay out.
        for m in _JSX_EXPR_CHILD_RE.finditer(text):
            body = m.group(1) or m.group(2) or ""
            for lit in _JSX_EXPR_LITERAL_RE.finditer(body):
                v = lit.group(2)
                if " " in v or (re.match(r"[A-Z]", v) and not re.fullmatch(r"[\w.$-]+", v)):
                    add(v, m.start() + lit.start(2))
    return out


# Keys a source file looks text up by, resolved against resource files.
_KEY_REF_RES = (
    re.compile(r'NSLocalizedString\(\s*"((?:[^"\\]|\\.)*)"'),
    re.compile(r'String\(\s*localized\s*:\s*"((?:[^"\\]|\\.)*)"'),
    re.compile(r"\bR\.string\.(?:\w+\.)?(\w+)\b"),          # R.swift and Android R.string.x
    re.compile(r"@string/(\w+)"),
    re.compile(r"""(?:\bt|\$t|i18n\.t|intl\.formatMessage|translate)\(\s*(?:\{\s*id\s*:\s*)?["'`]([\w.\-:/]+)["'`]"""),
    re.compile(r"""\b(?:i18nKey|id)\s*=\s*["']([\w.\-:/]+)["']"""),
)


def referenced_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for rx in _KEY_REF_RES:
        keys.update(m.group(1) for m in rx.finditer(text or ""))
    return keys


def _camel(key: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", key)
    parts = [p for p in parts if p]
    if not parts:
        return key
    return parts[0][:1].lower() + parts[0][1:] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def resolve_keys(keys: set[str], resources: list[UIString]) -> list[UIString]:
    """Resource strings whose key a file references (R.swift camel-cases keys)."""
    if not keys:
        return []
    camel = {_camel(k) for k in keys}
    return [r for r in resources if r.key and (r.key in keys or _camel(r.key) in camel)]


# --- labels in prose ---------------------------------------------------------

#: Quote pairs a plan uses around a label.
QUOTED_RE = re.compile(r"[\"“'‘]([^\"”'’\n]{1,80}?)[\"”'’](?![A-Za-z])")

_LABEL_VERBS = (
    r"(?i:tap|taps|tapping|tapped|click|clicks|clicking|clicked|press|presses|pressing|select|selects|"
    r"selecting|choose|chooses|choosing|toggle|toggles|open|opens|using|via|find|locate|see|sees|"
    r"shows?|showing|displays?|displaying|reads?|reading|titled|labell?ed|named|called|says|saying|"
    r"heading|header|title|button|row|option|item|entry)"
)
_CONTROL_NOUNS = (
    r"(?i:button|link|tab|menu|menu item|row|item|entry|action|option|toggle|switch|checkbox|"
    r"dialog|modal|sheet|alert|toast|banner|snackbar|heading|header|title|label|placeholder|"
    r"message|screen|page|empty state|text|cell|chip|pill|field|icon|tooltip|prompt|cta)"
)
_LABEL_CONTEXT_RES = (
    re.compile(rf"\b{_LABEL_VERBS}\s+(?:(?i:on|the|a|an|its)\s+){{0,2}}{QUOTED_RE.pattern}"),
    re.compile(rf"{QUOTED_RE.pattern}\s+(?:(?i:more[- ]menu|context|action|menu|primary|secondary)\s+)?{_CONTROL_NOUNS}\b"),
    re.compile(rf"(?:→|->|>|›)\s*{QUOTED_RE.pattern}"),
)


def quoted_labels(text: str) -> list[str]:
    """Quoted text a step tells the tester to find or tap.

    Only quotes in a UI context — after a UI verb ("tap 'Save'"), before a
    control noun ("the 'Leave PDF' option", "a 'Sign In Person' row"), or at the
    end of a navigation path ("more menu → 'Signers'"). A quoted value typed
    into a field or a quoted ticket phrase is not a label.
    """
    out: list[str] = []
    for rx in _LABEL_CONTEXT_RES:
        for m in rx.finditer(text or ""):
            label = m.group(1).strip()
            if label and re.search(r"[A-Za-z]", label) and label not in out:
                out.append(label)
    return out


def _canon(s: str) -> str:
    """Typographic quotes, dashes and whitespace runs are not part of a label."""
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("…", "...")
    return " ".join(s.split())


def _loose(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


#: Placeholders in a format string, and in a plan's label ("[First Last] is signing").
_CODE_PLACEHOLDER_RE = re.compile(r"%(?:\d+\$)?[@dsfilu]|%lld|\{\{?\s*\w*\s*\}?\}|\$\{[^}]*\}|\\\([^)]*\)")
_PLAN_PLACEHOLDER_RE = re.compile(r"\[[^\]]+\]|<[^>]+>|\{[^}]+\}")


def _format_match(label: str, literal: str) -> bool:
    """Whether a format literal ("%@ is signing") renders as the label, with the
    label's own placeholders ("[name] is signing") standing for any text."""
    if not _CODE_PLACEHOLDER_RE.search(literal) and not _PLAN_PLACEHOLDER_RE.search(label):
        return False
    parts = _CODE_PLACEHOLDER_RE.split(_canon(literal))
    fixed = "".join(parts).strip()
    # " %@" renders as anything; the code has to supply the words.
    if len(re.findall(r"[A-Za-z]", fixed)) < 3 or len(fixed) < 0.4 * len(_PLAN_PLACEHOLDER_RE.sub("", label)):
        return False
    rx = ".+?".join(re.escape(p) for p in parts)
    want = _PLAN_PLACEHOLDER_RE.sub("\x00", _canon(label))
    probe = want.replace("\x00", "X")
    if re.fullmatch(rx, probe):
        return True
    # The label's placeholder sits where the code's does.
    lparts = want.split("\x00")
    return len(lparts) == len(parts) and all(a == b for a, b in zip(lparts, parts))


def find_literal(label: str, literals: list[UIString]) -> UIString | None:
    """The literal ``label`` quotes verbatim (typography aside), or None."""
    want = _canon(label)
    for lit in literals:
        if _canon(lit.text) == want:
            return lit
    for lit in literals:
        if _format_match(label, lit.text):
            return lit
    # A label is often one piece of a longer literal ("Sign In-Person" inside a
    # format string); a whole-word containment still quotes the code.
    for lit in literals:
        if len(want) >= 3 and re.search(rf"(?<![A-Za-z0-9]){re.escape(want)}(?![A-Za-z0-9])", _canon(lit.text)):
            return lit
    return None


def near_literals(label: str, literals: list[UIString]) -> list[UIString]:
    """Literals that say the same thing with different case, spaces or punctuation."""
    want = _loose(label)
    if not want:
        return []
    out, seen = [], set()
    for lit in literals:
        if _loose(lit.text) == want and lit.text not in seen:
            seen.add(lit.text)
            out.append(lit)
    return out


# --- functions and their conditions -----------------------------------------

_FUNC_DEF_RES = (
    re.compile(r"\b(?:func|fun|function|def|fn)\s+(?:[A-Za-z_][\w.]*\.)?([A-Za-z_]\w*)\s*[(<]"),
    re.compile(r"\b(?:const|let|var|val)\s+([A-Za-z_]\w*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_]\w*)\s*(?::[^=]+)?=>"),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s+)?function\b"),
    # An arrow function whose parameters run onto the next lines.
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_]\w*)\s*(?::[^=]+)?=\s*(?:async\s*)?\(\s*(?:[A-Za-z_{\[]\w*[^)]*)?$"),
    # A TypeScript/C# member with modifiers but no return type before the name.
    re.compile(r"^\s*(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)+([A-Za-z_]\w*)\s*[(<]"),
    re.compile(r"^\s*(?:(?:public|private|protected|internal|static|async|override|virtual|final|"
               r"abstract|sealed|readonly|suspend|inline|open)\s+)+[\w<>\[\],.?]+\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*(?:async\s+)?([A-Za-z_]\w*)\s*\([^)]*\)\s*(?::\s*[\w<>\[\]|, ]+)?\s*\{\s*$"),
)
_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "guard", "return", "else", "case", "when", "do",
    "try", "with", "elif", "except", "func", "function", "init", "new", "super", "self", "this",
})


@dataclass
class Function:
    name: str
    start: int
    end: int
    lines: list[str] = field(repr=False)

    def body(self) -> list[tuple[int, str]]:
        return [(n, self.lines[n - 1]) for n in range(self.start, self.end + 1)]


def _strip_strings_and_comments(line: str) -> str:
    line = _DQ_RE.sub('""', line)
    line = _SQ_RE.sub("''", line)
    return re.sub(r"(//|#(?!\w)).*$", "", line)


def _extent(lines: list[str], start: int, python: bool) -> int:
    """Last line (1-based) of the definition starting at ``start``."""
    if python:
        indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
        end = start
        for n in range(start + 1, len(lines) + 1):
            s = lines[n - 1]
            if s.strip() and (len(s) - len(s.lstrip())) <= indent:
                break
            if s.strip():
                end = n
        return end
    # Braces inside the parameter list are not the body: a React hook's
    # `useSignNow({ flags, userInfo }: { flags?: F; userInfo: any }) {` closed
    # its "body" at the parameter type's `}`, and its typed members ended the
    # scan early as if `userInfo: any;` were an expression body.
    depth, opened, parens = 0, False, 0
    for n in range(start, len(lines) + 1):
        s = _strip_strings_and_comments(lines[n - 1])
        for ch in s:
            if ch == "(":
                parens += 1
            elif ch == ")":
                parens = max(0, parens - 1)
            elif parens == 0 and ch == "{":
                depth, opened = depth + 1, True
            elif parens == 0 and ch == "}":
                depth -= 1
                if opened and depth == 0:
                    return n
        if not opened and parens == 0 and (s.rstrip().endswith(";") or (n > start and not s.strip())):
            return n  # an expression body: `=> expr;` or `fun f() = expr`
    return len(lines)


def functions(path: str, text: str) -> list[Function]:
    """Every function, method or closure-valued binding defined in ``text``."""
    lines = (text or "").splitlines()
    python = _ext(path) == ".py"
    out: list[Function] = []
    for n, line in enumerate(lines, 1):
        code = _strip_strings_and_comments(line)
        for rx in _FUNC_DEF_RES:
            m = rx.search(code)
            if m and m.group(1) not in _KEYWORDS:
                out.append(Function(m.group(1), n, _extent(lines, n, python), lines))
                break
    return out


def function_named(fns: list[Function], name: str) -> Function | None:
    hits = [f for f in fns if f.name == name]
    return min(hits, key=lambda f: f.end - f.start) if hits else None


def enclosing_function(fns: list[Function], line: int) -> Function | None:
    hits = [f for f in fns if f.start <= line <= f.end]
    return min(hits, key=lambda f: f.end - f.start) if hits else None


_CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)\s*\(|\b(?:self|this|Self|cls)\.([A-Za-z_]\w*)\s*\(")


def called_names(fn: Function) -> set[str]:
    names: set[str] = set()
    for _, line in fn.body()[1:]:
        for m in _CALL_RE.finditer(_strip_strings_and_comments(line)):
            names.add(m.group(1) or m.group(2))
    return names - _KEYWORDS


def with_helpers(fn: Function, fns: list[Function], limit: int = 6) -> list[Function]:
    """``fn`` plus the same-file functions it calls, transitively."""
    out, todo = [fn], [fn]
    by_name = {f.name: f for f in fns}
    while todo and len(out) < limit:
        cur = todo.pop()
        for name in sorted(called_names(cur)):
            helper = by_name.get(name)
            if helper and helper not in out:
                out.append(helper)
                todo.append(helper)
    return out


@dataclass(frozen=True)
class Atom:
    """One condition a rule applies, and the words that name it in prose."""

    kind: str           # literal | nonnull | bool | compare | aggregate | predicate
    display: str
    line: int
    literal: str | None = None
    fields: tuple[str, ...] = ()
    op: str | None = None
    #: The object a predicate is read from (`userInfo` in `!userInfo.isDelegate`).
    #: A rule naming the predicate implies that object's null check.
    receiver: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"kind": self.kind, "condition": self.display, "line": self.line}


_COMPARATOR_METHODS = frozenset({
    "caseInsensitiveCompare", "localizedCaseInsensitiveCompare", "compare", "equals",
    "equalsIgnoreCase", "startsWith", "endsWith", "hasPrefix", "hasSuffix", "contains",
    "includes", "lowercased", "uppercased", "toLowerCase", "toUpperCase", "lower", "upper",
    "trim", "trimmed", "rawValue", "value", "orderedSame", "localeCompare", "strip",
    "casefold", "Equals", "StartsWith", "Contains", "ToLower", "ToUpper",
})
_NOISE_LINE_RE = re.compile(
    r"\b(?:print|NSLog|debugPrint|log|logger|Logger|console|fatalError|assert|assertionFailure|"
    r"precondition|throw|raise|NSLocalizedString|Log\.\w)\b\s*[.(]")
_HAS_COMPARISON_RE = re.compile(
    r"===?|!==?|<=|>=|\s<\s|\s>\s|\b(?:caseInsensitiveCompare|localizedCaseInsensitiveCompare|equals|"
    r"equalsIgnoreCase|startsWith|endsWith|hasPrefix|hasSuffix|contains|includes|localeCompare|"
    r"Equals|StartsWith|Contains)\s*\(|\bin\s*[\[(]|\bcase\b|\bwhen\b")
_OPERAND = r"[\w$?.!\]\[()\\]+?"
_NIL_RE = re.compile(rf"({_OPERAND})\s*(!==?|===?)\s*(nil|null|None|undefined)\b")
_PY_NONE_RE = re.compile(r"([\w.\]\[]+)\s+is\s+(not\s+)?None\b")
#: Optional binding: `guard let s = $0.status,` and the `, let t = …` that
#: continue it. A plain `let x = y.filter {` is an assignment, not a check, and
#: a binding of a call's result (`= xs.min()`) is the aggregate, not a field.
_BIND_RE = re.compile(r"(?:\b(?:guard|if|while)\s+|,\s*|^\s*)(?:let|var)\s+\w+\s*=\s*([\w$?.]+)(?![\w$?.]*\()")
_BOOL_RE = re.compile(rf"({_OPERAND})\s*(!==?|===?)\s*(true|false|True|False)\b")
_CMP_RE = re.compile(rf"({_OPERAND})\s*(===?|!==?|<=|>=|\s<\s|\s>\s)\s*({_OPERAND})(?=[\s,;){{&|]|$)")
_AGG_RE = re.compile(
    r"(?:\.|\b)(min|max|minBy|maxBy|minOf|maxOf|minByOrNull|maxByOrNull|minWith|maxWith|"
    r"min_by|max_by|Min|Max|MinBy|MaxBy)\s*[({]|\bMath\.(min|max)\s*\(")
_SORT_FIRST_RE = re.compile(r"\b(sort|sorted|sortedBy|sorted_by|OrderBy|orderBy|sortBy)\b.*?(?:\.first\b|\[0\]|\.First\(\))")
_PREDICATE_CALL_RE = re.compile(r"\b((?:is|has|can|should|allows?)[A-Z]\w*)\b")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_FIELD_NOISE = frozenset({"self", "this", "it", "Self", "cls", "item", "x", "r", "e", "o", "s", "v",
                          "true", "false", "nil", "null", "None", "undefined", "return", "guard",
                          "let", "var", "val", "const", "if", "else", "filter", "map", "compactMap",
                          "first", "where", "count", "length", "size", "toString", "description",
                          "getTime", "valueOf", "toISOString", "lowercased", "uppercased", "toLowerCase",
                          "toUpperCase", "trimmingCharacters", "o", "a", "b", "acc"})


def _fields_of(operand: str) -> tuple[str, ...]:
    """The property names an operand reads: ``$0.group?.order`` → (group, order)."""
    inner = re.sub(r"^[\w.]*\(\s*(.+)\)\s*$", r"\1", operand.strip())  # normalized($0.email)
    names = [n for n in _IDENT_RE.findall(inner.replace("$0", "")) if n not in _FIELD_NOISE
             and n not in _COMPARATOR_METHODS]
    return tuple(names[-2:]) if names else ()


def _literal_field(before: str) -> tuple[str, ...]:
    names = [n for n in _IDENT_RE.findall(before) if n not in _COMPARATOR_METHODS and n not in _FIELD_NOISE]
    return (names[-1],) if names else ()


_FILTER_CALL_RE = re.compile(
    r"(?:^|[^\w.])(?:(?:let|var|val|const)\s+)?(?:(\w+)\s*=\s*)?(?:return\s+)?([\w$.?()]+?)\s*\.\s*"
    r"(?:filter|filterNot|removeAll|removeIf|first|firstIndex|last|find|findIndex|findLast|some|every|"
    r"allSatisfy|contains|where|Where|Any|All|First|takeWhile|dropWhile|partition|reject|select)\b\s*(?:\(|\{|$)")
_RETURN_RE = re.compile(r"\breturn\b\s*(.*)$")
_EXIT_RE = re.compile(r"\b(?:return|throw|raise|continue|break)\b")


def _matching_close(lines: list[str], i: int, opener: str) -> int:
    """Index of the line closing the first ``opener`` at or after line ``i``."""
    closer = {"{": "}", "(": ")", "[": "]"}[opener]
    depth, opened = 0, False
    for j in range(i, len(lines)):
        for ch in _strip_strings_and_comments(lines[j]):
            if ch == opener:
                depth, opened = depth + 1, True
            elif ch == closer and opened:
                depth -= 1
                if depth == 0:
                    return j
    return len(lines) - 1


def decision_lines(body: list[tuple[int, str]]) -> set[int] | None:
    """Lines whose conditions decide what the function returns.

    A guard clause; an ``if`` whose block exits (return/throw/continue/break);
    a filter-like closure whose result is returned (directly or through the
    variable it is assigned to); a boolean ``return``; a Python comprehension's
    ``if``. A condition that only shapes the output — ``if mode == .inPerson {
    items.append(…) }`` — decides nothing about which items qualify, and
    counting it made a correct "returns nil unless …" restatement fail.
    None when the body has no ``return`` (an expression body): everything counts.
    """
    nums = [n for n, _ in body]
    lines = [_strip_strings_and_comments(t) for _, t in body]
    returns = [(i, m.group(1)) for i, t in enumerate(lines) for m in [_RETURN_RE.search(t)] if m]
    if not returns:
        return None
    returned = {tok for _, expr in returns for tok in _IDENT_RE.findall(expr)[:2]}
    out: set[int] = set()
    i = 0
    while i < len(lines):
        t = lines[i]
        if re.search(r"\bguard\b", t):
            j = i
            while j < len(lines) and not re.search(r"\belse\b", lines[j]):
                j += 1
            out.update(nums[i: j + 1])
        elif re.search(r"\b(?:if|elif|else\s+if|unless)\b", t):
            j = i
            while j < len(lines) - 1 and "{" not in lines[j] and not lines[j].rstrip().endswith(":"):
                j += 1
            if "{" in lines[j]:
                end = _matching_close(lines, j, "{")
            else:  # Python: the indented block
                ind = len(body[j][1]) - len(body[j][1].lstrip())
                end = j
                for k in range(j + 1, len(lines)):
                    if body[k][1].strip() and len(body[k][1]) - len(body[k][1].lstrip()) <= ind:
                        break
                    end = k
            block = "\n".join(lines[j: end + 1])
            if _EXIT_RE.search(block):
                out.update(nums[i: j + 1])
        m = _FILTER_CALL_RE.search(t)
        if m:
            assigned, receiver = m.group(1), (m.group(2) or "").split(".")[0].split("(")[0]
            if "return" in t or assigned in returned or receiver in returned:
                opener = "{" if "{" in t[m.end() - 1:] else "("
                end = _matching_close(lines, i, opener) if opener in t else i
                out.update(nums[i: end + 1])
        r = _RETURN_RE.search(t)
        if r and re.search(r"&&|\|\||[=!<>]=|\s[<>]\s|\band\b|\bor\b|\bnot\b", r.group(1)):
            out.add(nums[i])
        if re.search(r"\bfor\b.+\bin\b.+\bif\b", t):
            out.add(nums[i])
        i += 1
    # An aggregate feeds a decision through the variable it is assigned to.
    decided_text = "\n".join(lines[k] for k, n in enumerate(nums) if n in out)
    decided_text += "\n" + "\n".join(expr for _, expr in returns)
    for k, t in enumerate(lines):
        a = re.match(r"\s*(?:let|var|val|const)?\s*(\w+)\s*=", t)
        if a and _AGG_RE.search(t) and re.search(rf"\b{re.escape(a.group(1))}\b", decided_text):
            out.add(nums[k])
    return out


def _prev_line(body: list[tuple[int, str]], n: int) -> str:
    prev = [line for m, line in body if m == n - 1]
    return prev[0] if prev else ""


def condition_atoms(path: str, body: list[tuple[int, str]]) -> list[Atom]:
    """The conditions a function body applies, one per comparison or filter step.

    Recognised across C-family languages, Swift, Kotlin and Python: comparisons
    against string literals (including caseInsensitiveCompare/equals forms),
    nil/null/None checks and optional bindings, boolean comparisons, other
    comparisons, min/max/sort-and-take-first, and calls to ``isX``/``hasX``
    predicates. Not recognised: conditions hidden in a lookup table, a regex,
    or a function in another file — the caller reports the last case.
    """
    atoms: list[Atom] = []
    seen: set[tuple] = set()

    def add(atom: Atom) -> None:
        key = (atom.kind, atom.literal, atom.fields, atom.op)
        if key not in seen:
            seen.add(key)
            atoms.append(atom)

    res = _literal_res(path)
    decides = decision_lines(body)
    for n, raw in body:
        code = re.sub(r"(//|#(?![\w\[])).*$", "", raw) if "//" in raw or "#" in raw else raw
        if not code.strip() or _NOISE_LINE_RE.search(code) or (decides is not None and n not in decides):
            continue
        comparing = bool(_HAS_COMPARISON_RE.search(code))
        if comparing:
            for rx in res:
                for m in rx.finditer(code):
                    lit = _unescape(m.group(1))
                    if not lit.strip():
                        continue
                    flds = _literal_field(code[: m.start()])
                    add(Atom("literal", f'{".".join(flds) or "value"} matches "{lit}"', n, lit, flds))
        stripped = _strip_strings_and_comments(code)
        for m in _NIL_RE.finditer(stripped):
            flds = _fields_of(m.group(1))
            if flds:
                op = "nonnull" if m.group(2).startswith("!") else "null"
                add(Atom("nonnull", f"{'.'.join(flds)} {'is set' if op == 'nonnull' else 'is unset'}", n,
                         fields=flds, op=op))
        for m in _PY_NONE_RE.finditer(stripped):
            flds = _fields_of(m.group(1))
            if flds:
                add(Atom("nonnull", f"{'.'.join(flds)} {'is set' if m.group(2) else 'is unset'}", n,
                         fields=flds, op="nonnull" if m.group(2) else "null"))
        binds = _BIND_RE.finditer(stripped) if re.search(r"\b(?:guard|if|while)\b|^\s*,|,\s*$", stripped) \
            or (n > body[0][0] and re.search(r"\b(?:guard|if)\s+let\b[^{]*,\s*$", _prev_line(body, n))) else ()
        for m in binds:
            if "." in m.group(1) or "$" in m.group(1):
                flds = _fields_of(m.group(1))
                if flds:
                    add(Atom("nonnull", f"{'.'.join(flds)} is set", n, fields=flds, op="nonnull"))
        for m in _BOOL_RE.finditer(stripped):
            flds = _fields_of(m.group(1))
            if flds:
                add(Atom("bool", f"{'.'.join(flds)} {m.group(2)} {m.group(3)}", n, fields=flds,
                         op=f"{m.group(2)}{m.group(3).lower()}"))
        for m in _CMP_RE.finditer(stripped):
            left, op, right = m.group(1), m.group(2).strip(), m.group(3)
            if re.fullmatch(r"(nil|null|None|undefined|true|false|True|False|\"\"|'')", right.strip()):
                continue
            if re.search(r"\"\"|''", left + right):
                continue  # a string comparison, already a literal atom
            flds = _fields_of(left) + tuple(f for f in _fields_of(right) if f not in _fields_of(left))
            if flds:
                add(Atom("compare", f"{left.strip()} {op} {right.strip()}", n, fields=flds, op=op))
        for m in _AGG_RE.finditer(stripped):
            which = (m.group(1) or m.group(2) or "").lower()
            agg = "min" if which.startswith("min") else "max"
            keypath = re.findall(r"\\\.([\w?.]+)|\$0\.([\w?.]+)|\bit\.([\w?.]+)|=>\s*\w+\.([\w?.]+)|lambda\s+\w+:\s*\w+\.([\w.]+)",
                                 stripped)
            flds = tuple(n2 for grp in keypath for part in grp if part
                         for n2 in _IDENT_RE.findall(part) if n2 not in _FIELD_NOISE)
            add(Atom("aggregate", f"keeps the {agg} {'.'.join(flds) or 'value'}", n, fields=flds, op=agg))
        if _SORT_FIRST_RE.search(stripped):
            add(Atom("aggregate", "sorts and keeps the first", n, op="min"))
        for m in _PREDICATE_CALL_RE.finditer(stripped):
            if m.group(1) in ("isEmpty", "isNotEmpty", "hasNext") or any(
                    m.group(1) in a.fields for a in atoms if a.line == n):
                continue
            owner = re.search(r"([A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)*)\??\.$", stripped[:m.start()])
            receiver = tuple(re.split(r"\??\.", owner.group(1))) if owner else ()
            add(Atom("predicate", f"{m.group(1)}", n, fields=(m.group(1),), op="call",
                     receiver=receiver))
    return atoms


# --- matching prose against atoms --------------------------------------------

_MIN_WORDS = r"(?:min|minimum|lowest|smallest|earliest|least|first[- ]in[- ]order)"
_MAX_WORDS = r"(?:max|maximum|highest|largest|latest|greatest|most recent)"


def _words(identifier: str) -> list[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", identifier).replace("_", " ").split()
    return [p.lower() for p in parts if p]


def field_pattern(name: str) -> str:
    """Prose that names a field: ``isActive`` → isActive | is active | active."""
    words = _words(name)
    alts = {re.escape(name.lower()), r"[\s_-]*".join(map(re.escape, words))}
    if len(words) > 1 and words[0] in ("is", "has", "can", "should"):
        alts.add(r"[\s_-]*".join(map(re.escape, words[1:])))
    return r"(?<![A-Za-z0-9])(?:" + "|".join(sorted(alts, key=len, reverse=True)) + r")(?![A-Za-z0-9])"


def literal_pattern(lit: str) -> str:
    words = _words(lit) if re.fullmatch(r"[A-Za-z0-9_ ]+", lit) else [lit.lower()]
    core = r"[\s_-]*".join(map(re.escape, words)) if words else re.escape(lit.lower())
    if len(lit) <= 2:  # 'Me': only as a quoted value, not the English word
        return rf"[\"“'‘`]{re.escape(lit.lower())}[\"”'’`]"
    return rf"(?<![A-Za-z0-9]){core}(?![A-Za-z0-9])"


def atoms_mentioned(atoms: list[Atom], prose: str) -> list[Atom]:
    """The atoms ``prose`` names. A field said to equal a value ("status is
    InProgress") is also said to be set: the null check is implied. So is the
    check on an object whose property the prose names: "not a delegate"
    (``!userInfo.isDelegate``) cannot hold unless ``userInfo`` is set, and on
    SK-2687 demanding the guard separately downgraded correct expected results."""
    named = [a for a in atoms if atom_mentioned(a, prose)]
    valued = {f for a in named if a.kind in ("literal", "compare", "bool") for f in a.fields[-1:]}
    parents = {a.fields[:i] for a in named if a.kind != "nonnull" for i in range(1, len(a.fields))}
    parents |= {a.receiver[:i] for a in named for i in range(1, len(a.receiver) + 1)}
    for a in atoms:
        if a in named or a.kind != "nonnull" or a.op != "nonnull" or not a.fields:
            continue
        if a.fields[-1] in valued or a.fields in parents:
            named.append(a)
    return named


def atom_mentioned(atom: Atom, prose: str) -> bool:
    text = (prose or "").lower()
    if atom.kind == "literal":
        return bool(re.search(literal_pattern(atom.literal or ""), text))
    if atom.kind == "aggregate":
        words = _MIN_WORDS if atom.op == "min" else _MAX_WORDS
        if not atom.fields:
            return bool(re.search(rf"(?<![a-z]){words}(?![a-z])", text))
        for m in re.finditer(rf"(?<![a-z]){words}(?![a-z])", text):
            window = text[max(0, m.start() - 60): m.end() + 60]
            if any(re.search(field_pattern(f), window) for f in atom.fields):
                return True
        return False
    if any(re.search(field_pattern(f), text) for f in atom.fields):
        return True
    if atom.kind == "compare" and atom.fields:
        # `ageDays <= REFUND_WINDOW_DAYS` is named by "within 30 days": the
        # compared value's significant words count. Only the left side's — the
        # right side is usually a constant or a variable whose words ("refund
        # window") are the feature's name and appear everywhere.
        words = {w for w in _words(atom.fields[0]) if len(w) >= 4 and w not in _GENERIC_WORDS}
        return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) for w in words)
    return False


_GENERIC_WORDS = frozenset({"value", "values", "item", "items", "data", "list", "count", "index",
                            "result", "results", "normalized", "current", "active", "matches", "string"})


# --- client auth config ------------------------------------------------------

#: Scopes every OIDC client asks for; they say nothing about what an API accepts.
STANDARD_SCOPES = frozenset({"openid", "email", "profile", "offline_access", "phone", "address"})

_SCOPE_RES = (
    re.compile(r"<key>\s*scopes?\s*</key>\s*<string>([^<]+)</string>", re.I),
    re.compile(r"<key>\s*scopes?\s*</key>\s*<array>(.*?)</array>", re.I | re.S),
    re.compile(r"""["']?\b(?:scopes?|SCOPES?|defaultScopes|requestedScopes)\b["']?\s*[:=]\s*(\[[^\]]*\]|"[^"]*"|'[^']*'|`[^`]*`|[^\s,;]+)""", re.S),
    re.compile(r"""\.(?:setScopes?|scopes?)\s*\(\s*([^)]*)\)"""),
    re.compile(r"""\b(?:OKTA|OIDC|OAUTH|AUTH)_?SCOPES?\s*=\s*(.+)$""", re.M),
)
_AUDIENCE_RES = (
    re.compile(r"<key>\s*audience\s*</key>\s*<string>([^<]+)</string>", re.I),
    re.compile(r"""["']?\baudience\b["']?\s*[:=]\s*["'`]([^"'`]+)["'`]""", re.I),
    re.compile(r"""\b(?:OKTA|OIDC|OAUTH|AUTH)_?AUDIENCE\s*=\s*(\S+)""", re.M),
)
_SCOPE_TOKEN_RE = re.compile(r"[A-Za-z][\w.:/-]*[\w]")
_ENV_WORDS = ("integ", "integration", "dev", "qa", "test", "uat", "staging", "stage", "preprod", "prod", "production")


def _env_of(path: str) -> str | None:
    base = posixpath.basename(path).lower()
    for w in _ENV_WORDS:
        if re.search(rf"(?<![a-z]){w}(?![a-z])", re.sub(r"[-_.]", " ", base)):
            return "prod" if w == "production" else "integ" if w == "integration" else w
    return None


def parse_auth_config(path: str, text: str) -> dict | None:
    """``{path, line, scopes, audience, env}`` from a client's auth config, or None."""
    for rx in _SCOPE_RES:
        m = rx.search(text or "")
        if not m:
            continue
        raw = re.sub(r"</?string>", " ", m.group(1))
        scopes = [s for s in _SCOPE_TOKEN_RE.findall(raw)
                  if s.lower() not in ("string", "array", "listof", "arrayof", "setof", "scope", "scopes")]
        if not scopes:
            continue
        audience = None
        for arx in _AUDIENCE_RES:
            a = arx.search(text)
            if a:
                audience = a.group(1).strip()
                break
        return {"path": path, "line": _line_of(text, m.start(1)), "scopes": scopes,
                "audience": audience, "env": _env_of(path)}
    return None


def api_scopes(scopes: list[str]) -> list[str]:
    """The scopes that grant API access, without the OIDC standard ones."""
    return [s for s in scopes if s.lower() not in STANDARD_SCOPES and not s.lower().startswith("okta.")]


# --- reading -----------------------------------------------------------------


@dataclass
class SourceFile:
    repo: str
    path: str
    ref: str | None
    text: str

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()


@dataclass
class SourceIndex:
    """Files read whole at each PR's head commit, keyed by (repo, path)."""

    files: dict[tuple[str, str], SourceFile] = field(default_factory=dict)
    #: (repo, path) -> why it could not be read. Never confused with "absent".
    unreadable: dict[tuple[str, str], str] = field(default_factory=dict)
    resources: dict[str, list[UIString]] = field(default_factory=dict)
    auth_configs: list[dict] = field(default_factory=list)
    platforms: dict[str, str] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    #: repo -> its tree at the ref, or None when it could not be listed. The
    #: tree, not a fetch, says whether a path exists: GitHubClient returns
    #: nothing for a timeout and for a 404 alike.
    trees: dict[str, list[str] | None] = field(default_factory=dict)
    #: (repo, path) -> new-file line numbers the PRs added or changed.
    changed_lines: dict[tuple[str, str], set[int]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def candidates(self, locator: str) -> list[tuple[str, str]]:
        path = _clean_locator(locator)
        if not path:
            return []
        known = list(self.files) + list(self.unreadable)
        exact = [k for k in known if k[1] == path]
        return exact or [k for k in known if k[1].endswith("/" + path) or path.endswith("/" + k[1])]

    def resolve(self, locator: str) -> SourceFile | str | None:
        """The file a citation names; a reason string when it could not be
        read; None when no PR file matches at all.

        A citation whose directories are off (``WebController/X.swift`` for
        ``WebController/X/X.swift``) resolves to the one read file with that
        name, when there is exactly one."""
        keys = self.candidates(locator)
        if not keys:
            base = posixpath.basename(_clean_locator(locator))
            same = [k for k in self.files if posixpath.basename(k[1]) == base]
            keys = same if len(same) == 1 else []
        for k in keys:
            if k in self.files:
                return self.files[k]
        for k in keys:
            if k in self.unreadable:
                return self.unreadable[k]
        return None

    def literals_for(self, src: SourceFile) -> list[UIString]:
        """Every string the cited file can show: its own literals and the
        resource values of the keys it references."""
        own = [UIString(v, src.path, n, None, src.repo) for v, n in string_literals(src.path, src.text)]
        own += [UIString(u.text, u.path, u.line, u.key, src.repo) for u in ui_strings(src.path, src.text)]
        refs = resolve_keys(referenced_keys(src.text), self.resources.get(src.repo, []))
        return own + refs


_LINE_SUFFIX_RE = re.compile(r":(\d+)(?:\s*[-–]\s*(\d+))?\s*$")


def _clean_locator(locator: str) -> str:
    loc = (locator or "").strip()
    loc = re.sub(r"^(?:pr|file):", "", loc)
    loc = re.split(r"\s+\(|\s+—|\s+-\s", loc)[0].strip()
    loc = _LINE_SUFFIX_RE.sub("", loc).strip().lstrip("./")
    return loc


def parse_locator(locator: str) -> tuple[str, tuple[int, int] | None, list[str]]:
    """``(path, (start, end) or None, named symbols)`` from a citation.

    Accepts ``path:12``, ``path:12-30``, ``path (fnA + Type.fnB)`` and the
    combinations a model writes. Symbols are identifiers named in the aside.
    """
    raw = (locator or "").strip()
    raw = re.sub(r"^(?:pr|file):", "", raw)
    head = re.split(r"\s+\(|\s+—|\s+-\s", raw)[0].strip()
    rng = None
    m = _LINE_SUFFIX_RE.search(head)
    if m:
        a = int(m.group(1))
        rng = (a, int(m.group(2)) if m.group(2) else a)
    aside = raw[len(head):]
    symbols = []
    for ident in re.findall(r"[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*", aside):
        last = ident.split(".")[-1]
        if re.search(r"[a-z][A-Z]|_", last) and last not in symbols:
            symbols.append(last)
    return _clean_locator(raw), rng, symbols


def added_lines(patch: str | None) -> set[int]:
    """New-file line numbers a unified diff adds."""
    out: set[int] = set()
    n = 0
    for line in (patch or "").splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            n = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            out.add(n)
            n += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif n:
            n += 1
    return out


def _pr_ref(pr: dict) -> str | None:
    return pr.get("head_sha") or pr.get("merge_commit_sha")


async def read_source_index(github, pull_requests: list[dict] | None) -> SourceIndex:
    """Read the changed source files whole, plus locale resources and client
    auth configs from each repo's tree, at each PR's head commit.

    Never raises. A file GitHub would not return is recorded in ``unreadable``
    with the reason, so a later check says "could not re-read" rather than
    "not in the code" — a failed read is not evidence of absence.
    """
    from .build_targets import detect_platform

    index = SourceIndex()
    by_repo: dict[str, list[dict]] = {}
    for pr in pull_requests or []:
        if isinstance(pr, dict) and pr.get("repository") and not pr.get("github_enrichment_failed"):
            by_repo.setdefault(pr["repository"], []).append(pr)
    for repo, prs in by_repo.items():
        ref = next((r for r in (_pr_ref(p) for p in prs) if r), None)
        if ref:
            index.refs[repo] = ref
        changed: list[str] = []
        for pr in prs:
            for f in pr.get("files_changed") or []:
                name = f.get("filename") if isinstance(f, dict) else None
                if not name or f.get("status") == "removed":
                    continue
                index.changed_lines.setdefault((repo, name), set()).update(added_lines(f.get("patch")))
                if is_source_path(name) and name not in changed:
                    changed.append(name)
                    if f.get("full_content"):
                        index.files[(repo, name)] = SourceFile(repo, name, ref, f["full_content"])
        try:
            tree = await github.list_tree(repo, ref) if ref else None
        except Exception as e:  # noqa: BLE001 — recorded, never raised
            tree = None
            index.errors.append(f"could not list {repo}: {type(e).__name__}")
        index.trees[repo] = list(tree) if tree is not None else None
        index.platforms[repo] = detect_platform(tree or [], changed)
        want = [p for p in changed if (repo, p) not in index.files][:MAX_CHANGED_FILES]
        locale = [p for p in (tree or []) if is_locale_resource(p)][:MAX_RESOURCE_FILES]
        auth = ([p for p in (tree or []) if is_auth_config(p)][:MAX_AUTH_FILES]
                if index.platforms[repo] in CLIENT_PLATFORMS else [])
        paths = list(dict.fromkeys(want + locale + auth))
        got: dict[str, str] = {}
        if ref and paths:
            try:
                got = await github.fetch_files_at_ref(repo, ref, paths, max_bytes=MAX_FILE_BYTES) or {}
            except Exception as e:  # noqa: BLE001
                index.errors.append(f"could not read files from {repo}: {type(e).__name__}")
        for p in paths:
            if p in got:
                index.files[(repo, p)] = SourceFile(repo, p, ref, got[p])
            elif p in want:
                index.unreadable[(repo, p)] = (
                    f"{p} could not be read at {(ref or 'the PR head')[:7]}" if ref
                    else f"{p}: the PR has no head commit to read it at")
        resources: list[UIString] = []
        for p in locale:
            if p in got:
                resources += [UIString(u.text, u.path, u.line, u.key, repo) for u in resource_strings(p, got[p])]
        for (r, p), src in index.files.items():
            if r == repo and is_locale_resource(p) and p not in locale:
                resources += [UIString(u.text, u.path, u.line, u.key, repo) for u in resource_strings(p, src.text)]
        index.resources[repo] = resources
        for p in auth:
            if p in got:
                parsed = parse_auth_config(p, got[p])
                if parsed:
                    index.auth_configs.append({"repo": repo, "platform": index.platforms[repo], **parsed})
    return index


async def read_cited(index: SourceIndex, github, locators: list[str]) -> None:
    """Read cited files the index does not hold yet (an exploration finding can
    cite a file no PR changed), in the repo whose tree has them, at its PR ref.

    A path no listed tree contains is left unresolved, which the verifier
    reports as not in the linked repos. A path a tree contains but the fetch
    did not return, or one we could not look for because a tree was not
    listed, is recorded as unreadable: the fetch cannot tell a timeout from a
    404, and only the tree can say a file does not exist.
    """
    for loc in locators:
        path = _clean_locator(loc)
        if not path or index.candidates(path):
            continue
        found = False
        for repo, ref in index.refs.items():
            tree = index.trees.get(repo)
            if tree is None:
                continue
            hits = [p for p in tree if p == path or p.endswith("/" + path)]
            if len(hits) != 1:
                continue
            found = True
            try:
                got = await github.fetch_files_at_ref(repo, ref, hits, max_bytes=MAX_FILE_BYTES) or {}
            except Exception as e:  # noqa: BLE001
                got, why = {}, type(e).__name__
            else:
                why = "not returned"
            if hits[0] in got:
                index.files[(repo, hits[0])] = SourceFile(repo, hits[0], ref, got[hits[0]])
            else:
                index.unreadable[(repo, hits[0])] = f"{hits[0]} could not be read from {repo} ({why})"
            break
        if not found:
            unlisted = [r for r in index.refs if index.trees.get(r) is None]
            if unlisted:
                index.unreadable[(unlisted[0], path)] = (
                    f"{path} could not be looked for: {', '.join(unlisted)} could not be listed")


# --- the facts the generator is shown ---------------------------------------

#: Prompt budget for the facts section. On SK-2719 (3 PRs, 43 changed files)
#: the first cut of this listed 200 strings and 11 functions in 49k characters
#: and still left out the resolver the plan got wrong — R.generated.swift
#: spent half the string slots and view-lifecycle methods spent the function
#: budget. Hence: strings the PR added only, generated files skipped, and
#: functions ranked by how much they look like a rule.
MAX_UI_STRINGS = 120
MAX_UI_STRINGS_PER_FILE = 25
FUNCTION_BUDGET_CHARS = 12_000
FUNCTION_CAP_CHARS = 3_500

_GENERATED_RE = re.compile(r"(?:^|/)(?:R\.generated\.swift|[^/]*\.generated\.[^/]+|Generated/|__generated__/|"
                           r"[^/]*\.g\.dart|[^/]*_pb2?\.py|[^/]*\.pb\.\w+)$|(?:^|/)Generated/")
_COLLECTION_OP_RE = re.compile(r"\.(?:filter|where|first|find|min|max|sorted|sort|reduce|contains|includes|"
                               r"some|every|any|all|compactMap|flatMap|filterNot|takeWhile|dropWhile|"
                               r"Where|First|Any|All|OrderBy)\b|\bguard\b|\bfilter\(|\bany\(|\ball\(")
_LIFECYCLE_RE = re.compile(r"^(?:viewDid|viewWill|viewSafeArea|layoutSubviews|draw|webView|userContentController|"
                           r"tableView|collectionView|scrollView|onCreate|onResume|onPause|onDestroy|render|"
                           r"componentDid|componentWill|ngOn|setUp|tearDown|init|deinit|awakeFromNib)")


def is_generated(path: str) -> bool:
    return bool(_GENERATED_RE.search(path or ""))


def _rule_score(fn: Function, atoms: list[Atom]) -> float:
    kinds = [a.kind for a in atoms]
    score = (3 * kinds.count("aggregate") + 2 * kinds.count("literal")
             + kinds.count("nonnull") + kinds.count("bool") + 0.5 * kinds.count("compare"))
    body = "\n".join(line for _, line in fn.body())
    if _COLLECTION_OP_RE.search(body):
        score += 3
    if _LIFECYCLE_RE.match(fn.name):
        score -= 6
    return score


def _predicate_functions(index: SourceIndex) -> list[tuple[SourceFile, Function, list[Function], list[Atom]]]:
    """Changed functions that apply a rule, best first, with their helpers.

    A rule: two or more conditions, at least one of them a value, a null
    check, a boolean or a min/max — not just calls to other predicates.
    """
    out = []
    for (repo, path), src in index.files.items():
        changed = index.changed_lines.get((repo, path))
        if (not changed or is_generated(path) or is_locale_resource(path)
                or _ext(path) in (".json", ".plist", ".xml", ".strings", ".xcstrings")):
            continue
        fns = functions(path, src.text)
        for fn in fns:
            if not any(fn.start <= n <= fn.end for n in changed):
                continue
            group = with_helpers(fn, fns)
            atoms = condition_atoms(path, [b for g in group for b in g.body()[1:]])
            if len(atoms) < 2 or not any(a.kind in ("literal", "aggregate", "nonnull", "bool") for a in atoms):
                continue
            out.append((_rule_score(fn, atoms), src, fn, group, atoms))
    out.sort(key=lambda t: -t[0])
    return [(src, fn, group, atoms) for score, src, fn, group, atoms in out if score > 0]


def build_source_facts(index: SourceIndex, token_sources: list[dict] | None = None) -> dict:
    """The JSON-safe facts the prompt renders and the verifier re-checks.

    ``shown`` records the line ranges whose text the generator sees in this
    section, so the verifier can tell a rule the generator read whole from one
    it could only have guessed the rest of.
    """
    ui: list[dict] = []
    for (repo, path), src in index.files.items():
        changed = index.changed_lines.get((repo, path)) or set()
        if not changed or is_auth_config(path):
            continue
        strings = [u for u in ui_strings(path, src.text) if u.line in changed]
        ui += [{**u.as_dict(), "repo": repo} for u in strings[:MAX_UI_STRINGS_PER_FILE]]
        if is_generated(path):
            continue
        refs = resolve_keys(referenced_keys("\n".join(src.lines[n - 1] for n in changed
                                                      if 0 < n <= len(src.lines))),
                            index.resources.get(repo, []))
        ui += [{**r.as_dict(), "repo": repo, "referenced_from": path} for r in refs[:MAX_UI_STRINGS_PER_FILE]]
    seen_ui: set[tuple] = set()
    ui = [u for u in ui if not ((u["text"], u["path"], u["line"]) in seen_ui
                               or seen_ui.add((u["text"], u["path"], u["line"])))]
    # Generated files last: their strings are usually defined again in source.
    ui.sort(key=lambda u: is_generated(u["path"]))
    ui = ui[:MAX_UI_STRINGS]

    fns: list[dict] = []
    bodies: dict[tuple[str, int], dict] = {}
    shown: dict[str, list[list[int]]] = {}
    spent = 0
    for src, fn, group, atoms in _predicate_functions(index):
        new = [g for g in group if (src.path, g.start) not in bodies]
        texts = []
        for g in new:
            text = "\n".join(f"{n:5d}| {line}" for n, line in g.body())
            cut = len(text) > FUNCTION_CAP_CHARS
            if cut:
                text = text[:FUNCTION_CAP_CHARS].rsplit("\n", 1)[0] + "\n  … (function continues past the budget)"
            texts.append((g, text, cut))
        size = sum(len(t) for _, t, _ in texts)
        if spent + size > FUNCTION_BUDGET_CHARS:
            continue
        spent += size
        for g, text, cut in texts:
            bodies[(src.path, g.start)] = {"repo": src.repo, "path": src.path, "name": g.name,
                                           "start": g.start, "end": g.end, "text": text}
            if not cut:
                shown.setdefault(src.path, []).append([g.start, g.end])
        fns.append({"repo": src.repo, "path": src.path, "name": fn.name, "start": fn.start, "end": fn.end,
                    "helpers": [g.name for g in group[1:]], "conditions": [a.as_dict() for a in atoms]})

    return {
        "ui_strings": ui,
        "functions": fns,
        "bodies": sorted(bodies.values(), key=lambda b: (b["path"], b["start"])),
        "client_auth": list(index.auth_configs),
        "token_sources": list(token_sources or []),
        "shown": shown,
        "unreadable": sorted(f"{r}:{p} — {why}" for (r, p), why in index.unreadable.items()),
        "errors": list(index.errors),
    }


def render_source_facts(facts: dict | None) -> str:
    """The prompt section. Empty when there is nothing to show."""
    if not facts or not any(facts.get(k) for k in ("ui_strings", "functions", "client_auth", "unreadable")):
        return ""
    out = ["━━ SOURCE FACTS — read from the changed files whole, at the PR head commit ━━"]
    if facts.get("ui_strings"):
        out.append("\nUI STRINGS (verbatim; a step that names on-screen text quotes one of these exactly):")
        for u in facts["ui_strings"]:
            key = f" [{u['key']}]" if u.get("key") and u["key"] != u["text"] else ""
            out.append(f'- "{u["text"]}" — {u["path"]}:{u["line"]}{key}')
    if facts.get("functions"):
        out.append("\nRULES IN THE CHANGED CODE (whole functions with the same-file helpers they call; "
                   "a restated rule names EVERY condition listed and cites the range):")
        for f in facts["functions"]:
            out.append(f"- {f['path']}:{f['start']}-{f['end']} `{f['name']}`"
                       + (f" (calls {', '.join(f['helpers'])})" if f["helpers"] else "")
                       + ": " + "; ".join(f"{c['condition']} (line {c['line']})" for c in f["conditions"]))
        for b in facts.get("bodies") or []:
            out.append(f"\n--- {b['path']}:{b['start']}-{b['end']} `{b['name']}` ---")
            out.append(b["text"])
    if facts.get("client_auth"):
        out.append("\nCLIENT TOKENS (what each client app's sign-in requests; a backend case that "
                   "reproduces a client call needs a token with these scopes):")
        for a in facts["client_auth"]:
            aud = f", audience {a['audience']}" if a.get("audience") else ""
            out.append(f"- {a['repo']} ({a['platform']}{', ' + a['env'] if a.get('env') else ''}): "
                       f"{a['path']}:{a['line']} scopes `{' '.join(a['scopes'])}`{aud}")
    if facts.get("client_auth") or facts.get("token_sources") is not None:
        srcs = facts.get("token_sources") or []
        if srcs:
            out.append("\nTOKENS THE RUNNER CAN OBTAIN:")
            for s in srcs:
                out.append(f"- {s.get('name', 'unnamed')}: scopes `{' '.join(s.get('scopes') or [])}`"
                           + (f", audience {s['audience']}" if s.get("audience") else ""))
        elif facts.get("client_auth"):
            out.append("\nTOKENS THE RUNNER CAN OBTAIN: none configured — no token is known to carry "
                       "a client's scopes, so a case that needs one is marked needs_client_equivalent_token.")
    if facts.get("unreadable"):
        out.append("\nCOULD NOT READ (claims about these files cannot be verified):")
        out += [f"- {u}" for u in facts["unreadable"]]
    return "\n".join(out)
