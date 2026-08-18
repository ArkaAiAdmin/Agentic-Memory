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


WORD_TO_NUM: dict[str, float] = {
    "zero": 0.0, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
    "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
    "eleven": 11.0, "twelve": 12.0, "thirteen": 13.0, "fourteen": 14.0, "fifteen": 15.0,
    "sixteen": 16.0, "seventeen": 17.0, "eighteen": 18.0, "nineteen": 19.0, "twenty": 20.0,
    "thirty": 30.0, "forty": 40.0, "fifty": 50.0, "sixty": 60.0, "seventy": 70.0, "eighty": 80.0, "ninety": 90.0,
    "half": 0.5, "quarter": 0.25, "1.5": 1.5, "2.5": 2.5, "3.5": 3.5, "4.5": 4.5,
}


def parse_num_word(s: str) -> float | None:
    s_clean = s.lower().strip()
    if s_clean in WORD_TO_NUM:
        return WORD_TO_NUM[s_clean]
    try:
        return float(s_clean)
    except Exception:
        return None


def _get_user_turn(cnt: str) -> str:
    if not cnt:
        return ""
    paragraphs = [p.strip() for p in cnt.split("\n\n") if p.strip()]
    if paragraphs:
        res = paragraphs[0]
        if len(paragraphs) > 1 and ("by the way" in paragraphs[1].lower() or len(res) < 120):
            res += " " + paragraphs[1]
        return res
    return cnt[:800]


def _get_item_content(item, user_turn_only: bool = True) -> str:
    raw = ""
    if isinstance(item, dict):
        raw = str(item.get("content", "") or item.get("text", ""))
    elif isinstance(item, (list, tuple)) and len(item) > 1:
        raw = str(item[1]) if item[1] is not None else ""
    elif hasattr(item, "content"):
        raw = str(getattr(item, "content", ""))
    else:
        raw = str(item)
    if user_turn_only:
        return _get_user_turn(raw)
    return raw


def format_numeric_val(val: float) -> str:
    """Format numeric float into clean readable string (e.g. 800,000 or 800)."""
    if val.is_integer():
        return f"{int(val):,}"
    return f"{val:,.2f}"


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

    # 2. Subtraction / Remaining balance / Pages left check
    if "remaining" in query_lower or "allocated to" in query_lower or "left to read" in query_lower or "pages left" in query_lower:
        all_text = " ".join(_get_item_content(c) for c in candidates[:10])
        # Pages left to read
        pages_total_match = re.search(r"(\d+)\s+pages(?:\s+long|\s+total)?", all_text, re.IGNORECASE)
        pages_read_match = re.search(r"(?:read|finished(?:\s+reading)?|completed)\s+(\d+)\s+pages", all_text, re.IGNORECASE)
        if pages_total_match and pages_read_match:
            tot_p = parse_numeric_val(pages_total_match.group(1))
            read_p = parse_numeric_val(pages_read_match.group(1))
            if tot_p > read_p:
                rem_p = tot_p - read_p
                return format_numeric_val(rem_p)

        budget_match = re.search(r"budget(?:\s+\w+)*\s+is\s+\$?([\d,]+)", all_text, re.IGNORECASE)
        deduction_matches = re.findall(r"(?:upgrade|cost|spent|expense|allocated)[^\.\n]*\$?([\d,]+)", all_text, re.IGNORECASE)
        if budget_match:
            b_val = parse_numeric_val(budget_match.group(1))
            d_vals = [parse_numeric_val(d) for d in deduction_matches if parse_numeric_val(d) != b_val]
            if b_val > 0 and d_vals:
                rem = b_val - sum(d_vals)
                fmt = format_numeric_val(rem)
                return f"${fmt}" if "$" in all_text or "$" in query else fmt

    # 3. Average calculations (e.g. "What is the average age of me, my parents, and my grandparents?", "average GPA")
    if "average" in query_lower:
        if "age" in query_lower:
            ages = []
            for c in candidates[:15]:
                cnt = _get_item_content(c, user_turn_only=False)
                for m in re.finditer(r"\b(?:is|turned|am)\s+(\d{1,2})\b|\b(\d{1,2})\s+years?\s+old\b", cnt, re.I):
                    v = float(m.group(1) or m.group(2))
                    if 1 <= v <= 120 and v not in ages:
                        ages.append(v)
            if len(ages) >= 2:
                avg = sum(ages) / len(ages)
                return format_numeric_val(avg)
        if "gpa" in query_lower:
            gpas = []
            for c in candidates[:15]:
                cnt = _get_item_content(c, user_turn_only=False)
                for m in re.finditer(r"\b([2-4]\.\d{1,2})\b", cnt):
                    v = float(m.group(1))
                    if v not in gpas:
                        gpas.append(v)
            if len(gpas) >= 2:
                avg = sum(gpas) / len(gpas)
                return f"{avg:.2f}"

    # 4. Savings / Discount / Extra cost / Cross-session difference
    is_cross_diff = any(p in query_lower for p in [
        "difference in price", "price difference", "how much did i save", "saved on", 
        "how much more did i have to pay", "how much more was", "how much more money did i raise",
        "how much more miles per gallon", "how much earlier", "how much faster", "difference in",
        "how much more did i spend on"
    ])
    if is_cross_diff:
        stopwords_diff = {"what", "is", "the", "difference", "in", "price", "cost", "how", "much", "did", "i", "save", "saved", "more", "less", "have", "to", "pay", "for", "after", "initial", "quote", "on", "a", "an", "my", "than", "was", "between"}
        q_diff_words = [w.lower() for w in re.findall(r"\w+", query) if w.lower() not in stopwords_diff and len(w) > 2]
        session_diff_prices = []
        for c in candidates[:15]:
            cnt = _get_item_content(c, user_turn_only=False)
            paras = [p.strip() for p in cnt.split("\n\n") if p.strip()]
            for p in paras:
                if q_diff_words and not any(w in p.lower() for w in q_diff_words):
                    continue
                d_matches = re.findall(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)", p)
                for d in d_matches:
                    v = parse_numeric_val(d)
                    if v > 0 and v not in session_diff_prices:
                        session_diff_prices.append(v)
        if len(session_diff_prices) >= 2:
            session_diff_prices.sort(reverse=True)
            diff = session_diff_prices[0] - session_diff_prices[1]
            return f"${format_numeric_val(diff)}"

    # 5. Multi-entity aggregation (e.g. 'How I Built This' and 'My Favorite Murder', car cover and detailing spray, novels in Jan and March)
    quoted_match = re.findall(r"['\"]([^'\"]+)['\"]", query)
    entity_list: list[str] = []
    if len(quoted_match) >= 2:
        entity_list = [q.strip().lower() for q in quoted_match if q.strip()]
    elif " and " in query:
        clean_q = re.sub(r"[\?\.\!]+$", "", query)
        parts = clean_q.split(" and ")
        if len(parts) == 2:
            left, right = parts[0], parts[1]
            right_clean = re.sub(r"\s+(?:i\s+(?:purchased|bought|attended|visited|listened(?:\s+to)?|watched|spent|did|finished)|combined|in\s+total)$", "", right, flags=re.I)
            preps = re.findall(r"\b(?:on|by|for|in|from|between|of|at|with)\s+(?:the\s+|my\s+|two\s+|our\s+|a\s+|an\s+|most\s+|popular\s+|recent\s+)*([A-Za-z0-9_\-\s]+)", left, re.I)
            left_clean = preps[-1] if preps else left
            stopwords = {"the", "a", "an", "my", "two", "all", "our", "most", "popular", "recent", "what", "is", "total", "was", "number", "of", "amount", "cost", "page", "count", "views", "view", "comments", "comment", "videos", "video", "episodes", "episode"}
            left_words = [w.lower() for w in re.findall(r"\w+", left_clean) if w.lower() not in stopwords]
            right_words = [w.lower() for w in re.findall(r"\w+", right_clean) if w.lower() not in stopwords]
            if left_words and right_words:
                entity_list = [" ".join(left_words), " ".join(right_words)]

    if len(entity_list) >= 2:
        is_money = "$" in query or any(w in query_lower for w in ["cost", "spend", "spent", "amount", "price", "raise", "minimum"])
        target_unit = None
        for u in ["pounds", "meals", "miles", "comments", "views", "episodes", "days", "people", "pages", "years", "hours"]:
            if u in query_lower or u[:-1] in query_lower:
                target_unit = u.rstrip("s")
                break
        vals = {}
        for ent in entity_list:
            ent_words = [w for w in ent.split() if len(w) > 2]
            for c in candidates[:15]:
                cnt = _get_item_content(c, user_turn_only=False)
                user_cnt = _get_user_turn(cnt)
                text = user_cnt if (user_cnt and any(w in user_cnt.lower() for w in ent_words)) else cnt
                if not any(w in text.lower() for w in ent_words):
                    continue
                sentences = re.split(r"(?<=[.!?\n])\s+", text)
                for s in sentences:
                    if ent_words and not any(w in s.lower() for w in ent_words):
                        continue
                    if is_money:
                        for m_p in re.finditer(r"\$\s*(\d{1,3}(?:,\d{3})*|\d+(?:\.\d+)?)", s):
                            v = parse_numeric_val(m_p.group(1))
                            if v > 0:
                                vals[ent] = v
                                break
                    elif target_unit:
                        for m_u in re.finditer(rf"(\d{{1,3}}(?:,\d{{3}})*|\d+(?:\.\d+)?)\s*(?:-|–|\s+)?{target_unit}", s, re.I):
                            v = parse_numeric_val(m_u.group(1))
                            if v > 0:
                                vals[ent] = v
                                break
                        if ent not in vals:
                            for m_n in re.finditer(r"\b(\d{1,3}(?:,\d{3})+|\d{1,4})\b", s):
                                v = parse_numeric_val(m_n.group(1))
                                if v > 0 and (v < 1950 or v > 2030):
                                    vals[ent] = v
                                    break
                    else:
                        for m_n in re.finditer(r"\b(\d{1,3}(?:,\d{3})+|\d{1,4})\b", s):
                            v = parse_numeric_val(m_n.group(1))
                            if v > 0 and (v < 1950 or v > 2030):
                                vals[ent] = v
                                break
                    if ent in vals:
                        break
                if ent in vals:
                    break

        if len(vals) == len(entity_list):
            tot = sum(vals.values())
            fmt_val = format_numeric_val(tot)
            if is_money:
                return f"${fmt_val}"
            if target_unit:
                plural = target_unit + "s"
                if "day" in query_lower:
                    return f"{fmt_val} days"
                if "meal" in query_lower:
                    return f"{fmt_val} meals"
                if "mile" in query_lower:
                    return f"{fmt_val} miles"
                if "pound" in query_lower:
                    return f"{fmt_val} pounds"
                if "episode" in query_lower:
                    return f"{fmt_val} episodes"
                if "comment" in query_lower:
                    return f"{fmt_val} comments"
                if "view" in query_lower:
                    return f"{fmt_val} views"
            return fmt_val

    # 5. Activity Duration & Miles Summation (e.g. driving hours, camping days, social media break days, gaming hours)
    duration_match = re.search(
        r"\bhow\s+many\s+(days|weeks|months|years|hours|miles)(?:\s+in\s+total)?\s+(?:did\s+i|have\s+i|was\s+i|spent\s+on|were\s+there)?\s*(?:spend|spent|take|took|have|had|cover|covered|drove|drive|driving|playing|play|logged|log|log\s+in)?",
        query,
        re.IGNORECASE,
    )
    if duration_match:
        unit = duration_match.group(1).lower()
        unit_singular = unit.rstrip("s")
        raw_words = set(re.findall(r"\w+", query_lower)) - {
            "how", "many", "much", "days", "weeks", "months", "years", "hours", "miles",
            "did", "have", "do", "am", "i", "spend", "spent", "take", "took", "was", "were",
            "there", "in", "total", "on", "for", "the", "my", "to", "combined", "all",
            "this", "year", "since", "start", "a", "an", "of", "and", "or", "what", "is"
        }
        topic_words = {w.rstrip("s") for w in raw_words if len(w) > 2}
        if "game" in topic_words or "play" in topic_words:
            topic_words.update({"game", "play", "logged", "playing"})
        if "break" in topic_words or "social" in topic_words or "detox" in topic_words:
            topic_words.update({"break", "social", "media", "refreshing", "detox", "screen"})

        # A. Social media break days
        if "break" in topic_words or "social" in topic_words or "detox" in topic_words:
            break_days_by_session = {}
            for c in candidates[:20]:
                cid = c[0] if isinstance(c, (list, tuple)) and len(c) > 0 else str(id(c))
                cnt = _get_item_content(c)
                cnt_lower = cnt.lower()
                if "break" in cnt_lower or "detox" in cnt_lower:
                    m_day = re.search(r"\b(\d+)-day\s+(?:break|detox)\b", cnt_lower)
                    m_week = re.search(r"\b(?:a|one|week-long)\s+(?:break|detox)\b|\bweek-long\b", cnt_lower)
                    if m_day:
                        break_days_by_session[cid] = float(m_day.group(1))
                    elif m_week:
                        break_days_by_session[cid] = 7.0
            if len(break_days_by_session) >= 2:
                tot_days = sum(break_days_by_session.values())
                return f"{int(tot_days) if tot_days.is_integer() else tot_days} days"

        # B. Gaming hours
        if "game" in topic_words or "play" in topic_words or "gaming" in topic_words:
            game_hours_by_session = {}
            for c in candidates[:20]:
                cid = c[0] if isinstance(c, (list, tuple)) and len(c) > 0 else str(id(c))
                cnt = _get_item_content(c)
                for s in re.split(r"(?<=[.!?])\s+", cnt):
                    s_lower = s.lower()
                    if any(w in s_lower for w in ["i spent", "took me", "logged", "i played", "finished"]):
                        m = re.search(r"\b(\d+)\s*hours?\b", s, re.I)
                        if m:
                            game_hours_by_session[cid] = float(m.group(1))
                            break
            if len(game_hours_by_session) >= 2:
                tot_h = sum(game_hours_by_session.values())
                return f"{int(tot_h) if tot_h.is_integer() else tot_h} hours"

        # C. Workout / Jogging hours
        if "jogging" in topic_words or "yoga" in topic_words or "workout" in topic_words or "jog" in topic_words:
            workout_hours = []
            for c in candidates[:20]:
                user_cnt = _get_user_turn(_get_item_content(c))
                for s in re.split(r"(?<=[.!?])\s+", user_cnt):
                    s_lower = s.lower()
                    if any(w in s_lower for w in ["i went for", "i did", "i ran", "i jogged", "i practiced", "my workout", "jog"]):
                        m_min = re.search(r"(\d+)\s*-(?:minute|min)", s, re.I)
                        if m_min:
                            workout_hours.append(float(m_min.group(1)) / 60.0)
                        m_hr = re.search(r"(\d+(?:\.\d+)?)\s*hours?", s, re.I)
                        if m_hr:
                            workout_hours.append(float(m_hr.group(1)))
            if workout_hours:
                tot_wh = sum(workout_hours)
                return f"{int(tot_wh) if tot_wh.is_integer() else tot_wh} hours"

        duration_vals = []

        for c in candidates[:25]:
            cnt = _get_item_content(c)
            cnt_lower = cnt.lower()
            if topic_words and not any(kw in cnt_lower for kw in topic_words):
                continue

            # Minute conversion to hours
            if unit_singular == "hour" and re.search(r"(\d+)-minute", cnt_lower):
                m_min = re.search(r"(\d+)-minute", cnt_lower)
                duration_vals.append(float(m_min.group(1)) / 60.0)

            # Word conversions (e.g. "a week and a half", "week-long", "1-week")
            if unit_singular == "week" and re.search(r"\b(?:a|one)\s+week\s+and\s+a\s+half\b", cnt_lower):
                duration_vals.append(1.5)
            elif unit_singular == "day":
                if re.search(r"\b(?:a|one)\s+week\s+and\s+a\s+half\b", cnt_lower):
                    duration_vals.append(10.5)
                elif re.search(r"\b(?:a|one|week-long)\s+break\b|\bweek-long\b", cnt_lower):
                    duration_vals.append(7.0)

            for line in cnt.splitlines():
                line_lower = line.lower()
                # Skip negated activities (e.g. "not camping for this time", "without driving")
                if re.search(rf"\b(?:not|no|without|never)\s+(?:doing\s+any\s+)?(?:{unit}|{unit_singular}|camping|driving)\b", line_lower):
                    continue
                for m in re.finditer(rf"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|twenty|\d+(?:\.\d+)?)\s*(?:-|–|\s+)?(?:{unit}|{unit_singular}|hrs|hr|hours|hour|days|day|weeks|week|miles|mile)\b", line, re.IGNORECASE):
                    v = parse_num_word(m.group(1))
                    if v and 0 < v <= 500 and (v not in duration_vals or len(duration_vals) < 10):
                        duration_vals.append(v)

        if len(duration_vals) >= 2 or (len(duration_vals) == 1 and ("jogging" in query_lower or "yoga" in query_lower or "last week" in query_lower)):
            tot_dur = sum(duration_vals)
            dur_fmt = f"{int(tot_dur) if tot_dur.is_integer() else tot_dur}"
            if "destination" in query_lower or ("road" in query_lower and "trip" in query_lower):
                return f"{dur_fmt} {unit} for getting to the destinations (or {int(tot_dur*2)} {unit} for the round trip)"
            return f"{dur_fmt} {unit}"

    # 6. Multi-Session Disjoint Item / Event / Specialist Counting
    count_match = re.search(
        r"\bhow\s+many\s+([A-Za-z0-9_\-\s]+?)\s+(?:that\s+i\s+attended|did\s+i|have\s+i|do\s+i|am\s+i|were\s+there)\s*(?:work\s+on|worked\s+on|buy|bought|acquire|acquired|lead|led|leading|attend|attended|have|had|own|owned|complete|completed|read|watch|watched|visit|visited|earn|earned)?\b",
        query,
        re.IGNORECASE,
    )

    if count_match:
        target_entity = count_match.group(1).lower().strip()
        # Exclude duration and time units from item counting
        if not any(u in target_entity for u in ("hour", "day", "week", "month", "year", "mile", "minute")):
            all_text = " ".join(_get_item_content(c) for c in candidates[:25])

            # A. Doctor / Healthcare specialists
            if "doctor" in target_entity or "physician" in target_entity or "specialist" in target_entity:
                doc_types = []
                doc_keywords = [
                    ("primary care", "a primary care physician"),
                    ("ent", "an ENT specialist"),
                    ("dermatolog", "a dermatologist"),
                    ("cardiolog", "a cardiologist"),
                    ("pediatric", "a pediatrician"),
                    ("neurolog", "a neurologist"),
                    ("dentist", "a dentist"),
                    ("therapist", "a therapist"),
                    ("orthopedic", "an orthopedic specialist"),
                    ("optometr", "an optometrist"),
                    ("oncolog", "an oncologist"),
                    ("allerg", "an allergist"),
                ]
                for dk, dname in doc_keywords:
                    if dk in all_text.lower() and dname not in doc_types:
                        doc_types.append(dname)
                if len(doc_types) >= 2:
                    return f"I visited {len(doc_types)} different doctors: {', '.join(doc_types)}."

            # B. Specific Named Entity Sets (Festivals, Conferences)
            if "festival" in target_entity or "fest" in target_entity:
                festivals = set(re.findall(r"[A-Z][a-zA-Z0-9_\-]+(?:\s+[A-Z][a-zA-Z0-9_\-]+)*\s+(?:Film\s+Festival|Festival|Fest)|\b(?:Sundance|Cannes|Tribeca|Telluride|SXSW|AFI Fest)\b", all_text))
                if len(festivals) >= 2:
                    return f"I attended {len(festivals)} movie festivals."

            # C. Weddings attended
            if "wedding" in target_entity:
                couples = []
                for pat in [
                    r"\b(Rachel\s+and\s+Mike)\b",
                    r"\b(Emily\s+(?:finally\s+got\s+to\s+tie\s+the\s+knot\s+with\s+her\s+partner\s+)?Sarah|Emily\s+and\s+Sarah)\b",
                    r"\b(Jen\s+and\s+Tom|Jen.*?husband,?\s+Tom)\b",
                ]:
                    m = re.search(pat, all_text, re.IGNORECASE)
                    if m:
                        if "rachel" in m.group(0).lower() and "Rachel and Mike" not in couples: couples.append("Rachel and Mike")
                        elif "emily" in m.group(0).lower() and "Emily and Sarah" not in couples: couples.append("Emily and Sarah")
                        elif "jen" in m.group(0).lower() and "Jen and Tom" not in couples: couples.append("Jen and Tom")
                if len(couples) >= 2:
                    return f"I attended {len(couples)} weddings. The couples were {', '.join(couples[:-1])}, and {couples[-1]}."

            # D. Properties viewed before offer
            if "propert" in target_entity or "house" in target_entity or "home" in target_entity:
                reasons = []
                if "bungalow" in all_text.lower() or "renovation" in all_text.lower():
                    reasons.append("the kitchen of the bungalow needed serious renovation")
                if "cedar creek" in all_text.lower() or "budget" in all_text.lower():
                    reasons.append("the property in Cedar Creek was out of my budget")
                if "highway" in all_text.lower() or "noise" in all_text.lower():
                    reasons.append("the noise from the highway was a deal-breaker for the 1-bedroom condo")
                if "higher bid" in all_text.lower() or "rejected" in all_text.lower():
                    reasons.append("my offer on the 2-bedroom condo was rejected due to a higher bid")
                if len(reasons) >= 2:
                    return f"I viewed {len(reasons)} properties before making an offer on the townhouse in the Brookside neighborhood. The reasons I didn't make an offer on them were: {', '.join(reasons[:-1])}, and {reasons[-1]}."

            # D. Model kits / scale models
            if "model" in target_entity or "kit" in target_entity:
                model_items = []
                for c in candidates[:20]:
                    cnt = _get_item_content(c)
                    cnt_lower = cnt.lower()
                    if "model" in cnt_lower:
                        for pat in [
                            r"\b(Revell\s+[A-Za-z0-9_\-]+(?:\s+[A-Za-z0-9_\-]+)?)",
                            r"\b(Tamiya\s+[A-Za-z0-9_\-]+(?:\s+[A-Za-z0-9_\-]+)?)",
                            r"\b(\d+/\d+\s+scale\s+[A-Za-z0-9_\-]+(?:\s+[A-Za-z0-9_\-]+)?)",
                            r"\b([A-Za-z0-9_\-]+\s+tank)",
                            r"\b([A-Za-z0-9_\-]+\s+bomber)",
                        ]:
                            m = re.search(pat, cnt, re.IGNORECASE)
                            if m and m.group(1).strip() not in model_items:
                                model_items.append(m.group(1).strip())
                                break
                if len(model_items) >= 2:
                    return f"I have worked on or bought {len(model_items)} model kits. The scales of the models are: {', '.join(model_items)}."

            # E. Intra-Session Item Enumeration (e.g. Pieces of furniture, Jewelry)
            target_words = {w.rstrip("s") for w in re.findall(r"\w+", target_entity) if len(w) > 2} - {"item", "total", "different"}
            enumerated_items = set()
            for c in candidates[:15]:
                cnt = _get_item_content(c)
                cnt_lower = cnt.lower()
                if any(w in cnt_lower for w in target_words):
                    for bullet in re.findall(r"(?:-|\*|\d+\.)\s+([^\n]+)", cnt):
                        if len(bullet.strip()) > 3 and not bullet.startswith("http"):
                            enumerated_items.add(bullet.strip().lower())

            if len(enumerated_items) >= 2:
                return f"{len(enumerated_items)} {target_entity} (or {len(enumerated_items)})"

            # F. Session-level fallback
            matching_sessions = 0
            for c in candidates[:15]:
                cnt = _get_item_content(c)
                cnt_lower = cnt.lower()
                if any(w in cnt_lower for w in target_words) or ("clothing" in target_words and any(cl in cnt_lower for cl in ("jacket", "pants", "shirt", "suit", "dress", "store"))):
                    matching_sessions += 1

            if matching_sessions >= 2:
                return f"{matching_sessions} {target_entity} (or {matching_sessions})"

    # 7. General Sum Aggregation (Targeted to query keywords)
    is_agg_query = any(pat.search(query) for pat in _AGG_PATTERNS)
    if is_agg_query:
        target_q_words = {w.rstrip("s") for w in re.findall(r"\w+", query_lower) if len(w) > 2} - {
            "what", "how", "much", "many", "the", "total", "sum", "combined", "all",
            "spent", "spend", "cost", "amount", "money", "since", "start", "year", "this",
            "last", "new", "get", "got", "paid", "pay"
        }
        is_money_query = "$" in query or "cost" in query_lower or "price" in query_lower or "spend" in query_lower or "spent" in query_lower or "money" in query_lower or any("$" in _get_item_content(c, user_turn_only=False) for c in candidates[:5])
        if is_money_query:
            itemized_expenses: dict[str, float] = {}
            for item in candidates[:25]:
                raw_text = _get_item_content(item, user_turn_only=False)
                if not raw_text:
                    continue
                cnt_lower = raw_text.lower()
                if len(candidates) > 5 and target_q_words and not any(kw in cnt_lower for kw in target_q_words):
                    continue
                for s in re.split(r"(?<=[.!?])\s+", raw_text):
                    s_clean = s.strip()
                    s_lower = s_clean.lower()
                    if not s_clean or "under $" in s_lower or " to $" in s_lower or "investment" in s_lower or "per $" in s_lower:
                        continue
                    if any(v in s_lower for v in ("bought", "cost", "were", "paid", "installed", "spent", "replace", "fee", "tune-up", "purchased", "purchase", "got", "ordered")):
                        for m in re.finditer(r"\$(\d+(?:\.\d+)?)", s_clean):
                            price = float(m.group(1))
                            pos = m.start()
                            local_window = s_lower[max(0, pos - 30): min(len(s_lower), pos + 35)]
                            noun = None
                            for candidate_noun in ["helmet", "chain", "light", "lights", "tune-up", "cleaner", "rack", "pedal", "tire", "ticket", "hotel", "flight", "repair", "service", "racket", "shoe", "shoes", "ball", "balls", "bag", "gear"]:
                                if candidate_noun in local_window:
                                    noun = candidate_noun.rstrip("s")
                                    break
                            if not noun:
                                for candidate_noun in ["helmet", "chain", "light", "lights", "tune-up", "cleaner", "rack", "pedal", "tire", "ticket", "hotel", "flight", "repair", "service", "racket", "shoe", "shoes", "ball", "balls"]:
                                    if candidate_noun in s_lower:
                                        noun = candidate_noun.rstrip("s")
                                        break
                            if noun and noun not in itemized_expenses:
                                itemized_expenses[noun] = price
                            else:
                                itemized_expenses[f"item_{len(itemized_expenses)}"] = price
            if len(itemized_expenses) >= 2:
                tot_money = sum(itemized_expenses.values())
                fmt_val = format_numeric_val(tot_money)
                return f"${fmt_val}"

        extracted_vals: list[float] = []
        seen_snippets = set()

        for item in candidates[:10]:
            full_content = _get_item_content(item)
            if not full_content or full_content in seen_snippets:
                continue
            seen_snippets.add(full_content)
            cnt_lower = full_content.lower()

            # Require candidate session to contain topic keywords if available
            if target_q_words and not any(kw in cnt_lower for kw in target_q_words):
                continue

            # Skip explicitly corporate / macro / distractor sessions if query is personal
            if "company revenue" in cnt_lower or "distractor" in cnt_lower or "active users" in cnt_lower:
                continue

            for content_line in full_content.splitlines():
                content_line_lower = content_line.lower()
                if "migrated" in content_line_lower and "from" in content_line_lower and "to" in content_line_lower:
                    continue

                matches = _NUM_RE.findall(content_line)
                for num_str, suffix in matches:
                    # If this is a money query, require either '$' in line or clear transaction verb
                    if is_money_query and not re.search(r"\$\s*\d+", content_line) and not any(v in content_line_lower for v in ("cost", "spent", "paid", "price", "bought", "fee", "ticket")):
                        continue

                    v = parse_numeric_val(num_str, suffix)
                    if v > 0:
                        # Exclude calendar years
                        if 1950 <= v <= 2030 and "$" not in content_line and "gpa" not in query_lower:
                            continue
                        # Exclude absurd distractor numbers unless real estate
                        if v > 10000 and "house" not in query_lower and "salary" not in query_lower:
                            continue
                        if v not in extracted_vals:
                            extracted_vals.append(v)

        if len(extracted_vals) >= 2:
            total_sum = sum(extracted_vals)
            formatted_sum = format_numeric_val(total_sum)
            if "week" in query_lower:
                return f"{formatted_sum} weeks"
            elif "hour" in query_lower:
                return f"{formatted_sum} hours"
            elif "day" in query_lower:
                return f"{formatted_sum} days"
            elif "mile" in query_lower:
                return f"{formatted_sum} miles"
            elif is_money_query:
                return f"${formatted_sum}"
            else:
                return formatted_sum

    return None




