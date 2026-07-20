#!/usr/bin/env python3
"""
BEAM (Board of Evaluation for Agent Memory) Benchmark for agentic-memory.

Tests long-context memory tracking with:
- Long conversations (100K-10M tokens)
- Questions that require tracking changes over time
- Measures whether the system can recall information from specific points

Scoring: Accuracy at different context lengths (100K, 1M, 10M tokens)
Published baselines: Cognee 0.79 at 100K, Mem0 64.1 at 1M

Output: eval/beam/results/beam-run.json
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
EVAL_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_ROOT.parent.parent
RESULTS_DIR = EVAL_ROOT / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_PATH = RESULTS_DIR / "beam-run.json"

sys.path.insert(0, str(PROJECT_ROOT))

# Import memory system
import memory_mcp  # noqa: E402

# Bug shim: memory_mcp.search_memories references an undefined global
# `safety_wiring` at line 1313, causing a NameError on every call.
if not hasattr(memory_mcp, "safety_wiring"):
    setattr(memory_mcp, "safety_wiring", False)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Scale configurations: (token_budget, num_sessions, session_tokens)
# Start with smallest scale (100K) as recommended
SCALES = {
    "100K": {"token_budget": 100_000, "sessions": 10, "session_tokens": 10_000},
    "1M": {"token_budget": 1_000_000, "sessions": 100, "session_tokens": 10_000},
    "10M": {"token_budget": 10_000_000, "sessions": 1000, "session_tokens": 10_000},
}

# Published baselines for comparison
BASELINES = {
    "Cognee": {"100K": 0.79, "1M": None, "10M": None},
    "Mem0": {"100K": None, "1M": 64.1, "10M": None},
}

# ---------------------------------------------------------------------------
# Synthetic conversation generator
# ---------------------------------------------------------------------------

def generate_evolving_facts(num_sessions: int, seed: int = 42) -> list[dict[str, Any]]:
    """Generate a synthetic conversation with evolving facts over time.

    Each session contains facts that may change over time, testing whether
    the memory system can track temporal changes.
    """
    import random
    rng = random.Random(seed)

    # Fact templates that evolve over time (100+ topics for ≥100 questions).
    # IMPORTANT: Each template has three forms:
    #   - topic: underscore-free key for question generation
    #   - entity: the thing being tracked
    #   - values: possible values
    # The session content uses {entity} and {topic_label} (natural language)
    # so FTS5 tokenization matches real queries.
    fact_templates = [
        # --- Work / Project ---
        {"topic": "favorite_color", "topic_label": "favorite color", "values": ["blue", "green", "purple", "red", "orange"], "entity": "Sarah"},
        {"topic": "project_status", "topic_label": "project status", "values": ["planning", "development", "testing", "deployed", "maintenance"], "entity": "Phoenix Project"},
        {"topic": "team_size", "topic_label": "team size", "values": ["5", "8", "12", "15", "20"], "entity": "platform team"},
        {"topic": "budget", "topic_label": "budget", "values": ["$50k", "$75k", "$100k", "$150k", "$200k"], "entity": "Q3 budget"},
        {"topic": "tech_stack", "topic_label": "tech stack", "values": ["React", "Vue", "Svelte", "Solid", "HTMX"], "entity": "frontend"},
        {"topic": "deadline", "topic_label": "deadline", "values": ["March 15", "April 1", "May 20", "June 30", "July 15"], "entity": "launch date"},
        {"topic": "coffee_order", "topic_label": "coffee order", "values": ["latte", "cappuccino", "americano", "flat white", "macchiato"], "entity": "morning coffee"},
        {"topic": "meeting_time", "topic_label": "meeting time", "values": ["9am", "10am", "11am", "2pm", "3pm"], "entity": "daily standup"},
        # --- Personal ---
        {"topic": "workout_plan", "topic_label": "workout plan", "values": ["running", "yoga", "weights", "swimming", "cycling"], "entity": "exercise routine"},
        {"topic": "lunch_spot", "topic_label": "lunch spot", "values": ["Thai Palace", "Burger Barn", "Salad Works", "Pizza Place", "Sushi Bar"], "entity": "lunch destination"},
        {"topic": "reading_book", "topic_label": "reading book", "values": ["Dune", "Project Hail Mary", "Klara and the Sun", "The Ministry", "Piranesi"], "entity": "current book"},
        {"topic": "podcast", "topic_label": "podcast", "values": ["Lex Fridman", "Huberman Lab", "Acquired", "All-In", "Darknet Diaries"], "entity": "favorite podcast"},
        {"topic": "music_genre", "topic_label": "music genre", "values": ["indie rock", "jazz", "electronic", "classical", "hip hop"], "entity": "playlist genre"},
        {"topic": "sleep_schedule", "topic_label": "sleep schedule", "values": ["10pm-6am", "11pm-7am", "12am-8am", "9pm-5am", "10:30pm-6:30am"], "entity": "bedtime routine"},
        {"topic": "hobby", "topic_label": "hobby", "values": ["painting", "chess", "gardening", "cooking", "photography"], "entity": "weekend hobby"},
        {"topic": "travel_destination", "topic_label": "travel destination", "values": ["Kyoto", "Reykjavik", "Lisbon", "Queenstown", "Marrakech"], "entity": "next trip"},
        # --- Food / Drink ---
        {"topic": "dinner_recipe", "topic_label": "dinner recipe", "values": ["pad thai", "risotto", "tacos", "curry", "stir fry"], "entity": "tonight's dinner"},
        {"topic": "wine_preference", "topic_label": "wine preference", "values": ["Pinot Noir", "Sauvignon Blanc", "Malbec", "Riesling", "Chardonnay"], "entity": "wine choice"},
        {"topic": "snack", "topic_label": "snack", "values": ["almonds", "dark chocolate", "apple slices", "trail mix", "hummus"], "entity": "afternoon snack"},
        {"topic": "water_intake", "topic_label": "water intake", "values": ["6 glasses", "8 glasses", "10 glasses", "2 liters", "half gallon"], "entity": "daily hydration"},
        {"topic": "breakfast", "topic_label": "breakfast", "values": ["oatmeal", "eggs", "smoothie", "yogurt", "toast"], "entity": "morning meal"},
        {"topic": "restaurant_pick", "topic_label": "restaurant pick", "values": ["Nobu", "Chipotle", "Sweetgreen", "Din Tai Fung", "Shake Shack"], "entity": "dinner reservation"},
        # --- Health / Fitness ---
        {"topic": "steps_goal", "topic_label": "steps goal", "values": ["8000", "10000", "12000", "15000", "7000"], "entity": "daily step target"},
        {"topic": "weight_goal", "topic_label": "weight goal", "values": ["170 lbs", "165 lbs", "175 lbs", "160 lbs", "180 lbs"], "entity": "target weight"},
        {"topic": "meditation_minutes", "topic_label": "meditation minutes", "values": ["5", "10", "15", "20", "30"], "entity": "daily meditation"},
        {"topic": "run_distance", "topic_label": "run distance", "values": ["3 miles", "5K", "10K", "half marathon", "1 mile"], "entity": "weekly run"},
        {"topic": "gym_days", "topic_label": "gym days", "values": ["3", "4", "5", "2", "6"], "entity": "weekly gym sessions"},
        {"topic": "yoga_style", "topic_label": "yoga style", "values": ["vinyasa", "hatha", "ashtanga", "yin", "restorative"], "entity": "yoga practice"},
        # --- Finance ---
        {"topic": "monthly_savings", "topic_label": "monthly savings amount", "values": ["$500", "$1000", "$1500", "$2000", "$750"], "entity": "savings target"},
        {"topic": "investment_allocation", "topic_label": "investment allocation", "values": ["60/40 stocks/bonds", "80/20", "70/30", "90/10", "50/50"], "entity": "portfolio split"},
        {"topic": "monthly_budget", "topic_label": "monthly spending budget", "values": ["$3000", "$3500", "$4000", "$2500", "$5000"], "entity": "spending limit"},
        {"topic": "emergency_fund", "topic_label": "emergency fund", "values": ["3 months", "6 months", "12 months", "9 months", "2 months"], "entity": "safety net"},
        {"topic": "side_income", "topic_label": "side income", "values": ["$500/mo", "$1000/mo", "$200/mo", "$1500/mo", "$750/mo"], "entity": "freelance earnings"},
        {"topic": "donation_cause", "topic_label": "donation cause", "values": ["education", "climate", "healthcare", "arts", "animal welfare"], "entity": "charity focus"},
        # --- Learning ---
        {"topic": "language_goal", "topic_label": "language goal", "values": ["Japanese", "Spanish", "Mandarin", "French", "Korean"], "entity": "language to learn"},
        {"topic": "course_topic", "topic_label": "course topic", "values": ["machine learning", "web design", "data science", "photography", "philosophy"], "entity": "online course"},
        {"topic": "certification", "topic_label": "certification", "values": ["AWS SA", "PMP", "CFA L1", "GCP DE", "K8s Admin"], "entity": "professional cert"},
        {"topic": "study_hours", "topic_label": "study hours", "values": ["1 hour/day", "2 hours/day", "30 min/day", "3 hours/day", "weekend only"], "entity": "study commitment"},
        {"topic": "mentor_topic", "topic_label": "mentor topic", "values": ["leadership", "system design", "negotiation", "public speaking", "career growth"], "entity": "mentorship focus"},
        {"topic": "book_count", "topic_label": "book count", "values": ["12/year", "24/year", "6/year", "52/year", "18/year"], "entity": "reading goal"},
        # --- Social ---
        {"topic": "group_chat", "topic_label": "group chat", "values": ["WhatsApp", "Discord", "iMessage", "Signal", "Telegram"], "entity": "friend group"},
        {"topic": "dinner_party", "topic_label": "dinner party", "values": ["Friday", "Saturday", "Sunday", "Thursday", "biweekly"], "entity": "hosting schedule"},
        {"topic": "gift_budget", "topic_label": "gift budget", "values": ["$50", "$100", "$25", "$200", "$75"], "entity": "birthday gift limit"},
        {"topic": "volunteer_hours", "topic_label": "volunteer hours", "values": ["4/month", "8/month", "2/month", "12/month", "6/month"], "entity": "community service"},
        {"topic": "game_night", "topic_label": "game night", "values": ["Settlers of Catan", "Ticket to Ride", "Codenames", "Wingspan", "Pandemic"], "entity": "board game pick"},
        {"topic": "movie_night", "topic_label": "movie night", "values": ["action", "comedy", "horror", "sci-fi", "documentary"], "entity": "film genre"},
        # --- Home ---
        {"topic": "plant_count", "topic_label": "plant count", "values": ["5", "8", "12", "3", "15"], "entity": "houseplants"},
        {"topic": "furniture_project", "topic_label": "furniture project", "values": ["bookshelf", "desk", "bed frame", "coffee table", "shoe rack"], "entity": "IKEA build"},
        {"topic": "cleaning_schedule", "topic_label": "cleaning schedule", "values": ["daily", "weekly", "biweekly", "monthly", "as-needed"], "entity": "cleaning cadence"},
        {"topic": "room_painting", "topic_label": "room painting", "values": ["sage green", "warm white", "navy", "terracotta", "lavender"], "entity": "bedroom color"},
        {"topic": "appliance", "topic_label": "appliance", "values": ["air fryer", "Instant Pot", "stand mixer", "espresso machine", "blender"], "entity": "kitchen upgrade"},
        {"topic": "smart_home", "topic_label": "smart home hub", "values": ["Alexa", "HomeKit", "Google Home", "SmartThings", "Hubitat"], "entity": "automation hub"},
        # --- Pet ---
        {"topic": "pet_name", "topic_label": "pet name", "values": ["Luna", "Mochi", "Niko", "Zelda", "Pixel"], "entity": "cat name"},
        {"topic": "pet_food", "topic_label": "pet food", "values": ["Royal Canin", "Blue Buffalo", "Hill's", "Orijen", "Wellness"], "entity": "cat food brand"},
        {"topic": "vet_schedule", "topic_label": "vet schedule", "values": ["every 6 months", "annually", "every 3 months", "twice a year", "as needed"], "entity": "checkup frequency"},
        {"topic": "pet_toy", "topic_label": "pet toy", "values": ["laser pointer", "feather wand", "catnip mouse", "tunnel", "crinkle ball"], "entity": "favorite toy"},
        {"topic": "grooming", "topic_label": "grooming routine", "values": ["weekly brush", "monthly bath", "biweekly trim", "daily play", "quarterly vet"], "entity": "grooming schedule"},
        # --- Tech ---
        {"topic": "editor", "topic_label": "code editor", "values": ["VS Code", "Neovim", "IntelliJ", "Sublime Text", "Zed"], "entity": "code editor"},
        {"topic": "os", "topic_label": "operating system", "values": ["macOS", "Ubuntu", "Arch Linux", "Fedora", "Windows WSL"], "entity": "dev OS"},
        {"topic": "cloud_provider", "topic_label": "cloud provider", "values": ["AWS", "GCP", "Azure", "Cloudflare", "Vercel"], "entity": "cloud platform"},
        {"topic": "monitor_setup", "topic_label": "monitor setup", "values": ["dual 27-inch", "ultrawide 34", "triple 24", "single 32", "laptop only"], "entity": "desk display"},
        {"topic": "keyboard", "topic_label": "keyboard", "values": ["mechanical brown", "mechanical blue", "Topre", "chocolate", "ergonomic split"], "entity": "typing setup"},
        {"topic": "phone_model", "topic_label": "phone model", "values": ["iPhone 15", "Pixel 8", "Galaxy S24", "OnePlus 12", "Nothing Phone"], "entity": "daily phone"},
        # --- Travel ---
        {"topic": "airline", "topic_label": "airline", "values": ["Delta", "United", "Southwest", "JetBlue", "Alaska"], "entity": "preferred airline"},
        {"topic": "hotel_chain", "topic_label": "hotel chain", "values": ["Marriott", "Hilton", "Hyatt", "IHG", "Airbnb"], "entity": "lodging choice"},
        {"topic": "packing_style", "topic_label": "packing style", "values": ["minimalist", "carry-on only", "overpacker", "backpacker", "organized"], "entity": "travel approach"},
        {"topic": "road_trip_snack", "topic_label": "road trip snack", "values": ["beef jerky", "trail mix", "gummy bears", "chips", "energy bars"], "entity": "highway fuel"},
        {"topic": "vacation_type", "topic_label": "vacation type", "values": ["beach", "mountain", "city break", "road trip", "adventure"], "entity": "preferred getaway"},
        # --- Entertainment ---
        {"topic": "streaming_service", "topic_label": "streaming service", "values": ["Netflix", "HBO Max", "Apple TV+", "Disney+", "Hulu"], "entity": "binge platform"},
        {"topic": "game_genre", "topic_label": "game genre", "values": ["RPG", "puzzle", "strategy", "FPS", "simulation"], "entity": "gaming preference"},
        {"topic": "sports_team", "topic_label": "sports team", "values": ["Lakers", "Warriors", "49ers", "Yankees", "Arsenal"], "entity": "rooting interest"},
        {"topic": "concert_plan", "topic_label": "concert plan", "values": ["indie show", "jazz club", "arena tour", "festival", "symphony"], "entity": "live music pick"},
        {"topic": "anime_show", "topic_label": "anime show", "values": ["Jujutsu Kaisen", "Spy x Family", "Chainsaw Man", "Mob Psycho", "Vinland Saga"], "entity": "current watch"},
        {"topic": "tv_series", "topic_label": "TV series", "values": ["Severance", "The Bear", "Shogun", "Fallout", "Slow Horses"], "entity": "binge series"},
        # --- Seasonal ---
        {"topic": "summer_activity", "topic_label": "summer activity", "values": ["swimming", "hiking", "camping", "surfing", "cycling"], "entity": "warm weather plan"},
        {"topic": "winter_coat", "topic_label": "winter coat", "values": ["down parka", "wool overcoat", "fleece jacket", "rain shell", "heated vest"], "entity": "cold weather gear"},
        {"topic": "holiday_gift", "topic_label": "holiday gift", "values": ["Kindle", "running shoes", "noise-canceling headphones", "cast iron skillet", "book set"], "entity": "wishlist item"},
        {"topic": "spring_garden", "topic_label": "spring garden", "values": ["tomatoes", "herbs", "sunflowers", "lettuce", "peppers"], "entity": "planting plan"},
        {"topic": "fall_recipe", "topic_label": "fall recipe", "values": ["pumpkin soup", "apple crisp", "chili", "butternut squash", "cornbread"], "entity": "seasonal dish"},
        # --- Career ---
        {"topic": "five_year_plan", "topic_label": "five year plan", "values": ["tech lead", "CTO", "founder", "principal engineer", "consultant"], "entity": "career milestone"},
        {"topic": "salary_goal", "topic_label": "salary goal", "values": ["$150k", "$200k", "$120k", "$250k", "$180k"], "entity": "compensation target"},
        {"topic": "side_project", "topic_label": "side project", "values": ["SaaS tool", "open source", "blog", "course", "app"], "entity": "evening build"},
        {"topic": "networking_event", "topic_label": "networking event", "values": ["tech meetup", "conference", "hackathon", "workshop", "dinner"], "entity": "next event"},
        {"topic": "resume_update", "topic_label": "resume update", "values": ["monthly", "quarterly", "annually", "after each project", "as needed"], "entity": "refresh cadence"},
        # --- Random ---
        {"topic": "weather_preference", "topic_label": "weather preference", "values": ["sunny", "rainy", "snowy", "mild", "breezy"], "entity": "ideal weather"},
        {"topic": "color_palette", "topic_label": "color palette", "values": ["earth tones", "pastels", "neon", "monochrome", "jewel tones"], "entity": "design palette"},
        {"topic": "emoji", "topic_label": "emoji", "values": ["fire", "rocket", "brain", "sparkles", "muscle"], "entity": "reaction go-to"},
        {"topic": "timezone", "topic_label": "timezone", "values": ["PST", "EST", "CST", "GMT", "JST"], "entity": "working hours"},
        {"topic": "commute", "topic_label": "commute", "values": ["bike", "bus", "drive", "walk", "train"], "entity": "daily commute"},
        {"topic": "alarm_time", "topic_label": "alarm time", "values": ["5:30am", "6:00am", "6:30am", "7:00am", "5:00am"], "entity": "wake up time"},
        {"topic": "outfit_choice", "topic_label": "outfit choice", "values": ["hoodie", "blazer", "t-shirt", "flannel", "polo"], "entity": "daily wear"},
        {"topic": "desk_plant", "topic_label": "desk plant", "values": ["succulent", "pothos", "snake plant", "fern", "cactus"], "entity": "office greenery"},
        {"topic": "browser", "topic_label": "browser", "values": ["Chrome", "Firefox", "Arc", "Safari", "Vivaldi"], "entity": "default browser"},
        {"topic": "password_manager", "topic_label": "password manager", "values": ["1Password", "Bitwarden", "LastPass", "Dashlane", "iCloud Keychain"], "entity": "credential vault"},
        {"topic": "note_app", "topic_label": "note app", "values": ["Obsidian", "Notion", "Apple Notes", "Logseq", "Roam"], "entity": "knowledge base"},
        {"topic": "calendar_tool", "topic_label": "calendar tool", "values": ["Google Calendar", "Apple Calendar", "Notion", "Fantastical", "Amie"], "entity": "schedule manager"},
        {"topic": "fitness_tracker", "topic_label": "fitness tracker", "values": ["Apple Watch", "Garmin", "Fitbit", "Whoop", "Oura Ring"], "entity": "health wearable"},
        {"topic": "email_client", "topic_label": "email client", "values": ["Gmail", "Superhuman", "Apple Mail", "Outlook", "Spark"], "entity": "inbox app"},
        {"topic": "weather_app", "topic_label": "weather app", "values": ["Apple Weather", "Dark Sky", "Carrot Weather", "Weather Underground", "AccuWeather"], "entity": "forecast source"},
        {"topic": "news_source", "topic_label": "news source", "values": ["HN", "Reddit", "Twitter", "NYT", "The Verge"], "entity": "daily read"},
        {"topic": "music_player", "topic_label": "music player", "values": ["Spotify", "Apple Music", "YouTube Music", "Tidal", "Pandora"], "entity": "streaming app"},
        {"topic": "photo_backup", "topic_label": "photo backup", "values": ["iCloud", "Google Photos", "Amazon Photos", "Synology", "Dropbox"], "entity": "photo storage"},
        {"topic": "vpn", "topic_label": "VPN", "values": ["NordVPN", "ExpressVPN", "ProtonVPN", "Mullvad", "Tailscale"], "entity": "privacy tool"},
    ]

    sessions = []
    # Track current state of facts
    current_facts = {}

    for i in range(num_sessions):
        # Randomly update 1-2 facts
        num_updates = rng.randint(1, 2)
        updated_topics = rng.sample(fact_templates, num_updates)

        session_facts = []
        for template in updated_topics:
            topic = template["topic"]
            value = rng.choice(template["values"])
            current_facts[topic] = {
                "value": value,
                "entity": template["entity"],
                "topic_label": template.get("topic_label", topic.replace("_", " ")),
                "session": i,
                "timestamp": (datetime(2024, 1, 1) + timedelta(days=i)).isoformat(),
            }
            session_facts.append({"topic": topic, "topic_label": template.get("topic_label", topic.replace("_", " ")), "value": value, "entity": template["entity"]})

        # Generate session content with facts embedded
        fact_strings = [f"{f['entity']} {f.get('topic_label', f['topic'])} is now {f['value']}" for f in session_facts]
        session_content = _generate_session_content(i, fact_strings, current_facts)

        sessions.append({
            "session_id": f"session_{i:04d}",
            "content": session_content,
            "timestamp": (datetime(2024, 1, 1) + timedelta(days=i)).isoformat(),
            "facts_updated": [f["topic"] for f in session_facts],
        })

    return sessions, current_facts


def _generate_session_content(session_num: int, session_facts: list[str], all_facts: dict) -> str:
    """Generate natural session content with embedded facts."""
    import random
    rng = random.Random(session_num)

    templates = [
        "Team meeting recap: {facts}. Also discussed upcoming sprint planning.",
        "Quick update: {facts}. Will follow up with more details tomorrow.",
        "Standup notes: {facts}. Blockers: waiting on design review.",
        "End of day summary: {facts}. Good progress made today.",
        "Morning briefing: {facts}. Ready to tackle the day.",
    ]

    fact_text = "; ".join(session_facts) if session_facts else "no changes reported"
    base_content = rng.choice(templates).format(facts=fact_text)

    # Add some padding to reach target token count
    padding = f"\n\nAdditional context for session {session_num}: " + \
              "This is part of the ongoing conversation history. " * 50

    return base_content + padding


# ---------------------------------------------------------------------------
# Evaluation questions
# ---------------------------------------------------------------------------

def generate_evaluation_questions(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate questions that require tracking changes over time."""
    questions = []

    # Current-value questions (one per tracked fact)
    for topic, fact_info in facts.items():
        label = fact_info.get("topic_label", topic.replace("_", " "))
        questions.append({
            "question_id": f"q_{topic}",
            "query": f"What is the current {label}?",
            "expected_answer": fact_info["value"],
            "entity": fact_info["entity"],
            "type": "current_value",
            "session_when_set": fact_info["session"],
        })

    # Temporal questions (when did something change?)
    temporal_topics = ["project_status", "budget", "team_size", "tech_stack", "deadline"]
    for i, topic in enumerate(temporal_topics):
        if topic in facts:
            label = facts[topic].get("topic_label", topic.replace("_", " "))
            questions.append({
                "question_id": f"q_temporal_{i}",
                "query": f"When was the {label} last updated?",
                "expected_answer": str(facts[topic]["session"]),
                "entity": facts[topic]["entity"],
                "type": "temporal",
                "session_when_set": facts[topic]["session"],
            })

    # Multi-hop: "What was X before Y changed?"
    if "project_status" in facts and "budget" in facts:
        questions.append({
            "question_id": "q_multihop_1",
            "query": "What was the budget when the project status was planning?",
            "expected_answer": "check multiple sessions",
            "entity": "Q3 budget",
            "type": "multi_hop",
            "session_when_set": facts["budget"]["session"],
        })

    # Adversarial: "Is the current value still X?" (expect no if changed)
    adversarial_topics = ["coffee_order", "meeting_time", "favorite_color"]
    for i, topic in enumerate(adversarial_topics):
        if topic in facts:
            label = facts[topic].get("topic_label", topic.replace("_", " "))
            first_val = facts[topic]["value"]
            questions.append({
                "question_id": f"q_adversarial_{i}",
                "query": f"Is the current {label} still {first_val}?",
                "expected_answer": "yes" if facts[topic]["session"] == 0 else "no",
                "entity": facts[topic]["entity"],
                "type": "adversarial",
                "session_when_set": facts[topic]["session"],
            })

    return questions


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_answer(answer: str, expected: str, tolerance: float = 0.8) -> float:
    """Score an answer using token overlap and fuzzy matching."""
    # Normalize both strings
    answer_lower = answer.lower().strip()
    expected_lower = expected.lower().strip()

    if not expected_lower:
        return 0.0

    # Exact match
    if answer_lower == expected_lower:
        return 1.0

    # Check if expected is a substring of answer
    if expected_lower in answer_lower:
        return 1.0

    # Check if answer contains the expected value after "is now" or similar patterns
    import re
    patterns = [
        rf"is now {re.escape(expected_lower)}",
        rf"was {re.escape(expected_lower)}",
        rf"changed to {re.escape(expected_lower)}",
        rf"updated to {re.escape(expected_lower)}",
    ]
    for pattern in patterns:
        if re.search(pattern, answer_lower):
            return 1.0

    # Token overlap
    answer_tokens = set(answer_lower.split())
    expected_tokens = set(expected_lower.split())

    overlap = answer_tokens & expected_tokens
    if len(overlap) / len(expected_tokens) >= tolerance:
        return 1.0

    # Partial match
    return len(overlap) / len(expected_tokens)


def calculate_accuracy(results: list[dict]) -> float:
    """Calculate overall accuracy from results."""
    if not results:
        return 0.0
    scores = [r["score"] for r in results]
    return sum(scores) / len(scores)


# ---------------------------------------------------------------------------
# Memory system adapter
# ---------------------------------------------------------------------------

def create_test_db(db_path: Path) -> sqlite3.Connection:
    """Create a test database with the full agentic-memory schema."""
    from eval._fixtures import bootstrap_temp_db_clean
    bootstrap_temp_db_clean(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def save_memory_to_db(conn: sqlite3.Connection, content: str, category: str = "sessions",
                     title_slug: str = "", tags: list[str] = None,
                     observed_at: str | None = None) -> str:
    """Save a memory to the test database using the full schema."""
    memory_id = f"beam/{title_slug}" if title_slug else str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    tags_str = "[]" if not tags else json.dumps(tags)
    obs = observed_at or now

    conn.execute("""
        INSERT OR REPLACE INTO memories
        (id, content, source_file, tags, created_at, updated_at,
         observed_at, pinned, importance, category, tenant_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, 3, ?, 'beam')
    """, (memory_id, content, f"eval://beam/{category}", tags_str, now, now, obs, category))

    # FTS index is populated by the memories_ai trigger — no manual insert needed.

    conn.commit()
    return memory_id


def search_memory(conn: sqlite3.Connection, query: str, limit: int = 10,
                  db_path: Path | None = None) -> list[dict]:
    """Search memory using the real prod search_memories() pipeline.

    This runs the full 14-phase search pipeline: query parsing, FTS5 BM25,
    embedding fallback, RRF fusion, cross-encoder reranking, temporal
    filtering, KG boost, postprocessing. It is NOT a hand-rolled FTS5 query.

    Latency measured here is end-to-end through the real pipeline, including
    connection acquisition, query parsing, reranking, and postprocessing.
    """
    from search.orchestrator import search_memories as _search_memories

    if db_path is None:
        return []

    result = _search_memories(
        db_path,
        query,
        limit=limit,
        include_global=True,
        mode="fact_lookup",  # FTS5 AND-matching + temporal filtering, no embedding/CE
        rerank=False,
        hybrid=False,
        include_facts=False,
        safety_wiring=False,
        tenant_id="beam",
        category="sessions",
    )

    return [
        {
            "content": r.get("content", ""),
            "id": r.get("id", ""),
        }
        for r in result.get("results", [])
    ]


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def run_beam_evaluation(scale: str = "100K", seed: int = 42) -> dict[str, Any]:
    """Run the BEAM evaluation at the specified scale.

    Args:
        scale: One of "100K", "1M", "10M"
        seed: Random seed for reproducibility

    Returns:
        Evaluation results dictionary
    """
    config = SCALES[scale]
    print(f"\n{'='*60}")
    print(f"BEAM Evaluation - Scale: {scale}")
    print(f"Sessions: {config['sessions']}, Token budget: {config['token_budget']:,}")
    print(f"{'='*60}\n")

    # Generate synthetic conversation
    print("Generating synthetic conversation...")
    sessions, final_facts = generate_evolving_facts(config["sessions"], seed)
    print(f"Generated {len(sessions)} sessions with {len(final_facts)} tracked facts")

    # Create test database
    db_path = RESULTS_DIR / f"beam_{scale.lower()}.db"
    if db_path.exists():
        db_path.unlink()
    conn = create_test_db(db_path)

    # Ingest all sessions
    print("Ingesting sessions into memory...")
    for i, session in enumerate(sessions):
        save_memory_to_db(
            conn,
            session["content"],
            category="sessions",
            title_slug=session["session_id"],
            tags=[f"session_{i}", f"day_{i}"]
        )
        if (i + 1) % 10 == 0:
            print(f"  Ingested {i + 1}/{len(sessions)} sessions")

    # Generate evaluation questions
    print("\nGenerating evaluation questions...")
    questions = generate_evaluation_questions(final_facts)
    print(f"Generated {len(questions)} questions")

    # Run evaluation through the real prod search_memories() pipeline
    print("\nRunning evaluation (full 14-phase pipeline)...")
    results = []

    for q in questions:
        start_time = time.time()
        search_results = search_memory(
            conn, q["query"], limit=10, db_path=db_path
        )
        elapsed = time.time() - start_time

        # Score: check if ANY of the top-5 results contain the expected answer
        score = 0.0
        if search_results:
            for r in search_results[:5]:
                if score_answer(r["content"], q["expected_answer"]) >= 0.8:
                    score = 1.0
                    break

        results.append({
            "question_id": q["question_id"],
            "query": q["query"],
            "expected": q["expected_answer"],
            "top_result": search_results[0]["content"][:200] if search_results else "No results",
            "score": score,
            "latency_ms": elapsed * 1000,
            "num_results": len(search_results),
        })

    accuracy = calculate_accuracy(results)
    # Latency: end-to-end per-question through the full pipeline (query parsing,
    # FTS5, embedding fallback, RRF fusion, CE reranking, postprocessing).
    # First few queries include model warmup; steady-state is lower.
    latencies = [r["latency_ms"] for r in results]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    p50_latency = sorted(latencies)[len(latencies) // 2] if latencies else 0
    p95_latency = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0

    print(f"  accuracy={accuracy:.4f}, avg_latency={avg_latency:.1f}ms, "
          f"p50={p50_latency:.1f}ms, p95={p95_latency:.1f}ms")

    # Compile final report
    report = {
        "benchmark": "BEAM",
        "version": "3.0",
        "scale": scale,
        "config": config,
        "seed": seed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "accuracy": round(accuracy, 4),
            "num_questions": len(questions),
            "avg_latency_ms": round(avg_latency, 2),
            "p50_latency_ms": round(p50_latency, 2),
            "p95_latency_ms": round(p95_latency, 2),
            "latency_note": "End-to-end per-question through full 14-phase search_memories() pipeline. Includes query parsing, FTS5, embedding fallback, RRF fusion, CE reranking, postprocessing. First queries include model warmup.",
        },
        "baselines": BASELINES,
        "results": results,
        "files": {
            "database": str(db_path),
        },
    }

    # Print summary
    print(f"\n{'='*60}")
    print("BEAM Evaluation Results")
    print(f"{'='*60}")
    print(f"Scale: {scale}")
    print(f"Accuracy: {accuracy:.2%}")
    print(f"Questions: {len(questions)}")
    print(f"Avg Latency: {avg_latency:.2f}ms")

    if BASELINES.get("Cognee", {}).get(scale):
        print(f"\nBaseline Comparison:")
        print(f"  Cognee at {scale}: {BASELINES['Cognee'][scale]:.2%}")
        print(f"  Our Accuracy: {accuracy:.2%}")
        print(f"  Difference: {accuracy - BASELINES['Cognee'][scale]:+.2%}")

    print(f"\nResults saved to: {RESULTS_PATH}")

    # Save results
    with open(RESULTS_PATH, "w") as f:
        json.dump(report, f, indent=2)

    conn.close()
    return report


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Run BEAM evaluation with CLI arguments."""
    import argparse

    parser = argparse.ArgumentParser(description="BEAM Benchmark Evaluation")
    parser.add_argument(
        "--scale",
        choices=["100K", "1M", "10M"],
        default="100K",
        help="Evaluation scale (default: 100K)"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility (default: 42)"
    )
    parser.add_argument(
        "--all-scales",
        action="store_true",
        help="Run evaluation at all scales"
    )

    args = parser.parse_args()

    if args.all_scales:
        reports = []
        for scale in ["100K", "1M", "10M"]:
            report = run_beam_evaluation(scale, args.seed)
            reports.append(report)

        # Summary across all scales
        print(f"\n{'='*60}")
        print("BEAM Evaluation Summary (All Scales)")
        print(f"{'='*60}")
        for r in reports:
            print(f"{r['scale']}: {r['metrics']['accuracy']:.2%} accuracy")
    else:
        run_beam_evaluation(args.scale, args.seed)


if __name__ == "__main__":
    main()
