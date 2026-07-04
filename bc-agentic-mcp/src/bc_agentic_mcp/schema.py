"""schema — deterministic schema reconciliation & upgrade preflight (pure logic).

Two failure modes this session exposed, encoded as pure functions:

* Target reconciliation: "extend the API" must expose fields that ALREADY EXIST. Diff the
  spec's requested fields against the *deployed* metadata (OData ``$metadata`` or a provided
  field list) so we never recreate an existing field.
* Upgrade preflight: an app upgrade is rejected if it would REMOVE fields/tables the deployed
  baseline still has. Diff current vs baseline schema and flag removals as breaking.

Everything is input-driven and reproducible; metadata is fetched through an injectable seam.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set
from xml.etree import ElementTree as ET


def normalize_name(name: str) -> str:
    """Canonical form for comparing field/table names (case- and quote-insensitive)."""
    return re.sub(r"\s+", " ", str(name or "").strip().strip('"').lower())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_odata_metadata(xml_text: str) -> Dict[str, Set[str]]:
    """Parse an OData ``$metadata`` (EDMX) document into {EntityType: {property names}}.

    Namespace-agnostic (compares local element names), so it works across CSDL versions.
    """
    out: Dict[str, Set[str]] = {}
    if not (xml_text or "").strip():
        return out
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for el in root.iter():
        if _local(el.tag) == "EntityType":
            name = el.get("Name") or ""
            props: Set[str] = set()
            for child in el.iter():
                if _local(child.tag) == "Property":
                    pn = child.get("Name")
                    if pn:
                        props.add(pn)
            out[name] = props
    return out


def reconcile_fields(
    requested: List[str],
    deployed: List[str],
) -> Dict[str, Any]:
    """Split requested fields into those that already exist vs genuinely new.

    Deterministic set logic on normalized names; preserves the caller's original spelling.
    """
    deployed_norm = {normalize_name(d) for d in deployed}
    existing: List[str] = []
    new: List[str] = []
    for r in requested:
        (existing if normalize_name(r) in deployed_norm else new).append(r)
    return {
        "requested_count": len(requested),
        "existing": existing,
        "new": new,
        # If the item says "extend the API", any 'new' field is a red flag (likely a stale
        # local checkout) — the caller decides, we just surface it deterministically.
        "all_requested_exist": len(new) == 0,
    }


def diff_schema(
    current_fields: List[str],
    baseline_fields: List[str],
    *,
    current_tables: Optional[List[str]] = None,
    baseline_tables: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Diff a candidate schema against the deployed baseline. Removals are breaking."""
    cur_f = {normalize_name(x): x for x in current_fields}
    base_f = {normalize_name(x): x for x in baseline_fields}
    removed_fields = [base_f[k] for k in base_f.keys() - cur_f.keys()]
    added_fields = [cur_f[k] for k in cur_f.keys() - base_f.keys()]

    cur_t = {normalize_name(x): x for x in (current_tables or [])}
    base_t = {normalize_name(x): x for x in (baseline_tables or [])}
    removed_tables = [base_t[k] for k in base_t.keys() - cur_t.keys()]
    added_tables = [cur_t[k] for k in cur_t.keys() - base_t.keys()]

    breaking = bool(removed_fields or removed_tables)
    return {
        "removed_fields": sorted(removed_fields),
        "added_fields": sorted(added_fields),
        "removed_tables": sorted(removed_tables),
        "added_tables": sorted(added_tables),
        "breaking": breaking,
        "reason": (
            "Upgrade would REMOVE fields/tables the deployed baseline still has — rejected."
            if breaking else "No removals detected; upgrade is schema-compatible."
        ),
    }


MetadataFetcher = Callable[[str], str]


def _default_metadata_fetcher(url: str) -> str:
    from urllib import request as _r
    with _r.urlopen(url, timeout=60) as resp:  # noqa: S310 (caller-provided URL)
        return resp.read().decode("utf-8", "replace")


def reconcile_against_endpoint(
    *,
    requested: List[str],
    metadata_url: str,
    entity: str = "",
    fetcher: Optional[MetadataFetcher] = None,
) -> Dict[str, Any]:
    """Fetch OData $metadata (seam) and reconcile requested fields against it."""
    fetch = fetcher or _default_metadata_fetcher
    xml_text = fetch(metadata_url)
    parsed = parse_odata_metadata(xml_text)
    if entity:
        deployed = sorted(parsed.get(entity, set()))
    else:
        deployed = sorted({p for props in parsed.values() for p in props})
    result = reconcile_fields(requested, deployed)
    result["entity"] = entity
    result["deployed_field_count"] = len(deployed)
    return result
