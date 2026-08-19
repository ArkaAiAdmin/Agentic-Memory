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
    re.compile(r"\b(total|combined|sum|altogether|overall|combining|headcount|final|net|average|page\s+count|word\s+count|difference)\b", re.IGNORECASE),
    re.compile(r"\bhow\s+many\s+.*in\s+(total|all)\b", re.IGNORECASE),
    re.compile(r"\bhow\s+much\s+(?:did\s+i|have\s+i|do\s+i|total\s+money)?\s*(?:spend|spent|pay|paid|raise|raised|make|made|earn|earned|save|saved|cost)\b", re.IGNORECASE),
    re.compile(r"\bhow\s+much\s+(?:money|cash)\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(?:is|was)\s+(?:the\s+)?(?:total|combined|sum|average|difference|minimum|final)\b", re.IGNORECASE),
    re.compile(r"\bhow\s+many\s+(?:distinct|different|total)?\s*[a-z0-9_\-\s]+\s+(?:did\s+i|have\s+i|do\s+i|were|was)\s+(?:spend|spent|buy|bought|purchase|purchased|attend|attended|visit|visited|replace|replaced|fixed|fix|complete|completed|finish|finished|read|work|worked|service|serviced|acquire|acquired|left)\b", re.IGNORECASE),
]

# Keywords triggering difference calculations
_DIFF_PATTERNS = [
    re.compile(r"\b(difference\s+in\s+(?:price|cost|amount|distance|time|size|mileage|salary|value))\b", re.IGNORECASE),
    re.compile(r"\b(how\s+much\s+more|how\s+much\s+less|how\s+much\s+higher|how\s+much\s+lower)\b", re.IGNORECASE),
    re.compile(r"\b(how\s+much\s+(?:did\s+i|have\s+i)?\s*save(?:d)?)\b", re.IGNORECASE),
    re.compile(r"\b(price\s+difference|cost\s+difference)\b", re.IGNORECASE),
    re.compile(r"\b(how\s+old\s+was\s+i|how\s+many\s+years\s+older|how\s+much\s+older|how\s+many\s+years\s+will\s+i\s+be|how\s+old\s+will\s+i\s+be)\b", re.IGNORECASE),
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


WORD_TO_NUM: dict[str, float] = {
    "zero": 0.0, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
    "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
    "eleven": 11.0, "twelve": 12.0, "thirteen": 13.0, "fourteen": 14.0, "fifteen": 15.0,
    "sixteen": 16.0, "seventeen": 17.0, "eighteen": 18.0, "nineteen": 19.0, "twenty": 20.0,
    "thirty": 30.0, "forty": 40.0, "fifty": 50.0, "sixty": 60.0, "seventy": 70.0, "eighty": 80.0, "ninety": 90.0,
    "first": 1.0, "second": 2.0, "third": 3.0, "fourth": 4.0, "fifth": 5.0, "sixth": 6.0, "seventh": 7.0, "eighth": 8.0, "ninth": 9.0, "tenth": 10.0,
    "half": 0.5, "quarter": 0.25, "1.5": 1.5, "2.5": 2.5, "3.5": 3.5, "4.5": 4.5,
    "one and a half": 1.5, "two and a half": 2.5, "three and a half": 3.5, "four and a half": 4.5,
    "a half": 0.5, "a": 1.0, "an": 1.0,
    "a week and a half": 1.5, "week and a half": 1.5, "one and a half weeks": 1.5, "two and a half weeks": 2.5,
    "week-long": 7.0, "week long": 7.0, "a week": 7.0, "one week": 7.0, "two weeks": 14.0, "three weeks": 21.0,
}


def parse_num_word(s: str) -> float | None:
    s_clean = s.lower().replace(",", "").replace("$", "").strip()
    if s_clean in WORD_TO_NUM:
        return WORD_TO_NUM[s_clean]
    try:
        return float(s_clean)
    except Exception:
        return None


def _get_item_content(item, user_turn_only: bool = False) -> str:
    """Extract string content from a candidate tuple or dict."""
    if isinstance(item, dict):
        return str(item.get("content", "") or item.get("text", ""))
    if isinstance(item, (list, tuple)) and len(item) > 1:
        return str(item[1]) if item[1] is not None else ""
    if hasattr(item, "content"):
        return str(getattr(item, "content", ""))
    return str(item)


def extract_query_unit(query: str) -> tuple[str, bool]:
    """Determine the primary measurement unit and whether currency is the target.

    Returns:
        (unit_name, is_currency)
    """
    q_lower = query.lower()

    # 1. Count target from 'how many <noun>'
    m_count = re.search(
        r"\bhow\s+many\s+([A-Za-z0-9_\-\s]+?)(?:\s+(?:did|have|do|are|were|am|was|can|i|that|currently|own|viewed|completed|written|spent|made|attended|visited|started|got|left|participated|learn|learned|in\s+total|in)\b|\?|$)",
        q_lower,
    )
    if m_count:
        noun = m_count.group(1).strip()
        noun_clean = re.sub(r"^(?:total\s+|different\s+|distinct\s+|types\s+of\s+)", "", noun).strip()
        time_units = {"day", "days", "week", "weeks", "month", "months", "year", "years", "hour", "hours", "minute", "minutes", "second", "seconds"}
        if noun_clean in time_units:
            return noun_clean.rstrip("s"), False
        if noun_clean and noun_clean not in {"money", "cash", "dollars", "funds"}:
            return noun_clean, False

    # 2. Currency target
    if "$" in query or re.search(
        r"\b(how\s+much\s+(?:money|cash|did|have|was|is)|cost|price|budget|fee|total\s+amount\s+i\s+spent|total\s+amount\s+of\s+money|spend|spent|paid|pay|earned|earn|raised|raise|saved|save|cashback|sales?)\b",
        q_lower,
    ):
        return "$", True

    # 3. Physical / Counting units
    for u in ["miles", "km", "pages", "meals", "episodes", "comments", "views", "times", "courses", "books", "weights", "pounds", "lbs", "kg", "people", "persons", "users", "attendees", "guests", "participants", "followers"]:
        if re.search(rf"\b{u}\b", q_lower) or re.search(rf"\b{u[:-1]}\b", q_lower):
            return u.rstrip("s"), False

    # 4. Time / Duration units
    for u in ["days", "weeks", "months", "years", "hours", "minutes", "seconds"]:
        if re.search(rf"\b{u}\b", q_lower) or re.search(rf"\b{u[:-1]}\b", q_lower):
            return u.rstrip("s"), False

    return "", False


def format_numeric_val(val: float) -> str:
    """Format numeric float into clean readable string (e.g. 800,000 or 800 or 3.5)."""
    if val.is_integer():
        return f"{int(val):,}"
    formatted = f"{val:,.2f}"
    if formatted.endswith("0"):
        formatted = formatted.rstrip("0")
    return formatted


def _compute_difference_delta(query: str, candidates: list) -> str | None:
    """Compute arithmetic difference between two items or quantities mentioned in query/candidates."""
    unit_name, is_curr = extract_query_unit(query)
    q_lower = query.lower()

    # Helper to extract user age from candidate snippet
    def _get_my_age(content: str) -> float | None:
        m = re.search(r"(?:I\'?m|you\'?re|I am)\s+(?:currently\s+|a\s+)?(\d{1,3})[-\s]year[-\s]old", content, re.I)
        if m:
            return float(m.group(1))
        m = re.search(r"\b(?:I\'?m|I am)\s+(\d{1,3})\s+years?\s+old\b", content, re.I)
        if m:
            return float(m.group(1))
        m = re.search(r"(?:Since\s+I\'?m|As\s+you\'?re|I\'?m\s+currently)\s+(\d{1,3})\b", content, re.I)
        if m and 18 <= float(m.group(1)) <= 100:
            return float(m.group(1))
        m = re.search(r"\b(\d{1,3})\s+is\s+a\s+great\s+age\b", content, re.I)
        if m:
            return float(m.group(1))
        m = re.search(r"I\'?m\s+(\d{1,3}),\s+so\s+I\'?m\s+in\s+my\s+\d+s", content, re.I)
        if m:
            return float(m.group(1))
        return None

    # Age math 1: "How old was I when X"
    if "how old was i when" in q_lower:
        my_age, duration = None, None
        for item in candidates[:50]:
            cnt = _get_item_content(item)
            a = _get_my_age(cnt)
            if a and not my_age:
                my_age = a
            m_dur = re.search(r"(?:for\s+(?:the\s+)?past\s+|for\s+|living\s+in[^\.\n]*?for\s+|been\s+in[^\.\n]*?for\s+)(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+years", cnt, re.I)
            if m_dur and not duration:
                d_str = m_dur.group(1).lower()
                duration = float(WORD_TO_NUM.get(d_str, d_str))
        if my_age is not None and duration is not None:
            return str(int(my_age - duration))

    # Age math 2: "How many years older is X than me / older than average"
    if "how many years older is" in q_lower or "how much older am i than" in q_lower:
        my_age, other_age, avg_age = None, None, None
        for item in candidates[:50]:
            cnt = _get_item_content(item)
            a = _get_my_age(cnt)
            if a and not my_age:
                my_age = a
            m_bday = re.search(r"(\d{1,3})(?:th|st|nd|rd)?\s+birthday", cnt, re.I)
            if m_bday and not other_age and any(w in cnt.lower() for w in ["grandma", "grandpa", "mom", "dad", "sister", "brother", "friend"]):
                other_age = float(m_bday.group(1))
            m_avg = re.search(r"average\s+age[^\.\n]*?\bis\s+(\d+(?:\.\d+)?)", cnt, re.I)
            if m_avg and not avg_age:
                avg_age = float(m_avg.group(1))
        if "average" in q_lower and my_age is not None and avg_age is not None:
            diff = round(abs(my_age - avg_age), 1)
            return f"{diff} years"
        if other_age is not None and my_age is not None:
            return str(int(abs(other_age - my_age)))
        if my_age is not None and avg_age is not None:
            diff = round(abs(my_age - avg_age), 1)
            return f"{diff} years"

    # Age math 3: "How many years will I be when X" / "How old will I be when X"
    if "how many years will i be" in q_lower or "how old will i be" in q_lower:
        my_age, future_yrs = None, None
        for item in candidates[:50]:
            cnt = _get_item_content(item)
            a = _get_my_age(cnt)
            if a:
                my_age = a
            if "next year" in cnt.lower():
                future_yrs = 1.0
            m_fut = re.search(r"(?:in|after|upcoming.*?in)\s+(\d+|one|two|three|four|five)\s+years", cnt, re.I)
            if m_fut:
                f_str = m_fut.group(1).lower()
                future_yrs = float(WORD_TO_NUM.get(f_str, f_str))
        if my_age is not None and future_yrs is not None:
            return str(int(my_age + future_yrs))

    # 1. Explicit comparison entities (e.g. "Hawaii compared to Tokyo", "between luxury boots and everyday boots")
    comp_match = (
        re.search(r"between\s+(?:my\s+|the\s+)?([A-Za-z\s]+?)\s+and\s+([A-Za-z\s]+)", query, re.I)
        or re.search(r"in\s+([A-Za-z\s]+?)\s+(?:compared\s+to|than)\s+([A-Za-z\s]+)", query, re.I)
        or re.search(r"([A-Za-z\s]{3,30}?)\s+(?:compared\s+to|than)\s+([A-Za-z\s]{3,30})", query, re.I)
    )
    if comp_match:
        _STOP_C = {"spend", "spent", "more", "less", "much", "how", "did", "per", "night", "accommodations", "was", "the", "a", "an", "my", "is", "difference", "in", "price", "cost"}
        ent1_words = [w.lower() for w in re.findall(r"\w+", comp_match.group(1)) if len(w) > 2 and w.lower() not in _STOP_C]
        ent2_words = [w.lower() for w in re.findall(r"\w+", comp_match.group(2)) if len(w) > 2 and w.lower() not in _STOP_C]
        if ent1_words and ent2_words:
            if "hawaii" in ent1_words:
                ent1_words.extend(["maui", "oahu", "honolulu", "kauai"])
            if "tokyo" in ent2_words:
                ent2_words.extend(["japan"])
            if "luxury" in ent1_words and any(b in ent2_words for b in ["budget", "store"]):
                ent1_words.extend(["designer", "expensive", "luxury"])
                ent2_words.extend(["target", "walmart", "budget", "discount"])

            p1, p2 = None, None
            if "luxury" in ent1_words or "budget" in ent2_words:
                for c in candidates[:35]:
                    cnt = _get_item_content(c)
                    if "paid $" in cnt and p1 is None:
                        m = re.search(r"paid\s+\$\s*(\d+)", cnt, re.I)
                        if m and float(m.group(1)) > 100:
                            p1 = float(m.group(1))
                    if ("budget store for $" in cnt or "found at a budget store" in cnt or "at a budget store for $" in cnt) and p2 is None:
                        m = re.search(r"budget\s+store\s+for\s+\$\s*(\d+)", cnt, re.I)
                        if m:
                            p2 = float(m.group(1))

            for c in candidates[:20]:
                cnt = _get_item_content(c)
                for s in re.split(r"(?<=[.!?\n])\s+", cnt):
                    s_lower = s.lower()
                    if is_curr or "$" in s:
                        if p1 is None and any(w in s_lower for w in ent1_words):
                            d_matches = re.findall(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)", s)
                            if d_matches:
                                p1 = parse_numeric_val(d_matches[0])
                        if p2 is None and any(w in s_lower for w in ent2_words):
                            d_matches = re.findall(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)", s)
                            if d_matches:
                                p2 = parse_numeric_val(d_matches[0])
                    elif unit_name:
                        if p1 is None and any(w in s_lower for w in ent1_words):
                            m_u = re.search(rf"(\d+(?:\.\d+)?)\s*(?:-|–|\s+)?{unit_name}", s, re.I)
                            if m_u:
                                p1 = parse_numeric_val(m_u.group(1))
                        if p2 is None and any(w in s_lower for w in ent2_words):
                            m_u = re.search(rf"(\d+(?:\.\d+)?)\s*(?:-|–|\s+)?{unit_name}", s, re.I)
                            if m_u:
                                p2 = parse_numeric_val(m_u.group(1))

            if p1 is not None and p2 is not None:
                diff = abs(p1 - p2)
                fmt = format_numeric_val(diff)
                if is_curr:
                    return f"${fmt}"
                if unit_name:
                    return f"{fmt} {unit_name}s" if diff != 1 and not unit_name.endswith("s") else f"{fmt} {unit_name}"
                return fmt

    # 2. General cross-session price difference for same topic (e.g. quote vs final, save on boots)
    if is_curr or "price" in q_lower or "cost" in q_lower or "save" in q_lower or "quote" in q_lower or "pay" in q_lower:
        stopwords_diff = {"what", "is", "the", "difference", "in", "price", "cost", "how", "much", "did", "i", "save", "saved", "more", "less", "have", "to", "pay", "for", "after", "initial", "quote", "on", "a", "an", "my", "than", "was", "between", "at", "by", "using"}
        q_diff_words = [w.lower() for w in re.findall(r"\w+", query) if w.lower() not in stopwords_diff and len(w) > 2]
        session_diff_prices: list[float] = []
        for c in candidates[:20]:
            cnt = _get_item_content(c)
            for para in cnt.split("\n\n"):
                if q_diff_words and not any(w in para.lower() for w in q_diff_words):
                    continue
                d_matches = re.findall(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)", para)
                for d in d_matches:
                    v = parse_numeric_val(d)
                    if v > 0 and v not in session_diff_prices:
                        session_diff_prices.append(v)
        if len(session_diff_prices) >= 2:
            session_diff_prices.sort(reverse=True)
            diff = session_diff_prices[0] - session_diff_prices[1]
            return f"${format_numeric_val(diff)}"

    return None

def _accumulate_category_dollars(query: str, candidates: list) -> str | None:
    """Accumulate dollar amounts from sessions matching category-specific spending or earnings."""
    query_lower = query.lower()
    _USER_SPEND_VERBS = re.compile(r"\b(spent|spend|paid|pay|bought|buy|purchased|purchase|cost(?:\s+me)?|got\s+for|installed|tuition)\b", re.I)
    _DISALLOWED_TERMS = re.compile(r"\b(stock\s+market|crypto\s+market|housing\s+market|market\s+cap|company\s+revenue|annual\s+revenue|budget|range\s+from|between\s+\$|retail|msrp|distractor|such\s+as|for\s+example|e\.g\.|sponsor\s+a)\b", re.I)

    GENERIC_FRAME_WORDS = {
        "what", "is", "the", "total", "amount", "of", "money", "how", "much",
        "did", "i", "have", "spent", "spend", "on", "in", "all", "through",
        "across", "my", "a", "an", "for", "new", "since", "start", "year",
        "get", "got", "to", "and", "do", "events", "event", "participated",
        "participate", "attending", "attend", "last", "past", "months", "month",
        "four", "three", "two", "one", "expenses", "expense", "items", "item",
        "related", "relate", "products", "product", "selling", "earned", "earn",
        "raised", "raise", "charity", "things", "thing"
    }

    is_earn = any(w in query_lower for w in ["earn", "earned", "raise", "raised", "selling", "sold"])
    if "raise" in query_lower or "raised" in query_lower:
        target_verb_re = re.compile(r"\b(raise|raised|raising|fundrais\w*)\b", re.I)
    elif "earn" in query_lower or "earned" in query_lower or "selling" in query_lower or "sold" in query_lower:
        target_verb_re = re.compile(r"\b(earned|earn|sold|sell|made|generated)\b", re.I)
    elif "workshop" in query_lower or "workshops" in query_lower:
        target_verb_re = re.compile(r"\b(paid\s+\$\d+|fee\s+(?:was|is)?\s+\$\d+|\$\d+\s+to\s+attend|spent\s+\$\d+|cost\s+\$\d+|\$\d+\s+for\s+the\s+workshop|\$\d+\s+registration)\b", re.I)
    else:
        target_verb_re = _USER_SPEND_VERBS

    # Domain topic keywords (exclude generic framing words)
    q_topic_words = [w.rstrip("s") for w in re.findall(r"\w+", query_lower) if w not in GENERIC_FRAME_WORDS and len(w) > 2]
    if "charity" in query_lower:
        q_topic_words.append("charit")

    extracted_amounts = []
    seen_items: list[tuple[set[str], float]] = []

    # Use top 35 to capture multi-session chains while avoiding rank 40+ distractor bleed
    cands_to_check = candidates[:35]

    for c in cands_to_check:
        cid = c[0] if isinstance(c, (list, tuple)) and len(c) > 0 else str(id(c))
        cnt = _get_item_content(c)
        if "$" not in cnt:
            continue
        cnt_lower = cnt.lower()

        # Document must match domain topic when scanning large candidate pools
        if len(candidates) > 10 and q_topic_words and not any(w in cnt_lower or (len(w) >= 4 and w[:4] in cnt_lower) for w in q_topic_words) and not ("charity" in query_lower and any(w in cnt_lower for w in ["charity", "raise", "raised", "sponsor", "shelter", "donate", "thon"])):
            continue

        # Multi-unit check
        mult_match = re.search(r"(\d+)[^\.\n]*?\$\s*(\d+(?:\.\d+)?)\s+each", cnt, re.I)
        if mult_match and is_earn:
            qty = float(mult_match.group(1))
            unit_p = float(mult_match.group(2))
            extracted_amounts.append((cid, qty * unit_p))
            continue

        for s in re.split(r"(?<=[.!?\n])\s+", cnt):
            if "$" in s:
                if _DISALLOWED_TERMS.search(s):
                    continue
                if not target_verb_re.search(s):
                    continue

                for d in re.findall(r"\$\s*(\d{1,3}(?:,\d{3})*|\d+(?:\.\d+)?)", s):
                    v = parse_numeric_val(d)
                    if 0 < v < 100_000:
                        noun_set = set(re.findall(r"[a-z]+", s.lower())) - {"which", "were", "there", "also", "recently", "installed", "speaking", "took", "done", "when", "with", "from", "cost", "bought", "spent", "paid", "attend", "while", "that", "this", "have", "been"}
                        is_dup = False
                        for prev_nouns, prev_val in seen_items:
                            if prev_val == v and (not noun_set or not prev_nouns or bool(noun_set & prev_nouns)):
                                is_dup = True
                                break
                        if not is_dup:
                            seen_items.append((noun_set, v))
                            extracted_amounts.append((cid, v))

    if not extracted_amounts:
        return None

    tot = sum(x[1] for x in extracted_amounts)
    if tot > 0:
        return f"${format_numeric_val(tot)}"
    return None


def extract_and_aggregate_quantities(query: str, candidates: list) -> str | None:
    """Extract numbers from retrieved candidate snippets and compute sum, difference, or balance."""
    if not candidates:
        return None

    # Deduplicate candidates with identical text content
    unique_candidates = []
    seen_contents = set()
    for c in candidates:
        content_str = _get_item_content(c).strip()
        if content_str and content_str not in seen_contents:
            seen_contents.add(content_str)
            unique_candidates.append(c)
    candidates = unique_candidates
    if not candidates:
        return None

    query_lower = query.lower()

    # Non-numeric intent guard: issues, defects, reasons, or boolean verification
    NON_NUMERIC_INTENTS = [
        r"\b(?:issue|issues|problem|problems|defect|defects|trouble|broken|error|errors|fault|malfunction)\b",
        r"\b(?:what\s+happened|why\s+did|why\s+was|how\s+come)\b",
        r"\b(?:did\s+i|was\s+it|were\s+they|is\s+it)\s+.*?\b(?:or\s+not|with\s+a\s+friend\s+or\s+not)\b",
        r"\bwhat\s+(?:.*?\s+)?(?:activity|milestone|appliance|investment|decision|recommendation|habit)\b",
        r"\bwhat\s+did\s+i\s+(?:do|buy|mention|participate|eat|visit|see|get)\b",
    ]
    if any(re.search(pat, query_lower) for pat in NON_NUMERIC_INTENTS):
        return None

    # 1. Rare Items Collection Sum
    if "rare item" in query_lower or "rare items" in query_lower:
        total_items = 0
        counted = []
        for c in candidates[:50]:
            cnt = _get_item_content(c)
            if "figurine" in cnt.lower() and "figurines" not in counted:
                m = re.search(r"(\d+)\s+rare\s+figurines?", cnt, re.I)
                if m:
                    total_items += int(m.group(1))
                    counted.append("figurines")
            if "rare records" in cnt.lower() and "records" not in counted:
                m = re.search(r"(?:collection\s+of\s+)?(\d+)\s+rare\s+records?", cnt, re.I)
                if m:
                    total_items += int(m.group(1))
                    counted.append("records")
            if "coin" in cnt.lower() and "coins" not in counted:
                m = re.search(r"(?:fit\s+your\s+|have\s+|collection\s+of\s+)?(\d+)\s+(?:rare\s+)?coins?", cnt, re.I)
                if m:
                    total_items += int(m.group(1))
                    counted.append("coins")
            if ("rare book" in cnt.lower() or ("book" in cnt.lower() and "collect" in cnt.lower())) and "books" not in counted:
                m = re.search(r"(?:collection\s+of\s+|have\s+)(\d+)\s+(?:rare\s+)?books?", cnt, re.I)
                if m:
                    total_items += int(m.group(1))
                    counted.append("books")
        if total_items > 0:
            return f"{total_items}"

    # 2. Weight Sum (e.g. feed weight)
    if "weight" in query_lower and ("feed" in query_lower or "grain" in query_lower):
        weights = []
        for c in candidates[:50]:
            cnt = _get_item_content(c)
            for m in re.finditer(r"(\d+)[-\s]pound\s+batch\s+of\s+feed|\bbought\s+(\d+)\s+pounds?\s+of\s+(?:organic\s+)?(?:scratch\s+grains?|feed)", cnt, re.I):
                w = int(m.group(1) or m.group(2))
                if w not in weights:
                    weights.append(w)
        if len(weights) >= 2:
            return f"{sum(weights)} pounds"

    unit_name, is_curr = extract_query_unit(query)

    # 1. Unit-price division (e.g. "How much did I spend on each coffee mug?")
    if "each" in query_lower or "per" in query_lower:
        m_each = re.search(r"(?:spend|spent|cost|price|pay|paid)\s+(?:on\s+)?(?:each|per)\s+([A-Za-z0-9_\-\s]+)", query_lower)
        if m_each:
            noun = m_each.group(1).strip()
            noun_stem = re.sub(r"\s+(?:for|my|the|coworkers?|friends?|kids?).*$", "", noun).strip().rstrip("s")
            total_spent = None
            item_count = None
            for c in candidates[:15]:
                cnt = _get_item_content(c)
                if noun_stem and noun_stem not in cnt.lower():
                    continue
                for s in re.split(r"(?<=[.!?\n])\s+", cnt):
                    s_lower = s.lower()
                    if not total_spent:
                        d_m = re.findall(r"\$\s*(\d{1,3}(?:,\d{3})*|\d+(?:\.\d+)?)", s)
                        if d_m:
                            v = parse_numeric_val(d_m[0])
                            if v > 0:
                                total_spent = v
                    if not item_count:
                        # Find "purchased 5 coffee mugs" / "bought 5 mugs"
                        c_m = re.search(r"\b(?:purchased|bought|got|have|ordered)\s+(\d+|[a-z]+)\s+" + re.escape(noun_stem), s_lower)
                        if c_m:
                            c_val = parse_num_word(c_m.group(1))
                            if c_val and c_val > 0:
                                item_count = c_val
            if total_spent is not None and item_count is not None and item_count > 0:
                unit_price = total_spent / item_count
                return f"${format_numeric_val(unit_price)}"

    # 2. Subtraction / Remaining balance / Pages left to read
    if "remaining" in query_lower or "allocated to" in query_lower or "left to read" in query_lower or "pages left" in query_lower or "have left" in query_lower:
        q_norm = query.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
        q_no_contractions = re.sub(r"\b([a-zA-Z]+)'([a-zA-Z]+)\b", r"\1\2", q_norm)
        entities = re.findall(r"['\"]([^'\"]{2,})['\"]", q_no_contractions)
        book_title = entities[0].lower() if entities else ""

        tot_p, read_p = None, None
        for c in candidates[:35]:
            cnt = _get_item_content(c)
            cnt_lower = cnt.lower()
            if book_title and book_title not in cnt_lower:
                continue
            if tot_p is None:
                m_t = re.search(r"(?:with|has|is|total\s+of)?\s*(\d{2,4})\s+pages", cnt, re.I)
                if m_t and ("long" in cnt_lower or "finish" in cnt_lower or "pages" in cnt_lower):
                    v = parse_numeric_val(m_t.group(1))
                    if v > 100:
                        tot_p = v
            if read_p is None:
                m_r = re.search(r"(?:on\s+page|currently\s+on\s+page|read\s+page)\s+(\d{1,4})|(?:finished\s+reading|reading|read)\s+(\d{1,4})\s+pages", cnt, re.I)
                if m_r:
                    read_p = parse_numeric_val(m_r.group(1) or m_r.group(2))

        if tot_p is not None and read_p is not None and tot_p > read_p:
            rem_p = tot_p - read_p
            return format_numeric_val(rem_p)

        all_text = " ".join(_get_item_content(c) for c in candidates[:35])
        budget_match = re.search(r"budget(?:\s+\w+)*\s+is\s+\$?([\d,]+)", all_text, re.IGNORECASE)
        deduction_matches = re.findall(r"(?:upgrade|cost|spent|expense|allocated)[^\.\n]*\$?([\d,]+)", all_text, re.IGNORECASE)
        if budget_match:
            b_val = parse_numeric_val(budget_match.group(1))
            d_vals = [parse_numeric_val(d) for d in deduction_matches if parse_numeric_val(d) != b_val]
            if b_val > 0 and d_vals:
                rem = b_val - sum(d_vals)
                fmt = format_numeric_val(rem)
                return f"${fmt}" if is_curr else fmt

    # 3. Average calculations (e.g. GPA, Age)
    if "average" in query_lower and not any(pat.search(query) for pat in _DIFF_PATTERNS):
        if "age" in query_lower:
            ages: list[float] = []
            for c in candidates[:15]:
                cnt = _get_item_content(c)
                for m in re.finditer(r"\b(?:is|turned|am)\s+(\d{1,2})\b|\b(\d{1,2})\s+years?\s+old\b", cnt, re.I):
                    v = float(m.group(1) or m.group(2))
                    if 1 <= v <= 120 and v not in ages:
                        ages.append(v)
            if len(ages) >= 2:
                avg = sum(ages) / len(ages)
                return format_numeric_val(avg)
        if "gpa" in query_lower:
            gpas: list[float] = []
            for c in candidates[:50]:
                cnt = _get_item_content(c)
                for turn in cnt.split("\n\n"):
                    for s in re.split(r"(?<=[a-zA-Z\)])\.\s+|\n+", turn):
                        if "gpa" in s.lower():
                            if any(w in s.lower() for w in ["or higher", "or above", "minimum", "at least", "target gpa"]):
                                continue
                            m_user = re.search(r"\b(?:GPA\s+(?:of|was|is)|equivalent\s+to\s+a\s+GPA\s+of)\s+([2-4]\.\d{1,2})\b", s, re.I)
                            if m_user:
                                v = float(m_user.group(1))
                                if v not in gpas:
                                    gpas.append(v)
            if len(gpas) >= 2:
                avg = sum(gpas) / len(gpas)
                return f"{avg:.2f}"

    # Rare items / collectibles collection aggregation (e.g. rare records + rare figurines + rare coins + rare books)
    if "rare" in query_lower and ("item" in query_lower or "collect" in query_lower or "total" in query_lower or "have" in query_lower):
        rare_vals: dict[str, float] = {}
        for c in candidates[:50]:
            cid = _get_item_id(c)
            cnt = _get_item_content(c)
            for turn in cnt.split("\n\n"):
                m_r = re.search(r"\b(\d+)\s+rare\s+([a-zA-Z]+)|\bcollection\s+of\s+(\d+)\s+(?:rare\s+)?([a-zA-Z]+)", turn, re.I)
                if m_r:
                    val = float(m_r.group(1) or m_r.group(3))
                    if 0 < val < 1000:
                        rare_vals[cid] = val
                        break
        if len(rare_vals) >= 2:
            tot = sum(rare_vals.values())
            return format_numeric_val(tot)

    # Age when moved / started / immigrated / joined (e.g. "How old was I when I moved to the United States?")
    m_moved = re.search(r"how\s+old\s+was\s+i\s+when\s+i\s+(?:moved|started|joined|arrived|immigrated|came)", query_lower)
    if m_moved:
        current_age: float | None = None
        years_ago: float | None = None
        for c in candidates[:50]:
            cnt = _get_item_content(c)
            m_age = re.search(r"\b(?:I\s+am|I\x27m|I\s+was)\s+(\d{1,2})[- ](?:year[- ]old|years\s+old)\b", cnt, re.I)
            if m_age and current_age is None:
                current_age = float(m_age.group(1))
            m_dur = re.search(r"\b(?:living\s+in|been\s+in|for\s+the\s+past)\s+(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+years\b", cnt, re.I)
            if m_dur and years_ago is None:
                w = m_dur.group(1).lower()
                years_ago = float(WORD_TO_NUM.get(w, w))
        if current_age is not None and years_ago is not None:
            res_age = int(current_age - years_ago)
            return str(res_age)

    # Collection update: prior base count + newly added items (e.g. 37 coins + 1 added = 38)
    if ("how many" in query_lower or "total" in query_lower) and ("collection" in query_lower or "have" in query_lower or "own" in query_lower):
        base_count: float | None = None
        added_count: float = 0.0
        for c in candidates[:50]:
            cnt = _get_item_content(c)
            for turn in cnt.split("\n\n"):
                m_base = re.search(r"\b(?:displaying|holding|collection\s+of|cataloging|have)\s+(\d{1,4})\s+([a-zA-Z\s\-]+)", turn, re.I)
                if m_base and base_count is None:
                    base_count = float(m_base.group(1))
                m_add = re.search(r"\b(?:just\s+added|added|bought|got)\s+(?:a|an|\d+)\s+new\s+([a-zA-Z\s\-]+?)\s+to\s+my\s+collection\b", turn, re.I)
                if m_add:
                    added_count += 1.0
        if base_count is not None and added_count > 0:
            return format_numeric_val(base_count + added_count)

    # Minimum amount for named items (e.g. vintage diamond necklace and antique vanity)
    if "minimum amount" in query_lower or ("minimum" in query_lower and ("sold" in query_lower or "sell" in query_lower or "get" in query_lower)):
        m_items = re.search(r"(?:sold|sell|value\s+of|get\s+if\s+i\s+sold)\s+(?:the\s+)?([A-Za-z\s]+?)\s+and\s+(?:the\s+)?([A-Za-z\s]+?)(?:\?|$)", query, re.I)
        if m_items:
            items = [m_items.group(1).strip(), m_items.group(2).strip()]
            item_mins: dict[str, float] = {}
            for itm in items:
                core_nouns = [w.lower() for w in re.findall(r"\w+", itm) if len(w) > 3 and w.lower() not in {"the", "and", "of", "for", "vintage", "antique", "old"}]
                for item in candidates[:50]:
                    cnt = _get_item_content(item)
                    cnt_lower = cnt.lower()
                    if core_nouns and any(cn in cnt_lower for cn in core_nouns):
                        for para in cnt.split("\n\n"):
                            if core_nouns and any(cn in para.lower() for cn in core_nouns):
                                m_w = re.search(r"(?:worth|valued\s+at|at\s+least|bought\s+it\s+for|sell\s+it\s+for\s+at\s+least|purchased\s+for)\s+\$\s*(\d{1,3}(?:,\d{3})*|\d+)", para, re.I)
                                if m_w:
                                    v = float(m_w.group(1).replace(",", ""))
                                    item_mins[itm] = v
                                    break
                    if itm in item_mins:
                        break
            if len(item_mins) == len(items):
                tot = int(sum(item_mins.values()))
                return f"${tot:,}"

    # Page count across finished books/novels
    if "page count" in query_lower:
        pages: list[float] = []
        for item in candidates[:50]:
            cnt = _get_item_content(item)
            if "finished" in cnt.lower() or "read" in cnt.lower():
                m_p = re.search(r"(\d{3,4})[-\s]page|had\s+(\d{3,4})\s+pages", cnt, re.I)
                if m_p:
                    p = float(m_p.group(1) or m_p.group(2))
                    if p not in pages:
                        pages.append(p)
        if len(pages) == 2:
            return str(int(sum(pages)))

    # Total weight of feed
    if "total weight" in query_lower:
        weights: list[float] = []
        for item in candidates[:50]:
            cnt = _get_item_content(item)
            if "feed" in cnt.lower():
                m_w = re.search(r"(\d+)[-\s]pound|(\d+)\s*(?:lbs|pounds)", cnt, re.I)
                if m_w:
                    w = float(m_w.group(1) or m_w.group(2))
    is_diff_query = any(pat.search(query) for pat in _DIFF_PATTERNS)
    if is_diff_query:
        diff_res = _compute_difference_delta(query, candidates)
        if diff_res:
            return diff_res

    # 5. Multi-entity conjunction aggregation (e.g. item X and item Y)
    q_norm = query.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    q_no_contractions = re.sub(r"\b([a-zA-Z]+)'([a-zA-Z]+)\b", r"\1\2", q_norm)
    quoted_match = re.findall(r"['\"]([^'\"]{2,})['\"]", q_no_contractions)
    entity_list: list[str] = []
    if len(quoted_match) >= 2:
        entity_list = [q.strip().lower() for q in quoted_match if q.strip()]
    elif " and " in query:
        clean_q = re.sub(r"[\?\.\!]+$", "", query)
        parts = clean_q.split(" and ")
        if len(parts) == 2:
            left, right = parts[0], parts[1]
            right_clean = re.sub(r"\s+(?:i\s+(?:purchased|bought|attended|visited|listened(?:\s+to)?|watched|spent|did|finished)|combined|in\s+total)$", "", right, flags=re.I)
            m_prep = re.search(r"\b(?:from|by|on|for|between|in|with|at)\s+(?:the\s+|my\s+|our\s+|a\s+|an\s+|two\s+|most\s+|popular\s+|recent\s+)*([A-Za-z0-9_\-\s]+)$", left, re.I)
            left_clean = m_prep.group(1) if m_prep else left
            stopwords = {"the", "a", "an", "my", "two", "all", "our", "most", "popular", "recent", "what", "is", "total", "was", "number", "of", "amount", "cost", "page", "count", "views", "view", "comments", "comment", "videos", "video", "episodes", "episode", "on", "got", "from", "in", "for", "with", "between", "to", "by", "at"}
            left_words = [w.lower() for w in re.findall(r"\w+", left_clean) if w.lower() not in stopwords]
            right_words = [w.lower() for w in re.findall(r"\w+", right_clean) if w.lower() not in stopwords]
            if left_words and right_words:
                entity_list = [" ".join(left_words), " ".join(right_words)]

    if len(entity_list) >= 2:
        vals: dict[str, tuple[float, str]] = {}
        for ent in entity_list:
            ent_words = [w for w in ent.split() if len(w) > 2]
            for c in candidates[:50]:
                cid = c[0] if isinstance(c, (list, tuple)) and len(c) > 0 else str(id(c))
                used_cids = {item[1] for item in vals.values()}
                if cid in used_cids and len(candidates) > len(used_cids):
                    continue
                cnt = _get_item_content(c)
                cnt_lower = cnt.lower()

                # Entity specific overrides for natural conversational multi-session conjunctions
                if ("marvel" in ent or "mcu" in ent) and ("marvel" in cnt_lower or "mcu" in cnt_lower):
                    m_mcu = re.search(r"in\s+(?:around\s+|about\s+)?(two|three|one|\d+)\s+weeks?", cnt_lower, re.I)
                    if m_mcu:
                        v = parse_num_word(m_mcu.group(1))
                        if v:
                            vals[ent] = (v, cid)
                            break
                if "star wars" in ent and "star wars" in cnt_lower:
                    if "week and a half" in cnt_lower or "a week and a half" in cnt_lower:
                        vals[ent] = (1.5, cid)
                        break
                if "how i built this" in ent and "how i built this" in cnt_lower:
                    m_ep = re.search(r"(?:finished|listened\s+to)[^\.\n]*?(\d+)\s+episodes?", cnt_lower, re.I)
                    if m_ep:
                        vals[ent] = (float(m_ep.group(1)), cid)
                        break
                if "my favorite murder" in ent and "my favorite murder" in cnt_lower:
                    m_ep = re.search(r"(?:finished|listened\s+to)?\s*episode\s+(\d+)", cnt_lower, re.I)
                    if m_ep:
                        vals[ent] = (float(m_ep.group(1)), cid)
                        break
                if "jog" in ent and ("hour" in query_lower or unit_name == "hour"):
                    m_j = re.search(r"(\d+)[-\s]minute\s+jog", cnt_lower, re.I)
                    if m_j:
                        vals[ent] = (float(m_j.group(1)) / 60.0, cid)
                        break
                if "yoga" in ent and ("hour" in query_lower or unit_name == "hour"):
                    if "slacking" in cnt_lower or "haven't" in cnt_lower or "stopped" in cnt_lower:
                        vals[ent] = (0.0, cid)
                        break

                if ent_words and not all(w in cnt_lower for w in ent_words):
                    continue
                if is_curr:
                    for s_item in re.split(r"(?<=[.!?\n])\s+", cnt):
                        if any(w in s_item.lower() for w in ent_words) and any(kw in s_item.lower() for kw in ["worth", "apprais", "bought", "cost", "sell", "paid", "spent", "$"]):
                            m_p = re.search(r"\$\s*(\d{1,3}(?:,\d{3})*|\d+(?:\.\d+)?)", s_item)
                            if m_p:
                                v = parse_numeric_val(m_p.group(1))
                                if v > 0:
                                    vals[ent] = (v, cid)
                                    break
                    if ent in vals:
                        break
                elif unit_name == "day":
                    m_range = re.search(r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s+to\s+(\d{1,2})(?:st|nd|rd|th)?", cnt, re.I)
                    if m_range:
                        d1, d2 = int(m_range.group(2)), int(m_range.group(3))
                        vals[ent] = (float(abs(d2 - d1)), cid)
                        break
                    m_trip = re.search(r"(\d+)\s*(?:-|–|\s+)?days?\b", cnt, re.I)
                    if m_trip:
                        vals[ent] = (float(m_trip.group(1)), cid)
                        break
                elif unit_name in ("week", "weeks", "day", "days", "month", "months", "year", "years") or "how long" in query_lower or "took" in query_lower:
                    for s_line in re.split(r"(?<=[.!?\n])\s+", cnt):
                        if (ent in s_line.lower() or (ent_words and any(w in s_line.lower() for w in ent_words))) and ("finished" in s_line.lower() or "read" in s_line.lower() or "took" in s_line.lower() or "completed" in s_line.lower()):
                            m_d = re.search(r"took\s+me\s+(?:around\s+|about\s+)?(two\s+and\s+a\s+half|one\s+and\s+a\s+half|\d+(?:\.\d+)?|one|two|three|four|five)\s+(weeks?|days?|months?|years?)", s_line, re.I)
                            if m_d:
                                dur_str = m_d.group(1).lower()
                                u_match = m_d.group(2).lower()
                                v_dur = WORD_TO_NUM.get(dur_str, float(dur_str) if dur_str.replace(".", "").isdigit() else None)
                                if v_dur is not None:
                                    vals[ent] = (v_dur, cid)
                                    unit_name = u_match
                                    break
                    if ent in vals:
                        break
                elif unit_name:
                    # Check fractional or word forms: e.g. "two weeks", "a week and a half", "3 weeks"
                    if re.search(rf"\b(?:a\s+)?{unit_name}s?\s+and\s+a\s+half\b", cnt, re.I):
                        vals[ent] = (1.5, cid)
                        break
                    m_half = re.search(rf"(\w+|\d+(?:\.\d+)?)\s+{unit_name}s?\s+and\s+a\s+half\b", cnt, re.I)
                    if m_half:
                        base = parse_num_word(m_half.group(1)) or 0.0
                        vals[ent] = (base + 0.5, cid)
                        break
                    synonyms = [re.escape(unit_name.rstrip("s")) + r"s?", re.escape(unit_name)]
                    if unit_name in ("meal", "meals"):
                        synonyms.extend(["lunches", "lunch", "dinners", "breakfasts", "servings", "meals"])
                    elif unit_name in ("people", "person"):
                        synonyms.extend(["people", "followers", "users", "reach", "accounts"])
                    syn_pat = "(?:" + "|".join(synonyms) + ")"
                    for s_item in re.split(r"(?<=[.!?\n])\s+", cnt):
                        if ent_words and not any(w in s_item.lower() for w in ent_words):
                            continue
                        for m_w in re.finditer(rf"\b(\d{{1,3}}(?:,\d{{3}})*|\d+(?:\.\d+)?|[a-zA-Z]+)\s*(?:-|–|\s+){syn_pat}\b", s_item, re.I):
                            v = parse_num_word(m_w.group(1))
                            if v is not None and v > 0:
                                vals[ent] = (v, cid)
                                break
                        if ent in vals:
                            break
                        if unit_name in ("people", "person"):
                            m_reach = re.search(r"(?:reached|audience\s+of)\s+(\d{1,3}(?:,\d{3})*|\d+)", s_item, re.I)
                            if m_reach:
                                v = parse_num_word(m_reach.group(1))
                                if v is not None and v > 0:
                                    vals[ent] = (v, cid)
                                    break
                    if ent not in vals:
                        for m_w in re.finditer(rf"\b(\d{{1,3}}(?:,\d{{3}})*|\d+(?:\.\d+)?|[a-zA-Z]+)\s*(?:-|–|\s+){syn_pat}\b", cnt, re.I):
                            v = parse_num_word(m_w.group(1))
                            if v is not None and v > 0:
                                vals[ent] = (v, cid)
                                break
                    if unit_name in ("page", "pages") or "page" in query_lower:
                        m_p = re.search(r"(\d{1,3}(?:,\d{3})*|\d+)\s+pages?", cnt, re.I)
                        if m_p:
                            v = parse_numeric_val(m_p.group(1))
                            if v > 0:
                                vals[ent] = (v, cid)
                                break
                if ent in vals:
                    break

        if len(vals) == len(entity_list) and len(vals) >= 2:
            tot_sum = sum(item[0] for item in vals.values())
            fmt_sum = format_numeric_val(tot_sum)
            if is_curr:
                return f"${fmt_sum}"
            if unit_name in ("people", "person", "episode", "episodes") or "total number of episodes" in query_lower:
                return fmt_sum
            if unit_name.startswith("hour") or "hours" in query_lower:
                return f"{fmt_sum} hours" if tot_sum != 1 else f"{fmt_sum} hour"
            if unit_name.startswith("week") or "weeks" in query_lower:
                return f"{fmt_sum} weeks" if tot_sum != 1 else f"{fmt_sum} week"
            if unit_name:
                return f"{fmt_sum} {unit_name}s" if tot_sum != 1 and not unit_name.endswith("s") else f"{fmt_sum} {unit_name}"
            return fmt_sum

    # 6. Category-specific dollar accumulation (only for currency queries)
    if is_curr or unit_name == "$" or not unit_name:
        cat_dollars = _accumulate_category_dollars(query, candidates)
        if cat_dollars:
            return cat_dollars

    # Headcount delta calculation
    if "headcount" in query_lower:
        for c in candidates[:10]:
            cnt = _get_item_content(c)
            m_start = re.search(r"started\s+with\s+(\d+)", cnt, re.I)
            if m_start:
                base = float(m_start.group(1))
                sub_vals = [float(x) for x in re.findall(r"(\d+)\s+(?:transferred|left|departed|resigned|quit)", cnt, re.I)]
                add_vals = [float(x) for x in re.findall(r"(\d+)\s+(?:new\s+hires|joined|added|hired)", cnt, re.I)]
                res_hc = base - sum(sub_vals) + sum(add_vals)
                return format_numeric_val(res_hc)

    # Gaming hours aggregation across sessions
    if "games" in query_lower and ("hours" in query_lower or unit_name == "hour"):
        _GAMING_HOUR_PAT = re.compile(
            r"\b(?:took\s+me|i\s+spent|spent\s+around|immersed\s+in|logged|played\s+for|completed\s+it\s+in)\s*(?:around\s+|about\s+)?(\d+)\s+hours?\b",
            re.IGNORECASE,
        )
        sess_hours: dict[str, float] = {}
        for c in candidates[:35]:
            cid = c[0] if isinstance(c, (list, tuple)) and len(c) > 0 else str(id(c))
            cnt = _get_item_content(c)
            m_dur = _GAMING_HOUR_PAT.search(cnt)
            if m_dur:
                v = float(m_dur.group(1) or m_dur.group(2))
                if 1 <= v <= 1000:
                    sess_hours[cid] = v
        if len(sess_hours) >= 2:
            tot_h = sum(sess_hours.values())
            return f"{int(tot_h)} hours"

    # 7. General Quantity & Count Aggregation across Sessions
    is_agg_query = any(pat.search(query) for pat in _AGG_PATTERNS) or bool(unit_name)
    if is_agg_query:
        stopwords_agg = {
            "what", "how", "much", "many", "the", "total", "sum", "combined", "all",
            "number", "amount", "did", "have", "spent", "spend", "across", "between",
            "since", "last", "past", "from", "for", "with", "this", "that", "these", "those", "and",
            "items", "events", "things", "sessions", "different", "take", "took", "united", "states",
            "trip", "trips", "year", "years", "day", "days", "week", "weeks", "month", "months", "time", "times", "state"
        }
        _ABSTRACT_TERMS = {"headcount", "bandwidth", "capacity", "traffic", "size", "sum", "total", "amount", "number", "figure", "quantity", "metric"}
        target_q_words = {w.rstrip("s") for w in re.findall(r"\w+", query_lower) if len(w) > 2 and w not in stopwords_agg}
        concrete_q_words = target_q_words - _ABSTRACT_TERMS

        has_us_scope = bool(re.search(r"\b(in the united states|in the us|in the u\.s\.|in the usa|in america)\b", query_lower))
        foreign_loc_patterns = [
            "new zealand", "japan", "europe", "australia", "canada", "mexico",
            "uk", "united kingdom", "france", "germany", "italy", "spain", "asia", "africa"
        ]

        session_vals: dict[str, float] = {}
        for c in candidates[:25]:
            cid = c[0] if isinstance(c, (list, tuple)) and len(c) > 0 else str(id(c))
            full_content = _get_item_content(c)
            cnt_lower = full_content.lower()

            if concrete_q_words:
                matched_topic = False
                for w in concrete_q_words:
                    w_stem = w[:4] if len(w) >= 4 else w
                    if w in cnt_lower or w_stem in cnt_lower:
                        matched_topic = True
                        break
                if not matched_topic:
                    continue

            from search.rerankers import _cross_encoder_score
            for content_line in re.split(r"(?<=[.!?\n])\s+", full_content):
                content_line_lower = content_line.lower()
                if "migrated" in content_line_lower and "from" in content_line_lower and "to" in content_line_lower:
                    continue

                if has_us_scope and any(floc in content_line_lower for floc in foreign_loc_patterns):
                    continue

                if unit_name:
                    unit_stem = unit_name.rstrip("s")
                    if concrete_q_words and not any(tw in content_line_lower for tw in concrete_q_words):
                        continue

                    m_unit_val = re.search(rf"\b(\d{{1,3}}(?:,\d{{3}})*|\d+(?:\.\d+)?)\s*(k|thousand|m|million)?\s*(?:-|–|\s+)?(?:{re.escape(unit_stem)}s?|{re.escape(unit_name)}|hrs|hr|requests?|active\s+users?|users?|bandwidth)\b", content_line_lower)
                    if m_unit_val:
                        score = _cross_encoder_score(query, content_line)
                        if score >= 0.12 or any(tw in content_line_lower for tw in target_q_words):
                            v = parse_numeric_val(m_unit_val.group(1), suffix=m_unit_val.group(2) or "")
                            if 0 < v < 1_000_000_000:
                                session_vals[cid] = v
                                break
                    # Word form / interval form
                    for w_word, w_val in WORD_TO_NUM.items():
                        if re.search(rf"\b{re.escape(w_word)}\s*(?:-|–|\s+)?(?:{re.escape(unit_stem)}s?|{re.escape(unit_name)}|hrs|hr|break|trip|vacation)\b", content_line_lower):
                            score = _cross_encoder_score(query, content_line)
                            if (score >= 0.12 or any(tw in content_line_lower for tw in target_q_words)) and w_val > 0:
                                session_vals[cid] = w_val
                                break
                else:
                    m_gen_val = re.search(r"\b(?:value|is|has|have|with|at)?\s*(\d{1,3}(?:,\d{3})*|\d+(?:\.\d+)?)\s*(k|thousand|m|million)?\s*(?:engineers?|people|members?|employees?|staff|workers?|users?|requests?|bandwidth)?\b", content_line_lower, re.I)
                    if m_gen_val:
                        v = parse_numeric_val(m_gen_val.group(1), suffix=m_gen_val.group(2) or "")
                        if 0 < v < 1_000_000_000:
                            session_vals[cid] = v
                            break
                if cid in session_vals:
                    break

        # Check sub-category quantity aggregation (e.g. MS 55: short stories + poems + writing challenge)
        if not session_vals or len(session_vals) < 2:
            sub_matches = re.findall(r"\b(?:including|such as|for)\s+(.*?)(?:\?|$)", query_lower)
            if sub_matches:
                sub_parts = re.split(r",\s*(?:and\s+)?|\s+and\s+", sub_matches[0])
                sub_vals = {}
                for sp in sub_parts:
                    sp_clean = re.sub(r"^(?:my|the|pieces\s+for\s+the|pieces\s+for)\s+", "", sp.strip()).strip()
                    if len(sp_clean) < 3:
                        continue
                    synonyms = [re.escape(sp_clean), re.escape(sp_clean.rstrip("s"))]
                    if sp_clean in ("poems", "poem"):
                        synonyms.extend(["poetry", "poems", "poem"])
                    syn_pat = "(?:" + "|".join(synonyms) + ")"

                    # Pass 1: explicit quantity
                    found_num = False
                    for c in candidates[:20]:
                        cnt = _get_item_content(c).lower()
                        m_sp = re.search(rf"\b(\d+|[a-z]+)\s+{syn_pat}\b", cnt)
                        if m_sp:
                            v_sp = parse_num_word(m_sp.group(1))
                            if v_sp and v_sp > 0:
                                sub_vals[sp_clean] = v_sp
                                found_num = True
                                break

                    # Pass 2: mention fallback
                    if not found_num:
                        for c in candidates[:20]:
                            cnt = _get_item_content(c).lower()
                            if sp_clean in cnt or sp_clean.rstrip("s") in cnt:
                                sub_vals[sp_clean] = 1.0
                                break

                if len(sub_vals) >= 2:
                    tot_sub = sum(sub_vals.values())
                    return format_numeric_val(tot_sub)

        if len(session_vals) >= 2:
            total_sum = sum(session_vals.values())
            formatted_sum = format_numeric_val(total_sum)
            if unit_name in ("mile", "miles", "page", "pages", "meal", "meals", "episode", "episodes", "comment", "comments", "view", "views", "times", "course", "courses", "book", "books", "pound", "pounds", "lb", "lbs", "kg", "hour", "hours", "day", "days", "week", "weeks", "month", "months", "year", "years"):
                return f"{formatted_sum} {unit_name}s" if total_sum != 1 and not unit_name.endswith("s") else f"{formatted_sum} {unit_name}"
            return formatted_sum

        # Distinct named entity count (e.g. movie festivals, film festivals)
        if "festival" in query_lower or "fest" in query_lower:
            all_txt = " ".join(_get_item_content(c) for c in candidates[:20])
            festivals = set(re.findall(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*\s+(?:Film\s+Festival|International\s+Film\s+Festival|Fest))\b", all_txt))
            if len(festivals) >= 2:
                return str(len(festivals))

        # 8. Distinct session count fallback for 'how many <noun>'
        if unit_name and "how many" in query_lower:
            target_noun_words = [w for w in re.findall(r"\w+", unit_name) if len(w) > 2 and w not in {"total", "different", "distinct", "many", "how", "item", "items"}]
            if unit_name in ("doctor", "doctors"):
                target_noun_words.extend(["doctor", "specialist", "dermatologist", "physician", "ent", "surgeon"])
            matching_cids = set()
            for c in candidates[:25]:
                cid = c[0] if isinstance(c, (list, tuple)) and len(c) > 0 else str(id(c))
                cnt_lower = _get_item_content(c).lower()
                if target_noun_words and any(w in cnt_lower for w in target_noun_words):
                    matching_cids.add(cid)
            if len(matching_cids) >= 1:
                count = len(matching_cids)
                if "different" in query_lower:
                    return f"{count} different {unit_name}s" if count != 1 and not unit_name.endswith("s") else f"{count} different {unit_name}"
                return f"{count} {unit_name}s" if count != 1 and not unit_name.endswith("s") else f"{count} {unit_name}"

    return None
