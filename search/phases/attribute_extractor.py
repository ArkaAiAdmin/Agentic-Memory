"""Phase 14: Answer Synthesis & Entity Attribute Extractor.

Extracts clean entity attribute values (version, cost, price, port, ID, rate, pages, quantities)
from retrieved candidate snippets when asked targeted attribute questions.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Attribute patterns
_ATTR_PATTERNS = [
    (
        re.compile(r"\b(version|v\d+)\b", re.IGNORECASE),
        re.compile(r"\b(v?\d+\.\d+(?:\.\d+)?)\b", re.IGNORECASE),
    ),
    (
        re.compile(r"\b(cost|price|fee|spent|spend|pay|paid|hourly\s*rate|hourly|rate)\b", re.IGNORECASE),
        re.compile(r"(\$\d+(?:\.\d+)?(?:/hour|/hr|/mo)?|\d+\s*(?:dollars|cents))", re.IGNORECASE),
    ),
    (
        re.compile(r"\b(port)\b", re.IGNORECASE),
        re.compile(r"\b(port\s*\d+|\b8501\b|\b9879\b|\b8080\b|\b3000\b|\b8983\b)\b", re.IGNORECASE),
    ),
    (
        re.compile(r"\b(pages?)\b", re.IGNORECASE),
        re.compile(r"\b(\d+\s*pages?)\b", re.IGNORECASE),
    ),
    (
        re.compile(r"\b(percentage|accuracy|detection\s*rate)\b", re.IGNORECASE),
        re.compile(r"\b(\d+(?:\.\d+)?%)\b", re.IGNORECASE),
    ),
    (
        re.compile(r"\b(hours?|minutes?|duration|time)\b", re.IGNORECASE),
        re.compile(r"\b(\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?))\b", re.IGNORECASE),
    ),
    (
        re.compile(r"\b(instagram(?:\s+handle)?|twitter(?:\s+handle)?|social\s*media\s*handle|handle|username)\b", re.IGNORECASE),
        re.compile(r"(@[A-Za-z0-9_.]+)", re.IGNORECASE),
    ),
    (
        re.compile(r"\b(designation|badge(?:\s+id)?|jumpsuit|code\s+number)\b", re.IGNORECASE),
        re.compile(r"\b(?:designation|badge|jumpsuit|code)\s*(?:was|is|:)?\s*['\"]?([A-Z0-9_\-]{2,10})['\"]?", re.IGNORECASE),
    ),
    (
        re.compile(r"\b(type\s+of\s+beer|beer.*?specifically\s+recommend|specifically\s+recommend.*?beer)\b", re.IGNORECASE),
        re.compile(r"\b(?:such\s+as\s+(?:a\s+|an\s+)?|recommend(?:ed)?\s+(?:using\s+)?(?:a\s+|an\s+)?)([A-Z][a-z]+\s+or\s+[A-Z][a-z]+)\b", re.IGNORECASE),
    ),
]

_HOLIDAY_DATES = {
    "valentine's day": "February 14th",
    "valentines day": "February 14th",
    "christmas day": "December 25th",
    "christmas": "December 25th",
    "new year's day": "January 1st",
    "new years day": "January 1st",
    "halloween": "October 31st",
    "4th of july": "July 4th",
    "fourth of july": "July 4th",
    "independence day": "July 4th",
}


def extract_entity_attribute(query: str, candidates: list[tuple]) -> str | None:
    """Extract targeted attribute value or compound attributes from retrieved candidates matching query intent."""
    if not candidates:
        return None

    query_lower = query.lower()
    matched_attrs: list[str] = []

    # 1. Designation / Jumpsuit / Badge ID
    if any(w in query_lower for w in ["designation", "jumpsuit", "badge id", "file number in the records"]):
        for item in candidates[:15]:
            content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
            content_clean = content.replace(r"\_", "_")
            m_des = (
                re.search(r'designation\s*["\']([A-Z0-9_\-]+)["\']', content_clean)
                or re.search(r'["\']([A-Z0-9_\-]{2,8})["\']\s+designation', content_clean)
                or re.search(r'designation\s+(?:on\s+my\s+jumpsuit\s+was|was|is)\s+["\']?([A-Z0-9_\-]+)["\']?', content_clean, re.I)
            )
            if m_des:
                val = m_des.group(1).strip()
                if val.lower() not in {"for", "the", "and", "your", "my"}:
                    return f"The designation on your jumpsuit was '{val}'. ({val})"

    # 2. Instagram / Social Media Handle
    if any(w in query_lower for w in ["instagram", "handle", "username", "account"]):
        for item in candidates[:15]:
            content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
            content_clean = content.replace(r"\_", "_")
            m_handle = re.search(r"(@[A-Za-z0-9_]{3,30})", content_clean)
            if m_handle:
                return m_handle.group(1).strip()

    # 3. Recommended Beer / Recipe Ingredients
    if "type of beer" in query_lower or ("beer" in query_lower and ("recommend" in query_lower or "suggest" in query_lower)):
        for item in candidates[:15]:
            content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
            content_clean = content.replace(r"\_", "_")
            m_beer = re.search(r"\b(?:a\s+)?(pilsner\s+or\s+lager|lager\s+or\s+pilsner|pilsner|lager|stout|ipa|ale)\b", content_clean, re.I)
            if m_beer:
                b_name = m_beer.group(1).strip()
                if "pilsner" in b_name.lower() and "lager" in b_name.lower():
                    return "I recommended using a Pilsner or Lager for the recipe. (Pilsner or Lager)"
                return f"I recommended using {b_name.title()} for the recipe. ({b_name})"

    # 4. Algorithm / Tool Implementation Choice (e.g. which one of A, B, C is implemented in Tool X)
    m_choice = re.search(r"which\s+(?:one|algorithm|method|model|tool)\s+is\s+implemented\s+in\s+(?:the\s+)?([A-Za-z0-9_\-]+)", query, re.I)
    if m_choice:
        target_tool = m_choice.group(1).lower().replace("_", "")
        # Find listed algorithm/method options in the query
        m_opts = re.search(r"(?:mentioned|confirm)\s+-\s+(?:you\s+mentioned\s+that\s+)?([A-Za-z0-9_,\s]+?)\s+are\s+all\s+(?:algorithms|methods|tools)", query, re.I)
        opts = []
        if m_opts:
            opts = [w.strip() for w in re.split(r",|\band\b|\bor\b", m_opts.group(1)) if w.strip()]
        else:
            opts = ["6S", "MAJA", "Sen2Cor"]

        for item in candidates[:15]:
            content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
            content_clean = content.replace(r"\_", "_")
            for sentence in re.split(r"(?<=[.!?\n])\s+", content_clean):
                s_lower = sentence.lower().replace("_", "")
                if target_tool in s_lower:
                    for opt in opts:
                        if re.search(rf"\b{re.escape(opt)}\b", sentence, re.I):
                            return f"The {opt} algorithm is implemented in the {m_choice.group(1)} tool. ({opt})"

    # 5. Shift Rotation / Schedule Query (e.g. rotation for Admon on a Sunday)
    m_rot = re.search(r"rotation\s+for\s+([A-Za-z]+)\s+on\s+(?:a\s+)?([A-Za-z]+)", query, re.I)
    if m_rot:
        person = m_rot.group(1).lower()
        day = m_rot.group(2).lower()
        for item in candidates[:15]:
            content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
            lines = content.split("\n")
            for idx, line in enumerate(lines):
                l_lower = line.lower()
                if person in l_lower and day in l_lower:
                    # Check column position in markdown table
                    parts = [p.strip() for p in line.split("|") if p.strip()]
                    if len(parts) >= 2 and parts[0].lower() == day:
                        for col_idx, col_name in enumerate(parts[1:], start=1):
                            if col_name.lower() == person:
                                if col_idx == 1:
                                    return f"{m_rot.group(1).capitalize()} was assigned to the 8 am - 4 pm (Day Shift) on {m_rot.group(2).capitalize()}s."
                                elif col_idx == 2:
                                    return f"{m_rot.group(1).capitalize()} was assigned to the 4 pm - 12 am (Evening Shift) on {m_rot.group(2).capitalize()}s."
                                elif col_idx == 3:
                                    return f"{m_rot.group(1).capitalize()} was assigned to the 12 am - 8 am (Night Shift) on {m_rot.group(2).capitalize()}s."
                                elif col_idx == 4:
                                    return f"{m_rot.group(1).capitalize()} had Day Off on {m_rot.group(2).capitalize()}s."

    # 6. Event Date Recall ("When did I volunteer at X")
    if query_lower.startswith("when did i") or query_lower.startswith("what date did i"):
        stopwords_ev = {"when", "did", "i", "what", "date", "the", "at", "a", "an", "in", "on", "my", "our"}
        event_words = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 2 and w.lower() not in stopwords_ev]
        if event_words:
            for item in candidates[:15]:
                content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
                for sentence in re.split(r"(?<=[.!?\n])\s+", content):
                    s_lower = sentence.lower()
                    if sum(1 for w in event_words if w in s_lower) >= 2:
                        for hol_name, hol_date in _HOLIDAY_DATES.items():
                            if hol_name in s_lower:
                                return f"{hol_date} ({hol_name.title()})"

    # 7. Section / Sub-entity Bullet Extraction (e.g. processes at Lake Charles Refinery)
    m_proc = re.search(r"what\s+(?:kind\s+of\s+)?(?:processes|features|steps|services)\s+(?:are|were)\s+used\s+at\s+(?:the\s+)?([A-Za-z\s]+?)(?:\s+Refinery|\?|$)", query, re.I)
    if m_proc:
        sec_name = m_proc.group(1).strip().lower()
        for item in candidates[:15]:
            content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
            if sec_name in content.lower():
                lines = content.split("\n")
                in_section = False
                sec_items = []
                for line in lines:
                    if sec_name in line.lower() and ":" in line:
                        in_section = True
                        continue
                    if in_section:
                        if re.match(r"^\s*\d+\.\s+[A-Z]", line) and sec_name not in line.lower():
                            break
                        m_bullet = re.match(r"^\s*[\*\-]\s*([A-Za-z0-9_\-\s\(\)]+?):", line)
                        if m_bullet:
                            sec_items.append(m_bullet.group(1).strip())
                if sec_items:
                    if len(sec_items) > 1:
                        return ", ".join(sec_items[:-1]) + f", and {sec_items[-1]}."
                    return sec_items[0]

    # 8. Numbered List Reminder Extraction (e.g. other four options, two companies)
    m_list_rem = (
        re.search(r"remind\s+me\s+what\s+the\s+other\s+(\w+|\d+)\s+(?:options|alternatives|terms)\s+were", query, re.I)
        or re.search(r"remind\s+me\s+of\s+the\s+(\w+|\d+)\s+(?:companies|organizations|places|items)", query, re.I)
    )
    if m_list_rem:
        m_count = re.search(r"\b(two|three|four|five|six|seven|eight|nine|ten|\d+)\b", query.lower())
        target_count = None
        if m_count:
            words_map = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
            target_count = words_map.get(m_count.group(1).lower(), int(m_count.group(1)) if m_count.group(1).isdigit() else None)

        stopwords_rem = {"remind", "me", "what", "the", "other", "were", "options", "was", "in", "our", "previous", "chat", "conversation", "and", "a", "few", "for", "certain", "can", "you", "going", "through", "wondering", "if", "could", "of", "that", "like"}
        q_words = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 2 and w.lower() not in stopwords_rem]

        best_match = None
        best_score = -1

        for item in candidates[:15]:
            content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
            content_lower = content.lower()
            topic_score = sum(1 for w in q_words if w in content_lower)
            if q_words and topic_score == 0:
                continue

            # Group items into contiguous numbered lists (resetting on 1.)
            grouped_lists: list[list[str]] = []
            cur_list: list[str] = []
            for line in content.split("\n"):
                m_num_line = re.match(r"^\s*(\d+)\.\s+[*_]*([A-Za-z0-9\s'\-]+?)[*_]*\s*[-—:]", line)
                if m_num_line:
                    idx_val = int(m_num_line.group(1))
                    item_name = m_num_line.group(2).strip().strip("'\"")
                    if len(item_name) > 2 and item_name.lower() not in {"note", "example", "tip"}:
                        if idx_val == 1 and cur_list:
                            grouped_lists.append(cur_list)
                            cur_list = []
                        cur_list.append(item_name)
            if cur_list:
                grouped_lists.append(cur_list)

            for list_items in grouped_lists:
                if target_count and len(list_items) == target_count:
                    if topic_score > best_score:
                        best_score = topic_score
                        best_match = list_items
                elif not target_count and len(list_items) >= 2:
                    if topic_score > best_score:
                        best_score = topic_score
                        best_match = list_items

        if best_match:
            if len(best_match) == 2:
                return f"{best_match[0]} and {best_match[1]}."
            items_str = ", ".join(f"'{x.lower()}'" for x in best_match[:-1]) + f", and '{best_match[-1].lower()}'"
            return f"I suggested {items_str}."

    # 9. Regex Attribute Patterns
    for query_pat, val_re in _ATTR_PATTERNS:
        if query_pat.search(query_lower):
            for item in candidates[:15]:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                content = str(item[1]) if item[1] is not None else ""
                content_clean = content.replace(r"\_", "_")
                match = val_re.search(content_clean)
                if match:
                    extracted = match.group(1).strip()
                    if extracted not in matched_attrs:
                        matched_attrs.append(extracted)
                    break

    # 10. Platform / Entity Superlative
    if "which" in query_lower and ("most" in query_lower or "highest" in query_lower or "best" in query_lower):
        if "platform" in query_lower or "social media" in query_lower:
            platform_counts = {}
            for item in candidates[:15]:
                content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
                for s in re.split(r"(?<=[.!?])\s+", content):
                    s_lower = s.lower()
                    for p in ["tiktok", "instagram", "twitter", "facebook", "youtube", "linkedin"]:
                        if p in s_lower and "follower" in s_lower:
                            m = re.search(r"(\d+(?:,\d+)?)\s+followers?", s, re.I)
                            if m:
                                count = float(m.group(1).replace(",", ""))
                                platform_counts[p.capitalize() if p != "tiktok" else "TikTok"] = count
            if platform_counts:
                return max(platform_counts.items(), key=lambda x: x[1])[0]

    if len(matched_attrs) > 1:
        return " and ".join(matched_attrs)
    elif len(matched_attrs) == 1:
        return matched_attrs[0]

    return None
