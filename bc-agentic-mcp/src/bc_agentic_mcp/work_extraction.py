"""work_extraction — deterministic router + object/field extractor for BC work items.

Replaces the planner's implicit "every item is a rentalMutation API extension" assumption.
Follows Anthropic's *routing* pattern: classify the request deterministically, then let the
caller route to a specialized (externalized) template. Pure + deterministic — regex over the
item text, with NO domain literals baked in.

Public API:
    classify(text)        -> ordered list[str] of work types (a request can be several)
    extract_objects(text) -> list[dict] concrete AL objects named in the text
    extract_fields(text)  -> list[dict] fields requested to add {name, al_type, editable}
    summarize(text)       -> {work_types, objects, fields}  (one call for the planner)
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Set

# AL data types (for field extraction). Order matters: longer/again-specific first.
_AL_TYPE = (
    r"(Code\[\d+\]|Text\[\d+\]|Integer|BigInteger|Boolean|Date(?:Time|Formula)?|Time|"
    r"Decimal|Guid|Blob|Media|RecordId|Option|Enum\s+[A-Za-z0-9_]+)"
)

# Object kinds we detect by the AL keyword that precedes a name.
_OBJECT_KEYWORDS = (
    "tableextension", "pageextension", "enumextension", "reportextension",
    "permissionset",
    "table", "page", "codeunit", "enum", "report", "query", "xmlport", "interface",
)
_STOPWORDS = {"the", "a", "an", "this", "that", "each", "new", "existing", "id"}

# A genuine AL object name carries a strong signal that a common English noun does not:
# an internal capital (PascalCase, e.g. VeraSpaceDetailTypeFDN) or a known Zig/BC affix.
# This is what separates a real reference ("table VeraSpaceDetailTypeFDN") from common-noun
# prose ("...records in the table" followed by an "Ad 1." heading -> a phantom `table Ad`).
_AFFIX_RE = re.compile(r"(FDN|SAN|OPN|TAI|HSG|WRV)$")


def _looks_like_object_name(name: str) -> bool:
    """True when the token has the shape of a real AL object name (not a prose word)."""
    return bool(re.search(r"[a-z][A-Z]", name) or _AFFIX_RE.search(name))


def _norm(text: str) -> str:
    return (text or "").strip()


# ---------------------------------------------------------------------------
# Classification (deterministic router)
# ---------------------------------------------------------------------------

def classify(text: str) -> List[str]:
    """Classify the work item into one or more work types (primary first)."""
    t = (text or "").lower()
    types: List[str] = []

    def add(name: str, cond: bool) -> None:
        if cond and name not in types:
            types.append(name)

    add("api", bool(re.search(r"\bapi\b|entityname|extend (the )?api|odata", t)))
    add("table-field", bool(re.search(r"\b(add|new)\b[^.]*\bfield\b[^.]*\btable\b|\bfield\b[^.]*\bto (the )?table\b", t)))
    add("page", bool(re.search(r"\bon (the )?page\b|show[^.]*\bon\b[^.]*\bpage\b|\bpage\b.*\bfield\b", t)))
    add("upgrade", bool(re.search(r"\bupgrade\s*codeunit\b|\bdata upgrade\b|\bupgradecodeunit\b|populate[^.]*existing", t)))
    add("enum", bool(re.search(r"\benum(extension)?\b", t)))
    add("report", bool(re.search(r"\breport\b", t)))
    add("permission", bool(re.search(r"\bpermission set\b|\bpermissionset\b|\btabledata\b", t)))
    add("table", bool(re.search(r"\bnew table\b|\bcreate (a )?table\b", t)))
    # Decommission work (typical bugfix lane): objects/values/artifacts must be REMOVED.
    add("removal", bool(re.search(
        r"\b(remove|delete|decommission)\b[^.]{0,80}\b(codeunit|enum ?(value)?|feature|page|table|field|report|job queue)\b"
        r"|\bnot needed anymore\b|\bno longer (needed|used|visible)\b", t)))
    return types or ["unknown"]


# ---------------------------------------------------------------------------
# Object extraction
# ---------------------------------------------------------------------------

def _action_for(kind: str, text: str, name: str) -> str:
    """Heuristic: is this object created or modified?

    A per-object explicit verb wins: 'MODIFY … <name>' / 'extend <name>' beats the
    global 'add/create appears somewhere' heuristic (observed live: 'MODIFY … add
    value X' was misread as create because 'add' appeared in the sentence).
    """
    window = text.lower()
    name_low = name.lower()
    # Explicit per-object verbs (look at the 120 chars before the name mention).
    idx = window.find(name_low)
    if idx >= 0:
        before = window[max(0, idx - 120):idx]
        # Removal verbs win FIRST: 'Remove codeunit X' is decommission work — the
        # 'codeunits are almost always newly delivered' heuristic misread it as a
        # create (observed live on Bug 267600, blocked by the spec contract gate).
        # BUT removing 'a value/field ... ' FROM an object is a MODIFICATION of that
        # object, not its removal — the part word may sit before OR right after the
        # object name ('Remove enumextension HousingFeatureHSG value X', observed live).
        m_rm = re.search(r"\b(remove|delete|decommission|drop)\b([^.]*)$", before)
        if m_rm:
            part_words = r"\b(value|field|column|action|entry|record)s?\b"
            part_before = re.search(part_words, m_rm.group(2))
            part_after = re.match(rf"[^.\n]{{0,40}}?{part_words}",
                                  window[idx + len(name_low):])
            return "modify" if (part_before or part_after) else "remove"
        if re.search(r"\b(modify|extend|change|update)\b[^.]*$", before):
            return "modify"
        # create/new counts only when the OBJECT itself is created — 'a new FIELD/value
        # on <object>' is a modification of that object, not a creation of it.
        if re.search(r"\b(create|new)\b(?![^.]*\b(field|value|column|action|record)s?\b)[^.]*$", before):
            return "create"
    if kind in ("codeunit", "report", "query", "xmlport", "enum", "interface",
                "tableextension", "pageextension", "enumextension"):
        # These are almost always newly delivered by a work item.
        if re.search(r"\b(deliver|add|create|new|introduce)\b", window):
            return "create"
    if re.search(rf"\bnew\s+{re.escape(kind)}\b", window):
        return "create"
    # Existing table/page that we extend or show a field on => modify.
    return "modify"


def extract_objects(text: str) -> List[Dict[str, Any]]:
    """Extract concrete AL objects named in the item text (deterministic)."""
    text = _norm(text)
    # Humans write the SPACED kind forms ("enum extension X", "permission set Y",
    # "xml port Z") at least as often as the AL keywords — the spaced forms extracted
    # NOTHING and the objects silently fell out of scope (observed live on Bug 267600
    # fix-11: three enumextension modifies vanished from allowed_files →
    # scope_violation on write; suspicion sweep then found permission set / xml port /
    # report extension equally blind).
    text = re.sub(r"\b(enum|table|page|report)\s+extensions?\b",
                  lambda m: f"{m.group(1).lower()}extension", text, flags=re.IGNORECASE)
    text = re.sub(r"\bpermission\s+sets?\b", "permissionset", text, flags=re.IGNORECASE)
    text = re.sub(r"\bxml\s+ports?\b", "xmlport", text, flags=re.IGNORECASE)
    objects: List[Dict[str, Any]] = []
    seen: Set[tuple] = set()

    kinds_alt = "|".join(_OBJECT_KEYWORDS)
    # An object mentioned as a PRECEDENT/example is a reference, not work — emitting it
    # as work created phantom objects_to_create (observed live: 'per precedent codeunit
    # 11234919 ChangeNonNetRentFeature' became an unresolvable create).
    def _is_reference(match_start: int) -> bool:
        before = text[max(0, match_start - 60):match_start].lower()
        return bool(re.search(r"\b(precedent|mirror(?:ing)?|following|as in|like|see)\b[^.;]{0,50}$", before))

    pattern = re.compile(
        rf'\b({kinds_alt})s?\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))(?:\s*\(\s*id\s*(\d+)\s*\))?',
        re.IGNORECASE,
    )
    # AL-native header form: `<kind> <number> <Name>` (as pasted from source or
    # written by a precise item: "enumextension 11234914 FeatureExt"). The plural
    # form ("codeunits 51118 X and 51117 Y") names SEVERAL objects — the trailing
    # enumeration is captured by _ENUMERATION below (observed live on Bug 267600:
    # both test-codeunit removals silently dropped from scope).
    al_native = re.compile(
        rf'\b({kinds_alt})s?\s+(\d{{3,10}})\s+("[^"]+"|[A-Za-z_][A-Za-z0-9_]*)',
        re.IGNORECASE,
    )
    _ENUMERATION = re.compile(
        r'\s*(?:,|and|&)\s+(\d{3,10})\s+("[^"]+"|[A-Za-z_][A-Za-z0-9_]*)',
        re.IGNORECASE,
    )
    for m in al_native.finditer(text):
        kind = m.group(1).lower()
        raw_name = m.group(3)
        name = raw_name.strip('"')
        # Prose guard: 'codeunit 11282398 are deleted' is a SENTENCE, not a header —
        # real AL object names are PascalCase (or quoted). An all-lowercase bare word
        # after the id is prose (observed live: phantom `codeunit are` blocked a spec).
        if not raw_name.startswith('"') and name.islower():
            continue
        if name.lower() in _STOPWORDS or _is_reference(m.start()):
            continue
        key = (kind, name.lower())
        if key not in seen:
            seen.add(key)
            objects.append({
                "kind": kind,
                "name": name,
                "id": m.group(2),
                "action": _action_for(kind, text, name),
            })
        # Enumerated continuation: ", NNN Name" / "and NNN Name" inherit the kind.
        pos = m.end()
        while True:
            cont = _ENUMERATION.match(text, pos)
            if not cont:
                break
            cname = cont.group(2).strip('"')
            ckey = (kind, cname.lower())
            if cname.lower() not in _STOPWORDS and ckey not in seen:
                seen.add(ckey)
                objects.append({
                    "kind": kind,
                    "name": cname,
                    "id": cont.group(1),
                    "action": _action_for(kind, text, cname),
                })
            pos = cont.end()
    for m in pattern.finditer(text):
        kind = m.group(1).lower()
        quoted_name = m.group(2)
        name = quoted_name or m.group(3)
        if not name or name.lower() in _STOPWORDS or _is_reference(m.start()):
            continue
        tail = text[m.end():]
        # The pattern captures a trailing "(id N)" into group(4); also accept a
        # detached id that follows. Keying off the id is what lets a simple (non-affixed)
        # name like `Rental` resolve on its explicit id.
        has_id = bool(m.group(4)) or bool(re.match(r"\s*\(\s*id\s*\d+", tail, re.IGNORECASE))
        quoted = bool(quoted_name)
        # Accept only genuine AL object references. Require a strong signal — an explicit
        # "(id N)", quotes, or a real-object-name shape — so common-noun usage such as
        # "records in the table" followed by an "Ad 1." heading does not yield a phantom
        # `table Ad`. Key off the NAME, not the tail: real names may legitimately be followed
        # by a capitalised word (e.g. "table VeraSpaceDetailTypeFDN Properties ...").
        if not (has_id or quoted or _looks_like_object_name(name)):
            continue
        key = (kind, name.lower())
        if key in seen:
            continue
        seen.add(key)
        objects.append({
            "kind": kind,
            "name": name,
            "id": m.group(4),
            "action": _action_for(kind, text, name),
        })

    # Upgrade codeunit is often described without a name ("a data upgrade codeunit").
    if re.search(r"\bupgrade\s*codeunit\b|\bdata upgrade\b", text, re.IGNORECASE) \
            and not any(o["kind"] == "codeunit" for o in objects):
        objects.append({"kind": "codeunit", "name": None, "id": None,
                         "action": "create", "subtype": "upgrade"})

    # A NAMED upgrade codeunit ("upgrade codeunit JobQueueCleanupHSG") must carry the
    # upgrade subtype too — it drives placement and the upgrade contract.
    for o in objects:
        if o.get("kind") == "codeunit" and o.get("name") and not o.get("subtype"):
            if re.search(rf'upgrade\s+codeunit\s+"?{re.escape(o["name"])}"?', text, re.IGNORECASE):
                o["subtype"] = "upgrade"

    # Explicit file paths: an item that names the exact .al file (e.g.
    # "extensions/BaseApp/src/EmpireFeatures/FacilitiesPerSpaceFeature.Codeunit.al")
    # has ALREADY grounded the object — honor it instead of guessing a placement.
    # Repo convention drops the module affix in FILE names (codeunit
    # RealEstateObjectFeatureFDN lives in RealEstateObjectFeature.Codeunit.al),
    # so the affix-stripped name must be tried too.
    for o in objects:
        if not o.get("name"):
            continue
        candidates = [o["name"]]
        # Longest affix first: test-app names carry FDNT/SANT/EMPT/HSGT (precedent:
        # codeunit RealEstateObjectFeatureFDNT lives in RealEstateObjectFeature.Codeunit.al);
        # production modules also use HSG/OPN/TAI/WRV besides FDN/SAN/EMP.
        stripped = re.sub(r"(FDNT|SANT|EMPT|HSGT|FDN|SAN|EMP|HSG|OPN|TAI|WRV)$", "",
                          o["name"], flags=re.IGNORECASE)
        if stripped and stripped != o["name"]:
            candidates.append(stripped)
        # Permission sets (and other quoted multi-word names) squash to lowercase
        # alphanumerics in FILE names: permissionset "2C-ALG-PAGINA ALLEN" lives in
        # 2calgpaginaallen.permissionset.al (observed live: explicit target silently
        # unbound, write got scope_violation).
        squashed = re.sub(r"[^A-Za-z0-9]", "", o["name"])
        if squashed and squashed.lower() != o["name"].lower():
            candidates.append(squashed)
        # Two objects may share a stripped filename (BaseApp codeunit + its test).
        # Bind each object to the path CLOSEST to its own mention — first-match
        # binding gave the test codeunit its production twin's path (observed live).
        mention = text.lower().find(o["name"].lower())
        best = None
        for cand in candidates:
            for m in re.finditer(
                    rf'([A-Za-z0-9_.\-\\/]+[\\/]{re.escape(cand)}\.[A-Za-z]+\.al)\b',
                    text, re.IGNORECASE):
                distance = abs(m.start() - mention) if mention >= 0 else m.start()
                if best is None or distance < best[0]:
                    best = (distance, m.group(1))
            if best is not None:
                break
        if best is not None:
            o["path"] = best[1].replace("/", "\\")
    return objects


# ---------------------------------------------------------------------------
# Declared test-shape extraction (spec-time test pyramid)
# ---------------------------------------------------------------------------

_TEST_LINE_RE = re.compile(
    r"^[\s\-*]*TEST\s+(happy|negative|edge|regression|api)\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


def extract_declared_tests(text: str) -> List[Dict[str, str]]:
    """Parse `TEST <shape>: <scenario>` lines — the PBI-lane declaration template.

    The test pyramid is enforced at SPEC time (Bug 267600 lesson: 3 happy/edge tests
    passed the container while negative+api shapes were missing until a human caught
    it). Declared lines flow into the machine spec as acceptance tests with an explicit
    ``path_shape``, and the review quality gate refuses a plan whose declared shapes
    are incomplete.
    """
    out: List[Dict[str, str]] = []
    for m in _TEST_LINE_RE.finditer(_norm(text)):
        out.append({"shape": m.group(1).lower(), "scenario": m.group(2).strip()})
    return out


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def extract_fields(text: str) -> List[Dict[str, Any]]:
    """Extract requested fields: {name, al_type, editable}."""
    text = _norm(text)
    fields: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    # Pattern: name 'X' ... type <AL type>  (captures a following NotEditable within ~40 chars)
    for m in re.finditer(r"name\s+'([^']+)'[^.\n]*?type\s+" + _AL_TYPE, text, re.IGNORECASE):
        name = m.group(1).strip()
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        window = text[m.start():m.end() + 40]
        editable = not re.search(r"not\s*editable", window, re.IGNORECASE)
        fields.append({"name": name, "al_type": m.group(2).strip(), "editable": editable})

    # Pattern B — BC work-item "Captions/Type" phrasing (no inline `name 'X'`):
    #   Captions: ENU: <Caption> / NLD: ... / Type: [Text] <AL type> / Not Editable
    if not fields:
        type_m = re.search(r"\btype\b\s*:?\s*(?:text\s+|code\s+)?" + _AL_TYPE, text, re.IGNORECASE)
        enu_vals = [mm.group(1).strip() for mm in re.finditer(
            r"\bENU\b\s*:?\s*([^\n|]+?)\s*(?:\r?\n|NLD\b|tooltip|type\b|$)", text, re.IGNORECASE)]
        enu_vals = [v.rstrip(":").strip() for v in enu_vals if v.strip()]
        if type_m and enu_vals:
            # The caption is the shortest ENU value (a tooltip ENU is a full sentence).
            name = min(enu_vals, key=len)
            if name and name.lower() not in seen:
                editable = not re.search(r"not\s*editable", text, re.IGNORECASE)
                fields.append({"name": name, "al_type": type_m.group(1).strip(), "editable": editable})
    return fields


def summarize(text: str) -> Dict[str, Any]:
    """One call for the planner: work types, objects and fields.

    Declared TEST lines describe PROOF, not work — a 'TEST api: …' scenario must not
    route the item into the API template (observed live: the api work-type flipped the
    router and produced an empty spec). Classification and extraction run on the text
    WITHOUT the declared-test lines; the lines themselves are read separately by
    :func:`extract_declared_tests`.
    """
    work_text = _TEST_LINE_RE.sub("", _norm(text))
    return {
        "work_types": classify(work_text),
        "objects": extract_objects(work_text),
        "fields": extract_fields(work_text),
    }
