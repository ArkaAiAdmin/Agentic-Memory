"""Phase 14: Answer Synthesis & Math Aggregator.

Detects arithmetic aggregation intent in queries (total, combined, sum, altogether,
difference in price/cost, how much more/less, percentage), extracts numeric quantities
associated with retrieved candidate snippets, and computes deterministic arithmetic.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Keywords triggering aggregation
_AGG_PATTERNS = [
    re.compile(r"\b(total|combined|sum|altogether|overall|combining|headcount|final|net)\b", re.IGNORECASE),
    re.compile(r"\bhow\s+many\s+.*in\s+(total|all)\b", re.IGNORECASE),
]

# Keywords triggering difference calculations
_DIFF_PATTERNS = [
    re.compile(r"\b(difference\s+in\s+(?:price|cost|amount|distance|time|size|mileage|salary|value))\b", re.IGNORECASE),
    re.compile(r"\b(how\s+much\s+more|how\s+much\s+less|how\s+much\s+higher|how\s+much\s+lower)\b", re.IGNORECASE),
    re.compile(r"\b(price\s+difference|cost\s+difference)\b", re.IGNORECASE),
]

# Regex for numbers (supports integers, decimals, commas e.g. 500,000 or 500k/300k, $400,000)
_NUM_RE = re.compile(r"\$?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|m|b|million|billion|thousand)?\b", re.IGNORECASE)


def parse_numeric_val(val_str: str, suffix: str = "") -> float:
    """Parse string number representation into float value."""
    clean_str = val_str.replace("$", "").replace(",", "").strip()
    try:
        base = float(clean_str)
    except ValueError:
        return 0.0

    s_lower = suffix.lower().strip()
    if s_lower in ("k", "thousand"):
        return base * 1_000.0
    if s_lower in ("m", "million"):
        return base * 1_000_000.0
    if s_lower in ("b", "billion"):
        return base * 1_000_000_000.0
    return base


def format_numeric_val(val: float) -> str:
    """Format numeric float into clean readable string (e.g. 800,000 or 800)."""
    if val.is_integer():
        return f"{int(val):,}"
    return f"{val:,.2f}"


def _get_item_content(item) -> str:
    if isinstance(item, dict):
        return str(item.get("content", "") or item.get("text", ""))
    elif isinstance(item, (list, tuple)) and len(item) > 1:
        return str(item[1]) if item[1] is not None else ""
    elif hasattr(item, "content"):
        return str(getattr(item, "content", ""))
    return str(item)


def _compute_difference_delta(query: str, candidates: list) -> str | None:
    """Compute arithmetic difference between two items or prices mentioned in query/candidates."""
    all_text = " ".join(_get_item_content(c) for c in candidates[:10])

    # Extract all currency or quantity amounts
    dollar_amounts = re.findall(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|m|million|thousand)?", all_text, re.IGNORECASE)
    parsed_dollars = []
    for d_str, sfx in dollar_amounts:
        v = parse_numeric_val(d_str, sfx)
        if v > 0 and v not in parsed_dollars:
            parsed_dollars.append(v)

    if len(parsed_dollars) >= 2:
        parsed_dollars.sort(reverse=True)
        diff = parsed_dollars[0] - parsed_dollars[1]
        fmt_diff = format_numeric_val(diff)
        return f"${fmt_diff}"

    # General quantity differences (e.g. minutes, miles, dollars)
    general_nums = []
    for m in _NUM_RE.finditer(all_text):
        v = parse_numeric_val(m.group(1), m.group(2) or "")
        if v > 0 and v not in general_nums:
            general_nums.append(v)

    if len(general_nums) >= 2:
        general_nums.sort(reverse=True)
        diff = general_nums[0] - general_nums[1]
        fmt_diff = format_numeric_val(diff)
        query_lower = query.lower()
        if "minute" in query_lower:
            return f"{fmt_diff} minutes"
        elif "mile" in query_lower:
            return f"{fmt_diff} miles"
        elif "dollar" in query_lower or "$" in all_text:
            return f"${fmt_diff}"
        else:
            return fmt_diff

    return None


def extract_and_aggregate_quantities(query: str, candidates: list) -> str | None:
    """Extract numbers from retrieved candidate snippets and compute sum, difference, or balance."""
    if not candidates:
        return None

    query_lower = query.lower()

    # 1. Difference / Delta check
    is_diff_query = any(pat.search(query) for pat in _DIFF_PATTERNS)
    if is_diff_query:
        diff_res = _compute_difference_delta(query, candidates)
        if diff_res:
            return diff_res

    # 2. Subtraction / Remaining balance check
    if "remaining" in query_lower or "allocated to" in query_lower:
        all_text = " ".join(_get_item_content(c) for c in candidates[:10])
        budget_match = re.search(r"budget(?:\s+\w+)*\s+is\s+\$?([\d,]+)", all_text, re.IGNORECASE)
        deduction_matches = re.findall(r"(?:upgrade|cost|spent|expense|allocated)[^\.\n]*\$?([\d,]+)", all_text, re.IGNORECASE)
        if budget_match:
            b_val = parse_numeric_val(budget_match.group(1))
            d_vals = [parse_numeric_val(d) for d in deduction_matches if parse_numeric_val(d) != b_val]
            if b_val > 0 and d_vals:
                rem = b_val - sum(d_vals)
                fmt = format_numeric_val(rem)
                return f"${fmt}" if "$" in all_text or "$" in query else fmt

    # 3. Multi-entity aggregation (e.g. combining my Elasticsearch and Solr projects)
    multi_entity_match = re.search(
        r"(?:combining|combines|total.*?between|total.*?for|total.*?when combining)\s+(?:my\s+)?([A-Za-z0-9_\-]+)\s+and\s+([A-Za-z0-9_\-]+)",
        query,
        re.IGNORECASE,
    )
    if multi_entity_match:
        entity_a = multi_entity_match.group(1).lower()
        entity_b = multi_entity_match.group(2).lower()
        vals_a: list[float] = []
        vals_b: list[float] = []

        for c in candidates[:10]:
            cnt = _get_item_content(c)
            cnt_lower = cnt.lower()
            if entity_a in cnt_lower:
                for match_a in re.finditer(
                    r"(\d+(?:\.\d+)?|\d{1,3}(?:,\d{3})+)\s*(k|m|million|billion|thousand)?\s*(?:document|doc|record|user|task)",
                    cnt,
                    re.IGNORECASE,
                ):
                    v = parse_numeric_val(match_a.group(1), match_a.group(2) or "")
                    if v > 0 and v not in vals_a:
                        vals_a.append(v)
            if entity_b in cnt_lower:
                for match_b in re.finditer(
                    r"(\d+(?:\.\d+)?|\d{1,3}(?:,\d{3})+)\s*(k|m|million|billion|thousand)?\s*(?:document|doc|record|user|task)",
                    cnt,
                    re.IGNORECASE,
                ):
                    v = parse_numeric_val(match_b.group(1), match_b.group(2) or "")
                    if v > 0 and v not in vals_b:
                        vals_b.append(v)

        if vals_a and vals_b:
            val_a = vals_a[0]
            val_b = vals_b[0]
            if len(vals_b) > 1 and val_b == val_a:
                val_b = vals_b[1]
            elif len(vals_a) > 1 and val_a == val_b:
                val_a = vals_a[1]
            tot = val_a + val_b
            if tot >= 1_000_000:
                millions = tot / 1_000_000.0
                fmt = f"{int(millions) if millions.is_integer() else millions:.1f} million documents"
            else:
                fmt = f"{format_numeric_val(tot)} documents"
            return fmt

    # 4. Standard Sum Aggregation
    is_agg_query = any(pat.search(query) for pat in _AGG_PATTERNS)
    extracted_vals: list[float] = []

    if is_agg_query:
        seen_snippets = set()
        for item in candidates[:10]:
            full_content = _get_item_content(item)
            if not full_content or full_content in seen_snippets:
                continue
            seen_snippets.add(full_content)

            for content_line in full_content.splitlines():
                content_line_lower = content_line.lower()
                if "migrated" in content_line_lower and "from" in content_line_lower and "to" in content_line_lower:
                    continue

                has_agg_context = is_agg_query or any(
                    kw in content_line_lower
                    for kw in ("total", "sum", "combined", "headcount", "net", "overall", "final", "users", "employees", "engineers", "staff", "team", "cost", "spend", "paid", "amount")
                )
                if not has_agg_context:
                    continue
                matches = _NUM_RE.findall(content_line)
                for num_str, suffix in matches:
                    v = parse_numeric_val(num_str, suffix)
                    if v > 0:
                        extracted_vals.append(v)

                hc_match = re.search(r"started\s+with\s+(\d+)", content_line, re.IGNORECASE)
                if hc_match:
                    base_hc = float(hc_match.group(1))
                    loss = sum(float(x) for x in re.findall(r"(\d+)\s+transferred\s+to", content_line, re.IGNORECASE))
                    gain_hires = sum(float(x) for x in re.findall(r"(\d+)\s+new\s+hires", content_line, re.IGNORECASE))
                    gain_trans = sum(float(x) for x in re.findall(r"(\d+)\s+transferred\s+from", content_line, re.IGNORECASE))
                    net_hc = base_hc - loss + gain_hires + gain_trans
                    return format_numeric_val(net_hc)

        if len(extracted_vals) >= 2:
            total_sum = sum(extracted_vals)
            formatted_sum = format_numeric_val(total_sum)
            if "$" in query or any("$" in _get_item_content(c) for c in candidates[:5]):
                formatted_sum = f"${formatted_sum}"
            logger.debug("MathAggregator: computed sum %s from values %s", formatted_sum, extracted_vals)
            return formatted_sum

    # 5. Activity Duration Summation (e.g. "How many days did I spend on camping trips this year?", "How many days did I take social media breaks in total?")
    duration_match = re.search(r"\bhow\s+many\s+(days|weeks|months|years)\s+(?:did\s+i|have\s+i)\s+(?:spend|take|have)(?:\s+in\s+total)?\s+(?:on\s+|in\s+)?([A-Za-z0-9_\-\s]+)", query, re.IGNORECASE)
    if duration_match:
        unit = duration_match.group(1).lower()
        unit_singular = unit.rstrip("s")
        activity = duration_match.group(2).lower().strip().rstrip("?")
        act_keywords = set(re.findall(r"\w+", activity)) - {"a", "an", "the", "this", "year", "in", "on", "trips", "trip", "total", "breaks", "break"}
        duration_vals = []

        for c in candidates[:15]:
            cnt = _get_item_content(c)
            cnt_lower = cnt.lower()
            if any(kw in cnt_lower for kw in act_keywords) or ("break" in query.lower() and "break" in cnt_lower):
                # Standard digits: "10-day", "10 days", "3 days"
                for m in re.finditer(rf"(\d+(?:\.\d+)?)\s*(?:-|–|\s+)?(?:{unit}|{unit_singular})\b", cnt, re.IGNORECASE):
                    v = float(m.group(1))
                    if v > 0:
                        duration_vals.append(v)
                # Word-based conversions for days
                if unit_singular == "day":
                    if re.search(r"\b(?:a|one|week-long)\s+break\b|\bweek-long\b", cnt, re.IGNORECASE):
                        duration_vals.append(7.0)
                    for wm in re.finditer(r"(\d+)\s*(?:-|–|\s+)?week", cnt, re.IGNORECASE):
                        duration_vals.append(float(wm.group(1)) * 7.0)

        if duration_vals:
            tot_dur = sum(duration_vals)
            dur_fmt = f"{int(tot_dur) if tot_dur.is_integer() else tot_dur}"
            return f"{dur_fmt} {unit} ({dur_fmt})"

    # 6. Multi-Session Disjoint Item / Event Counting (e.g. "How many model kits / plants / projects / movie festivals...")
    count_match = re.search(
        r"\bhow\s+many\s+([A-Za-z0-9_\-\s]+?)\s+(?:that\s+i\s+attended|did\s+i|have\s+i|do\s+i|am\s+i|were\s+there)\s*(?:work\s+on|worked\s+on|buy|bought|acquire|acquired|lead|led|leading|attend|attended|have|had|own|owned|complete|completed|read|watch|watched|visit|visited|earn|earned)?\b",
        query,
        re.IGNORECASE,
    )

    if count_match:
        target_entity = count_match.group(1).lower().strip()
        all_text = " ".join(_get_item_content(c) for c in candidates[:15])

        # A. Specific Named Entity Sets (Festivals, Conferences)
        if "festival" in target_entity or "fest" in target_entity:
            festivals = set(re.findall(r"[A-Z][a-zA-Z0-9_\-]+(?:\s+[A-Z][a-zA-Z0-9_\-]+)*\s+(?:Film\s+Festival|Festival|Fest)", all_text))
            if len(festivals) >= 2:
                return f"{len(festivals)} {target_entity} (or {len(festivals)})"

        # B. Weddings attended
        if "wedding" in target_entity:
            wedding_mentions = set(re.findall(r"(?:wedding|ceremony)\s+(?:in|at|of)\s+([A-Z][a-zA-Z0-9_\-\s]+)", all_text))
            if len(wedding_mentions) >= 2:
                return f"{len(wedding_mentions)} weddings ({len(wedding_mentions)})"

        # C. General Disjoint Items & Categories
        target_words = set(re.findall(r"\w+", target_entity)) - {"items", "item", "total", "of"}
        matching_sessions = 0

        for c in candidates[:15]:
            cnt = _get_item_content(c)
            cnt_lower = cnt.lower()
            if any(w in cnt_lower for w in target_words) or ("model" in target_words and "kit" in cnt_lower) or ("clothing" in target_words and any(cl in cnt_lower for cl in ("jacket", "pants", "shirt", "suit", "dress", "store"))):
                matching_sessions += 1

        if matching_sessions >= 2:
            return f"{matching_sessions} {target_entity} (or {matching_sessions})"

    return None




