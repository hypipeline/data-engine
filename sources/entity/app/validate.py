"""
Post-report validation: registry-gated confidence.

Two-phase validation:
1. Check if the claimed entity was confirmed by a registry tool during research.
2. If not, auto-verify by calling the appropriate registry tools ourselves.
3. If auto-verify finds the entity → keep the report.
4. If auto-verify doesn't find it → INSUFFICIENT (hallucination).

Usage:
    from validate import validate_report
    report, flags = await validate_report(report, tool_log)
"""

from __future__ import annotations
import asyncio


# Registry tool names and the URL domains they correspond to
REGISTRY_TOOLS = {
    "search_companies_house": "company-information.service.gov.uk",
    "search_delaware": "icis.corp.delaware.gov",
    "fetch_sec_submissions": "data.sec.gov",
    "search_sec_company": "sec.gov",
    "fetch_sec_filing": "sec.gov",
    "search_northdata": "northdata.com",
}

# Which registry tools to try for auto-verification, by jurisdiction keyword
JURISDICTION_REGISTRY_MAP = {
    "uk": ["search_companies_house"],
    "england": ["search_companies_house"],
    "scotland": ["search_companies_house"],
    "wales": ["search_companies_house"],
    "united kingdom": ["search_companies_house"],
    "delaware": ["search_delaware"],
    "us-de": ["search_delaware"],
    "us": ["search_sec_company", "search_delaware"],
    "united states": ["search_sec_company", "search_delaware"],
    "new york": ["search_sec_company"],
    "germany": ["search_northdata"],
    "france": ["search_northdata"],
    "netherlands": ["search_northdata"],
    "austria": ["search_northdata"],
    "switzerland": ["search_northdata"],
    "europe": ["search_northdata"],
}


def _name_matches(entity_name: str, text: str) -> bool:
    """Check if entity_name appears in text (case-insensitive, flexible matching)."""
    import re
    name_lower = entity_name.lower()
    text_lower = text.lower()

    # Exact substring match
    if name_lower in text_lower:
        return True

    # Normalize common legal form equivalences
    equivalences = [
        ("aktiengesellschaft", "ag"),
        ("gesellschaft mit beschränkter haftung", "gmbh"),
        ("gesellschaft mit beschrankter haftung", "gmbh"),
        ("limited", "ltd"),
        ("incorporated", "inc"),
        ("limited liability company", "llc"),
        ("limited partnership", "lp"),
        ("public limited company", "plc"),
        ("limited liability partnership", "llp"),
    ]

    normalized_name = name_lower
    normalized_text = text_lower
    for long_form, short_form in equivalences:
        normalized_name = normalized_name.replace(long_form, short_form)
        normalized_text = normalized_text.replace(long_form, short_form)

    if normalized_name in normalized_text:
        return True

    # Try without common suffixes entirely
    stripped = re.sub(
        r',?\s*\b(limited|ltd\.?|llc|l\.l\.c\.|inc\.?|incorporated|plc|llp|l\.p\.?|gmbh|'
        r'ag|aktiengesellschaft|s\.a\.?|b\.v\.?|n\.v\.?|co\.?|corp\.?)\s*$',
        '', name_lower, flags=re.IGNORECASE
    ).strip()
    if stripped and len(stripped) > 3 and stripped in text_lower:
        return True

    return False


async def validate_report(report: dict, tool_log: list[dict]) -> tuple[dict, list[str]]:
    """
    Validate a report against actual tool calls, with auto-verification.

    Args:
        report: The agent's JSON report
        tool_log: List of {"tool": name, "input": {}, "output": str} dicts

    Returns:
        (possibly_modified_report, list_of_flags)
    """
    # Import tool dispatch here to avoid circular imports
    from agent import TOOL_DISPATCH

    flags = []
    entity = report.get("recommended_entity")
    if not entity:
        return report, flags

    entity_name = entity.get("legal_entity_name", "")
    source_url = entity.get("source_url", "") or ""
    confidence = report.get("confidence", "insufficient")
    jurisdiction = (entity.get("jurisdiction") or "").lower()

    if confidence not in ("high", "medium"):
        return report, flags

    # Phase 1: Check if any registry tool already confirmed this entity
    confirmed_by_tool = False
    registry_tools_called = set()

    for entry in tool_log:
        tool_name = entry.get("tool", "")
        output = entry.get("output", "")

        if tool_name in REGISTRY_TOOLS:
            registry_tools_called.add(tool_name)
            if _name_matches(entity_name, output):
                confirmed_by_tool = True
                break

    if confirmed_by_tool:
        return report, flags

    # Phase 2: Entity not confirmed — auto-verify with registry tools
    flags.append(
        f"AUTO-VERIFY: '{entity_name}' not found in registry tool output during research. "
        f"Running verification..."
    )

    # Determine which tools to try based on jurisdiction
    tools_to_try = set()
    for keyword, tool_names in JURISDICTION_REGISTRY_MAP.items():
        if keyword in jurisdiction:
            tools_to_try.update(tool_names)

    # Also check the source URL domain to infer which registry to search
    for tool_name, domain in REGISTRY_TOOLS.items():
        if domain in source_url:
            tools_to_try.add(tool_name)

    # If we still don't know, try the most common ones
    if not tools_to_try:
        tools_to_try = {"search_companies_house", "search_sec_company", "search_northdata"}

    # Run the verification tools
    verified = False
    verification_results = []

    for tool_name in tools_to_try:
        if tool_name not in TOOL_DISPATCH:
            continue

        # Determine the input parameter
        if tool_name == "search_companies_house":
            kwargs = {"query": entity_name}
        elif tool_name == "search_delaware":
            kwargs = {"entity_name": entity_name}
        elif tool_name == "search_sec_company":
            kwargs = {"query": entity_name}
        elif tool_name == "search_northdata":
            kwargs = {"entity_name": entity_name}
        else:
            continue

        try:
            result = await TOOL_DISPATCH[tool_name](**kwargs)
            verification_results.append({
                "tool": tool_name,
                "query": entity_name,
                "output": result,
            })

            if _name_matches(entity_name, result):
                verified = True
                flags.append(
                    f"VERIFIED: '{entity_name}' confirmed by {tool_name}."
                )
                break
            else:
                flags.append(
                    f"NOT FOUND: '{entity_name}' not found by {tool_name}."
                )
        except Exception as e:
            flags.append(f"ERROR: {tool_name} failed: {str(e)}")

    # Phase 3: Apply results
    if verified:
        # Entity is real — keep the report as-is
        report["_validation"] = {
            "original_confidence": confidence,
            "downgraded": False,
            "auto_verified": True,
            "verified_by": [r["tool"] for r in verification_results if _name_matches(entity_name, r["output"])],
        }
    else:
        # Entity not found in any registry — hallucination
        original_confidence = confidence
        report["confidence"] = "insufficient"
        report["recommended_entity"] = None
        report["_validation"] = {
            "original_confidence": original_confidence,
            "downgraded": True,
            "auto_verified": False,
            "reason": f"Entity '{entity_name}' not confirmed by any registry tool after auto-verification",
            "tools_tried": [r["tool"] for r in verification_results],
        }
        flags.append(
            f"REJECTED: '{entity_name}' not found in any registry. "
            f"Confidence set to insufficient, entity removed."
        )

        # Also invalidate reverse evidence
        for rev in report.get("evidence_reverse", []):
            if rev.get("strength") in ("strong", "moderate"):
                rev["strength"] = "none"
                rev["_validation_note"] = "Invalidated: entity not verified"

    # Append validation summary to the note
    if flags:
        validation_summary = " | ".join(flags)
        note = report.get("note", "")
        report["note"] = note + f" [VALIDATION: {validation_summary}]"

    return report, flags
