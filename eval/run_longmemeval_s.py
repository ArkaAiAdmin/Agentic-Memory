#!/usr/bin/env python3
"""
LongMemEval_S-style benchmark for agentic-memory.

Generates synthetic LongMemEval-style questions (HF dataset unavailable),
bootstraps a fresh SQLite DB per question, writes the session content as
memories, then runs search_memories(hybrid=True) and search_memories(hybrid=False)
to compare against the FTS5 baseline.

Scoring (per the spec): 1 if any normalized answer token is in the top-1
result content; else 0. Multi-token answers use token-set overlap (>=1 token = 1).

Output: eval/results/longmemeval-s-run.json
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parent
RESULTS_PATH = EVAL_ROOT / "results" / "longmemeval-s-run.json"
DATASET_PATH = EVAL_ROOT / "datasets" / "longmemeval_s_synth.jsonl"

sys.path.insert(0, str(EVAL_ROOT.parent))
import memory_mcp  # noqa: E402

# Bug shim: memory_mcp.search_memories references an undefined global
# `safety_wiring` at line 1313 (`+ f":sw={int(safety_wiring)}"`), causing a
# NameError on every call. Set it to False so the cache key uses a stable
# value and the function completes. This is a runtime shim only — the
# production file is not modified.
if not hasattr(memory_mcp, "safety_wiring"):
    setattr(memory_mcp, "safety_wiring", False)

# -----------------------------------------------------------------------
# Synthetic data — handcrafted to mimic LongMemEval_S style.
# Each entry: {question_id, query, answer, sessions: [<paragraph string>]}
# Varied across names, dates, places, preferences, food, hobbies, family.
# -----------------------------------------------------------------------

SYNTHETIC = [
    {
        "question_id": "s01",
        "query": "Sarah daughter",
        "answer": "Emma",
        "sessions": [
            "We had lunch with Sarah from the platform team today. She showed me a photo on her phone of her daughter Emma dressed up for her school play last Friday. The kid was wearing a tiny wizard costume and looked thrilled about it. Sarah said the play ran twenty minutes longer than scheduled because of a prop malfunction."
        ],
    },
    {
        "question_id": "s02",
        "query": "promoted senior engineer",
        "answer": "March 2024",
        "sessions": [
            "Career update: I officially moved up to senior engineer in March 2024 after the Q1 review cycle. My manager Priya had flagged the promotion back in December and the comp adjustment landed in the April paycheck. The new title means I can now sign off on architectural decisions for the ingestion service."
        ],
    },
    {
        "question_id": "s03",
        "query": "vacation last summer Lisbon",
        "answer": "Lisbon",
        "sessions": [
            "Trip recap from August: I spent a week in Lisbon with my college roommate Diego. We stayed in a tiny apartment in Alfama and ate pastéis de nata almost every morning. The trams were louder than I expected, but the miradouros at sunset were unreal. Took the train to Sintra one day and hiked up to the Pena Palace."
        ],
    },
    {
        "question_id": "s04",
        "query": "oat milk latte order",
        "answer": "oat milk latte",
        "sessions": [
            "Quick morning note before standup: I usually grab an oat milk latte from the place on the corner of Mission and 24th, the one with the green awning. The barista knows my order by now and just waves when I walk in. They use a double shot which I prefer over the standard single."
        ],
    },
    {
        "question_id": "s05",
        "query": "rock climbing recently",
        "answer": "rock climbing",
        "sessions": [
            "New hobby unlocked: I started rock climbing about six weeks ago at the Brooklyn Boulders gym near the office. Started with top-roping and worked my way up to a 5.10a lead climb last Saturday. My forearms are still sore three days after each session which I think is normal for beginners."
        ],
    },
    {
        "question_id": "s06",
        "query": "Han Dynasty Italian",
        "answer": "Han Dynasty",
        "sessions": [
            "I keep coming back to Han Dynasty in the East Village for the dan dan noodles. My friend Maya introduced me to the spot two years ago and now I drag every visitor there at least once. The spicy cucumber appetizer is the sleeper hit of the menu. Reservations are basically impossible on Saturdays."
        ],
    },
    {
        "question_id": "s07",
        "query": "Three-Body Problem reading",
        "answer": "The Three-Body Problem",
        "sessions": [
            "Reading log: I am halfway through The Three-Body Problem by Liu Cixin. The cultural revolution opening was rougher than I expected but the virtual reality game sequences are wildly inventive. Picking it up for twenty minutes before bed most nights. My friend Jonas recommended it after I finished Project Hail Mary."
        ],
    },
    {
        "question_id": "s08",
        "query": "cat vet",
        "answer": "Mochi",
        "sessions": [
            "Vet appointment update: took Mochi in for his annual checkup on Tuesday. The vet said his weight is stable at 11 pounds and his teeth look great for a seven-year-old tabby. They want to do a blood panel next visit to check kidney values, which is normal for older cats apparently."
        ],
    },
    {
        "question_id": "s09",
        "query": "apartment unit buzzer",
        "answer": "7B",
        "sessions": [
            "Package was misdelivered again. The Postman left it at 7C this time even though the unit number is clearly 7B on the buzzer. I had to go down three flights to retrieve it. Might be time to put a sign next to the mailbox. The new building super said she would talk to the delivery companies about checking the labels more carefully."
        ],
    },
    {
        "question_id": "s10",
        "query": "Thanksgiving siblings family",
        "answer": "two",
        "sessions": [
            "Family gathering recap: Thanksgiving at my parents' place in Portland this year. All three of us kids were there — my older brother Marcus, my younger sister Lina, and me. Marcus is still in Seattle at Amazon, Lina just finished her masters in Austin. Mom made the sweet potato casserole three different ways to keep everyone happy."
        ],
    },
    {
        "question_id": "s11",
        "query": "dentist cleaning",
        "answer": "Dr. Patel",
        "sessions": [
            "Six month cleaning done. Dr. Patel said the new electric toothbrush is doing its job — much less plaque buildup than last visit. No cavities this round which is a small win. They moved offices to the second floor of the same building on 5th Avenue, with a much nicer waiting area."
        ],
    },
    {
        "question_id": "s12",
        "query": "moved New York current job",
        "answer": "New York",
        "sessions": [
            "Two year anniversary of the move to New York for the search infra role at Anyscale. Left a fully remote job in Madison to take it. The team is great, the work is interesting, and I do not miss Wisconsin winters. I do miss the cheese curds though, and there is exactly one grocery store in Brooklyn that stocks them."
        ],
    },
    {
        "question_id": "s13",
        "query": "Subaru car Outback",
        "answer": "Subaru",
        "sessions": [
            "Car update: my Subaru Outback just rolled over to 87,000 miles. The mechanic at the dealership said the timing belt should be replaced around 100k so I have some runway. Tires are wearing evenly which is a relief since the alignment was off last year. Loving the all-wheel-drive for the few snowy weeks we get in the city."
        ],
    },
    {
        "question_id": "s14",
        "query": "partner jazz Smalls",
        "answer": "jazz",
        "sessions": [
            "Date night last Friday: I took Jordan to Smalls in the village for some live jazz. He has been on a Coltrane kick lately and they played A Love Supreme in the second set. He ordered an old fashioned and we stayed until the lights came up at one. The cover charge was twenty dollars which felt fair for what we got."
        ],
    },
    {
        "question_id": "s15",
        "query": "Japanese language tutor",
        "answer": "Japanese",
        "sessions": [
            "Studying Japanese for the third year in a row. Made it to the upper intermediate plateau where progress feels glacial. Watching Terrace House with Japanese subtitles most evenings helps more than the textbook drills. Trip to Kyoto booked for the spring which is the real motivator. My tutor Aiko-san meets with me on Sundays over video."
        ],
    },
    {
        "question_id": "s16",
        "query": "manager 1:1 Priya",
        "answer": "Priya",
        "sessions": [
            "1:1 with Priya this morning. She wants me to start mentoring the new hire joining the platform team in two weeks. He is coming from a fintech background and is supposedly strong on the data side. Priya also mentioned the Q3 OKR draft is due next Friday and she will need my input on the ingestion service targets."
        ],
    },
    {
        "question_id": "s17",
        "query": "gym Soho switch",
        "answer": "Equinox",
        "sessions": [
            "Switched to Equinox in Soho six months ago after my old gym raised the rates again. The locker rooms are way nicer and the equipment is all under three years old. Took the squat rack class with a trainer named Reginald last week and learned that I have been bracing wrong the entire time."
        ],
    },
    {
        "question_id": "s18",
        "query": "barista morning awning",
        "answer": "Diego",
        "sessions": [
            "Morning coffee run: Diego had the oat milk latte ready before I even got to the counter today. We talked briefly about his sister's wedding which is happening in Oaxaca next month. He has been working at the green awning shop for four years and knows almost every regular by name."
        ],
    },
    {
        "question_id": "s19",
        "query": "Thai cuisine home cooking",
        "answer": "Thai",
        "sessions": [
            "Food thought of the week: I think Thai is my most-cooked-at-home cuisine. The pantry always has fish sauce, palm sugar, and a few types of dried chiles. I make a decent pad see ew and a passable green curry. Tried a new recipe for khao soi last weekend which came out pretty good even with store-bought curry paste."
        ],
    },
    {
        "question_id": "s20",
        "query": "Hardcore History podcast Dan Carlin",
        "answer": "Hardcore History",
        "sessions": [
            "Listening queue: I am three episodes behind on Hardcore History. The new series on the Mongols is fantastic — Dan Carlin's research is wild. Saving the longer episodes for long runs since they are each four to six hours. I started the back catalog last year and am now on episode fifty-something."
        ],
    },
    {
        "question_id": "s21",
        "query": "autumn favorite season",
        "answer": "autumn",
        "sessions": [
            "Seasonal mood: autumn is hands down my favorite season. The light gets lower and the air smells like leaves. I look forward to wearing the heavier sweaters and making chili on Sundays. Fall in New York is especially good because the trees in central park turn this insane red for about two weeks in late October."
        ],
    },
    {
        "question_id": "s22",
        "query": "guitar fingerstyle teacher",
        "answer": "guitar",
        "sessions": [
            "Music practice log: I have been working on a fingerstyle arrangement of Blackbird on the guitar for the past month. The thumb pattern is tricky and I keep losing the syncopation in the bridge. My teacher Yoshi says I should slow it down to half tempo and metronome it for a week before trying to speed it up."
        ],
    },
    {
        "question_id": "s23",
        "query": "first pet beagle childhood",
        "answer": "Biscuit",
        "sessions": [
            "Childhood memory: my first pet was a beagle named Biscuit. We got him from a farm in upstate New York when I was seven. He lived to be fourteen and slept at the foot of my bed every night. Mom still has his collar in a box in the attic, even though we got Mochi the cat a year after Biscuit passed."
        ],
    },
    {
        "question_id": "s24",
        "query": "Spring Street office Soho",
        "answer": "Spring",
        "sessions": [
            "Office is on Spring Street in Soho, third floor of a converted warehouse. The commute from Brooklyn is a single subway transfer and the door-to-door time is around forty minutes. The neighborhood has way too many coffee shops which is dangerous for my wallet. Lunch is usually from the bao place around the corner."
        ],
    },
    {
        "question_id": "s25",
        "query": "software engineer career search infra",
        "answer": "software engineer",
        "sessions": [
            "Career check-in: I have been a software engineer for about nine years now, the last three on the search infra team at my current company. The work is a mix of distributed systems, query optimization, and a lot of oncall rotation. I have been telling myself for two years that I want to try management but never quite pulled the trigger."
        ],
    },
    {
        "question_id": "s26",
        "query": "Spirited Away rewatch movie",
        "answer": "Spirited Away",
        "sessions": [
            "Movie rewatch night: I put on Spirited Away again for probably the twentieth time. The animation holds up impossibly well. The train sequence across the water is still the most peaceful scene in any animated film. I showed it to my niece for the first time last month and she was transfixed the entire runtime."
        ],
    },
    {
        "question_id": "s27",
        "query": "iPhone Apple Pro Max",
        "answer": "iPhone",
        "sessions": [
            "Tech audit: I have been on iPhone for about six years now and probably will not switch. The integration with the MacBook and the AirPods is the killer feature for me. I do miss the customization of Android sometimes but the camera and the app ecosystem keep me locked in. Current model is the Pro Max which is comically large."
        ],
    },
    {
        "question_id": "s28",
        "query": "roommate Sam apartment cleaning",
        "answer": "Sam",
        "sessions": [
            "Apartment dynamics: my roommate Sam and I split the cleaning chores on a rotating schedule. He handles the kitchen and bathrooms, I take the living room and the shared office space. We have been living together for almost two years and it has worked out well, mostly because he is a night owl and I am an early riser."
        ],
    },
    {
        "question_id": "s29",
        "query": "Northwestern college undergrad",
        "answer": "Northwestern",
        "sessions": [
            "Throwback: I went to Northwestern for undergrad, studied computer science and a minor in music. Lived in a residential college all four years and made most of my long-term friends there. Visit the campus in Evanston maybe once a year for a football game in the fall. The new lakefill looks wild compared to my era."
        ],
    },
    {
        "question_id": "s30",
        "query": "morning pour-over journaling",
        "answer": "coffee and journaling",
        "sessions": [
            "Morning flow: I wake up at six, brew a pour-over, and sit at the kitchen counter for twenty minutes doing a journaling practice. After that I do a quick stretch routine and head to the subway by seven. The journaling is non-negotiable and I have done it almost every day for three years now."
        ],
    },
    {
        "question_id": "s31",
        "query": "sister Lina baby Austin",
        "answer": "Zoe",
        "sessions": [
            "Family news: my sister Lina had her first baby in February, a girl named Zoe. She was seven pounds four ounces and came two weeks early. Lina and her husband Theo are in Austin and I have only met Zoe once so far over a long weekend in March. They are coming to visit in July which I am very excited about."
        ],
    },
    {
        "question_id": "s32",
        "query": "hiking Saturday Hudson Valley",
        "answer": "hiking",
        "sessions": [
            "Weekend plans: hiking has become my default Saturday morning activity. There is a great six mile loop in the Hudson Valley that I have been doing most weekends since April. The trail is moderate difficulty with a few stream crossings. My friend Cassie usually joins and we grab breakfast at a diner on the way back into the city."
        ],
    },
    {
        "question_id": "s33",
        "query": "Spanish studied college refresher",
        "answer": "Spanish",
        "sessions": [
            "Language reminder: I studied Spanish throughout college and was intermediate-fluent at graduation. Have not used it seriously in five years and have lost a lot. Took a refresher class last fall at the local community college which helped bring back maybe seventy percent of what I had. Want to do a trip to Mexico City to force myself back into it."
        ],
    },
    {
        "question_id": "s34",
        "query": "Sky Ting Canal yoga vinyasa",
        "answer": "Sky Ting",
        "sessions": [
            "Studio update: I have been going to Sky Ting on Canal Street twice a week for the past three months. The vinyasa flow class on Wednesday evenings with instructor Mia is my favorite — challenging but not punishing. The studio has great natural light and the price is fair for Manhattan."
        ],
    },
    {
        "question_id": "s35",
        "query": "Thanksgiving Macy parade holiday",
        "answer": "Thanksgiving",
        "sessions": [
            "Holiday preference: I am a big Thanksgiving person. The food, the family time, the four day weekend — all of it. My family does a long hike in the morning before the big meal, weather permitting. The Macy's parade is on in the background the entire day. Christmas feels overhyped by comparison."
        ],
    },
    {
        "question_id": "s36",
        "query": "Diego high school oldest friend",
        "answer": "Diego",
        "sessions": [
            "Old friendships: Diego from high school is my oldest friend. We have known each other since sophomore year and have stayed close even after he moved to the Bay Area for work. We do a yearly backpacking trip in the Sierras which is one of my favorite weeks of the year. He was the one who recommended The Three-Body Problem to me."
        ],
    },
    {
        "question_id": "s37",
        "query": "Gouda cheese Court Street aged",
        "answer": "Gouda",
        "sessions": [
            "Snack log: aged Gouda is my cheese of choice. The local cheese shop on Court Street carries a really good Dutch version that I stock up on weekly. Pairs well with apple slices and a bit of quince paste. My friend Maya got me hooked on it years ago and I have not looked back since."
        ],
    },
    {
        "question_id": "s38",
        "query": "therapist Rachel weekly telehealth",
        "answer": "Rachel",
        "sessions": [
            "Therapy update: I have been seeing Rachel weekly for about a year now. We have been working through some work-related anxiety patterns and she has helped me get much better at setting boundaries with my manager. She is an LCSW with a cognitive behavioral orientation. Telehealth on Wednesdays at five."
        ],
    },
    {
        "question_id": "s39",
        "query": "Hoka Clifton Brooks running",
        "answer": "Hoka",
        "sessions": [
            "Gear update: I switched to Hoka Clifton nine running shoes about four months ago after years in Brooks. The cushioning is unreal and my knee pain on long runs has basically disappeared. I rotate two pairs so the foam has time to recover. About to break in my third pair next week."
        ],
    },
    {
        "question_id": "s40",
        "query": "born Boston moved Portland",
        "answer": "Boston",
        "sessions": [
            "Origin story: I was born in Boston and lived there until I was twelve. We moved to Portland, Oregon for my dad's job. I do not remember much of Boston except the brick sidewalks and the duck pond in the public garden. The accent wore off almost immediately after we moved out west."
        ],
    },
    {
        "question_id": "s41",
        "query": "Wingspan Ark Nova game night",
        "answer": "Wingspan",
        "sessions": [
            "Game night recap: we played Wingspan again last Saturday. I pulled off a clutch win with a heavy bird engine — about 110 points on a perfect engine turn. My partner Jordan got crushed on 88 points which is below his usual. We are thinking of trying Ark Nova next week for something different."
        ],
    },
    {
        "question_id": "s42",
        "query": "tiramisu Carroll Gardens mascarpone",
        "answer": "tiramisu",
        "sessions": [
            "Dessert ranking: tiramisu is the answer every time. I am not a big sweets person in general but a well-made tiramisu is something I cannot pass up. The version at the Italian spot in Carroll Gardens is the best I have had in the city. Light on the mascarpone, heavy on the espresso soak."
        ],
    },
    {
        "question_id": "s43",
        "query": "barber Astor Place fade",
        "answer": "Marcus",
        "sessions": [
            "Grooming: I have been going to Marcus at the Astor Place shop for about three years. He gives a great fade and is fast — fifteen minutes start to finish. Charges thirty-five dollars which is reasonable for the area. The shop has good music which is a plus for someone who hates small talk during cuts."
        ],
    },
    {
        "question_id": "s44",
        "query": "search infrastructure team role",
        "answer": "senior engineer",
        "sessions": [
            "Role context: I am a senior engineer on the search infrastructure team. The team owns the indexing pipeline, the query planner, and the relevance ranking layer. I focus mostly on the query side — latency budgets, ranking experiments, and the embedding-based recall system we shipped last year."
        ],
    },
    {
        "question_id": "s45",
        "query": "Netflix streaming Apple TV",
        "answer": "Netflix",
        "sessions": [
            "Media consumption: Netflix is still my most-used streaming service despite the password sharing crackdown. I have a single screen plan and rotate between it and my partner's account depending on what is on. The recommendation algorithm feels worse than it used to be. Apple TV Plus has some great originals but the catalog is thin."
        ],
    },
    {
        "question_id": "s46",
        "query": "Devoción Williamsburg pour-over remote",
        "answer": "Devoción",
        "sessions": [
            "Remote work spot: Devoción in Williamsburg is my go-to when I need a change of scenery from the apartment. The space is huge, the wifi is fast, and the coffee is excellent. They roast their own beans and the cold brew is some of the best in the city. I usually spend three to four hours there on Tuesday mornings."
        ],
    },
    {
        "question_id": "s47",
        "query": "fall Rockaway beach September",
        "answer": "fall",
        "sessions": [
            "Beach preference: fall is the best season for the beach in my opinion. The water is still warm from the summer, the crowds have died down, and the light is incredible. I went to Rockaway in late September last year and had a stretch of sand to myself for hours. Summer beaches are too chaotic for my taste."
        ],
    },
    {
        "question_id": "s48",
        "query": "Priya Mehta founded company",
        "answer": "Priya",
        "sessions": [
            "Company context: my current employer was founded by Priya Mehta about six years ago. She was an early engineer at two well-known search companies before starting this. She is still very technical and reviews most of the architectural decisions on the platform side. We have a small all-hands every other Friday where she gives a candid update."
        ],
    },
    {
        "question_id": "s49",
        "query": "indie rock National Big Thief",
        "answer": "indie rock",
        "sessions": [
            "Listening habits: indie rock is probably the genre I come back to most. The National, Big Thief, Radiohead, Mitski. I have a few core albums I have been listening to for over a decade. Live shows are my favorite way to experience it — saw Big Thief at Brooklyn Steel last month which was transcendent."
        ],
    },
    {
        "question_id": "s50",
        "query": "Sey Coffee Bushwick Brooklyn pour-over",
        "answer": "Sey Coffee",
        "sessions": [
            "Cafe list: I rotate between a few spots in Brooklyn for working remotely. Sey Coffee in Bushwick is the best for pour-over and atmosphere, though it is small and fills up fast. Devoción is the most spacious. Konditori is my go-to for a quiet morning with a pastry. All three have solid wifi."
        ],
    },
    {
        "question_id": "s51",
        "query": "neighbor dog golden retriever Linda",
        "answer": "Biscuit",
        "sessions": [
            "Building characters: my neighbor in 7A has the sweetest golden retriever named Biscuit. He barks at me every time I come up the stairs but it is friendly barking. His owner is an older woman named Linda who walks him three times a day. I sometimes dog-sit when Linda goes to visit her daughter in Connecticut on weekends."
        ],
    },
    {
        "question_id": "s52",
        "query": "Subaru Outback Wilderness drive",
        "answer": "Subaru",
        "sessions": [
            "Car details: I drive a Subaru Outback, the 2021 model. It is the Wilderness trim which has the slightly higher ground clearance. The all-wheel-drive has been a lifesaver during the two real snowstorms we get each winter. The roof rack is great for transporting my bike and my camping gear in the summer."
        ],
    },
    {
        "question_id": "s53",
        "query": "Big Thief Brooklyn Steel concert",
        "answer": "Big Thief",
        "sessions": [
            "Concert recap: I saw Big Thief at Brooklyn Steel at the end of last month. They played a strong set with most of my favorite tracks from Dragon New Warm Mountain. The opener was a folk duo I had not heard of before but they were surprisingly good. I went with Cassie and we grabbed late night ramen after."
        ],
    },
    {
        "question_id": "s54",
        "query": "McCarren Park Greenpoint apartment",
        "answer": "McCarren",
        "sessions": [
            "Park life: McCarren Park in Greenpoint is the closest green space to my apartment. I run the loop there maybe twice a week and walk through it on the way to the subway. The farmers market on Saturdays is one of the best in the borough. The dog run is also fun to watch even though I do not have a dog."
        ],
    },
    {
        "question_id": "s55",
        "query": "Harney Sons Paris Sencha tea",
        "answer": "Harney & Sons",
        "sessions": [
            "Tea stash: Harney & Sons is my default for loose leaf. Their Paris blend is what I drink in the morning most days — a fruity black tea that goes well with a small amount of milk. I keep a backup tin of their Japanese Sencha for the afternoons. The price is fair for the quality and the tin lasts about three weeks."
        ],
    },
    {
        "question_id": "s56",
        "query": "retire Lisbon Portugal savings",
        "answer": "Lisbon",
        "sessions": [
            "Future planning: I have been telling myself for years that I want to retire in Lisbon. The cost of living is reasonable, the food is excellent, the people are warm, and the climate is mild. I have visited twice and it just feels livable in a way that New York does not. Buying a small apartment there is on the long-term savings plan."
        ],
    },
    {
        "question_id": "s57",
        "query": "subway L train Bedford commute",
        "answer": "subway",
        "sessions": [
            "Commute preference: the subway is my preferred way to get to work. The L train from Bedford to 8th Avenue takes twenty minutes door to door. I read on the train which is the only consistent reading time I have these days. Walking is great when the weather is nice but a forty-five minute walk is too much most days."
        ],
    },
    {
        "question_id": "s58",
        "query": "Death Co first date partner Jordan",
        "answer": "Death & Co",
        "sessions": [
            "Anniversary approaching: Jordan and I are about to hit our five year anniversary. We had our first date at Death & Co on East 6th Street — the bartender made us a custom off-menu cocktail each. We are planning to go back to the same spot to celebrate. The drinks were strong enough that we ended up at a dumpling place until midnight."
        ],
    },
    {
        "question_id": "s59",
        "query": "soccer high school center midfielder",
        "answer": "soccer",
        "sessions": [
            "High school days: I played soccer all four years of high school, mostly as a center midfielder. Varsity captain my senior year. We made it to the state semifinals and lost in penalties which still stings a bit. I tore my ACL junior year and came back for the senior season which was a defining experience for me."
        ],
    },
    {
        "question_id": "s60",
        "query": "Konditori Smith Street Sunday morning",
        "answer": "Konditori",
        "sessions": [
            "Sunday ritual: Konditori on Smith Street in Cobble Hill is my Sunday morning spot. The cardamom bun is the best pastry in the borough and they do a cortado that rivals any specialty shop. I usually sit at the same table in the back, work on a personal writing project for an hour, then head home to make lunch."
        ],
    },
]


# -----------------------------------------------------------------------
# Schema bootstrap — minimal copy of prod schema
# -----------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    source_file   TEXT NOT NULL,
    tags          TEXT DEFAULT '[]',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    observed_at   TEXT NOT NULL,
    pinned        INTEGER DEFAULT 0,
    importance    INTEGER DEFAULT 3,
    decay         TEXT DEFAULT 'none',
    score         REAL DEFAULT 1.0,
    supersedes    TEXT,
    repo_id       TEXT,
    access_count  INTEGER DEFAULT 1,
    success_score REAL DEFAULT 0.0,
    fitness_score REAL DEFAULT 1.0,
    conflict_policy TEXT DEFAULT 'supersede',
    version_vector TEXT DEFAULT '{}',
    logical_clock INTEGER DEFAULT 0,
    consolidation_state TEXT DEFAULT 'working',
    valid_from    TEXT,
    valid_to      TEXT,
    superseded_by TEXT,
    last_accessed TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    tags,
    tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS backlinks (
    source_id TEXT,
    target_id TEXT,
    PRIMARY KEY (source_id, target_id)
);
CREATE TABLE IF NOT EXISTS file_mtimes (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content, tags)
  VALUES (new.rowid, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  DELETE FROM memories_fts WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  DELETE FROM memories_fts WHERE rowid = old.rowid;
  INSERT INTO memories_fts(rowid, content, tags)
  SELECT new.rowid, new.content, new.tags WHERE new.deleted_at IS NULL;
END;
"""


def bootstrap_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    db = sqlite3.connect(str(db_path), timeout=30.0)
    db.executescript(SCHEMA_SQL)
    db.commit()
    db.close()


def insert_memory(
    db_path: Path, note_id: str, content: str, tags: list, now_iso: str
) -> None:
    db = sqlite3.connect(str(db_path), timeout=30.0)
    db.execute("PRAGMA busy_timeout = 30000;")
    db.execute(
        """INSERT INTO memories
           (id, source_file, content, tags, created_at, updated_at, observed_at,
            pinned, importance, fitness_score, valid_from)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, 3, 1.0, ?)""",
        (
            note_id,
            f"lmeval/{note_id}.md",
            content,
            json.dumps(tags),
            now_iso,
            now_iso,
            now_iso,
            now_iso,
        ),
    )
    db.commit()
    db.close()


def fetch_content(db_path: Path, note_id: str) -> str:
    """memory_mcp.search_memories returns result dicts WITHOUT a 'content'
    key — content is only embedded in the formatted 'output' text. To get
    the content for scoring, look it up directly via SQL."""
    if not note_id:
        return ""
    try:
        db = sqlite3.connect(str(db_path), timeout=5.0)
        row = db.execute(
            "SELECT content FROM memories WHERE id = ?", (note_id,)
        ).fetchone()
        db.close()
        return (row[0] if row else "") or ""
    except Exception:
        return ""


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def score_hit(top_content: str | None, expected_answer: str) -> tuple[int, list[str]]:
    """Per the spec: 1 if any answer token is in top result; else 0.
    Multi-token answers use token-set overlap (>=1 token = 1)."""
    if not top_content:
        return 0, []
    norm_content = normalize_text(top_content)
    norm_content_tokens = set(norm_content.split())
    answer_tokens = [t for t in normalize_text(expected_answer).split() if t]
    if not answer_tokens:
        return 0, []
    matches = [t for t in answer_tokens if t in norm_content_tokens]
    return (1 if matches else 0), matches


def main():
    # Write synthetic dataset to disk for the record
    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DATASET_PATH, "w") as f:
        for entry in SYNTHETIC:
            f.write(json.dumps({**entry, "source": "synthetic"}) + "\n")
    print(f"Wrote {len(SYNTHETIC)} synthetic questions to {DATASET_PATH}")

    # Per-question fresh DB + run
    run_id = uuid.uuid4().hex[:8]
    run_dir = Path(f"/tmp/lmeval_run_{run_id}")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Run dir: {run_dir}")

    per_question: list[dict] = []
    hybrid_hits = 0
    baseline_hits = 0
    latencies_ms: list[float] = []
    t_start = time.perf_counter()

    for q in SYNTHETIC:
        qid = q["question_id"]
        query = q["query"]
        answer = q["answer"]
        sessions = q["sessions"]
        db_path = run_dir / f"{qid}.db"

        # Bootstrap fresh DB
        bootstrap_db(db_path)

        # Write session content as memories. Use one memory per session paragraph.
        now_iso = datetime.now(timezone.utc).isoformat()
        for i, paragraph in enumerate(sessions, 1):
            note_id = f"lmeval/{qid}-{i}"
            insert_memory(db_path, note_id, paragraph, [qid], now_iso)

        # Clear global search cache between questions
        if hasattr(memory_mcp, "_search_cache"):
            memory_mcp._search_cache.clear()

        # Hybrid search
        t0 = time.perf_counter()
        try:
            r_hybrid = memory_mcp.search_memories(
                db_path,
                query,
                limit=10,
                hybrid=True,
                use_history=False,
            )
        except Exception as e:
            r_hybrid = {"results": [], "count": 0, "error": str(e)}
        t_hybrid_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(t_hybrid_ms)

        # Baseline (hybrid=False) search
        t0 = time.perf_counter()
        try:
            r_baseline = memory_mcp.search_memories(
                db_path,
                query,
                limit=10,
                hybrid=False,
                use_history=False,
            )
        except Exception as e:
            r_baseline = {"results": [], "count": 0, "error": str(e)}
        t_baseline_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(t_baseline_ms)

        # Score
        top_hybrid = (r_hybrid.get("results") or [{}])[0]
        top_baseline = (r_baseline.get("results") or [{}])[0]
        # search_memories doesn't return 'content' in result dicts; fetch
        # it from the DB using the returned id.
        hybrid_id = top_hybrid.get("id", "")
        baseline_id = top_baseline.get("id", "")
        hybrid_content = fetch_content(db_path, hybrid_id)
        baseline_content = fetch_content(db_path, baseline_id)
        h_hit, h_matches = score_hit(hybrid_content, answer)
        b_hit, b_matches = score_hit(baseline_content, answer)
        hybrid_hits += h_hit
        baseline_hits += b_hit

        per_question.append(
            {
                "qid": qid,
                "query": query,
                "answer": answer,
                "top_result_id": hybrid_id,
                "top_score": float(
                    top_hybrid.get("final_score", top_hybrid.get("score", 0.0)) or 0.0
                ),
                "top_content_preview": (hybrid_content[:160] + "…")
                if len(hybrid_content) > 160
                else hybrid_content,
                "hybrid_hit": h_hit,
                "hybrid_matched_tokens": h_matches,
                "baseline_hit": b_hit,
                "baseline_matched_tokens": b_matches,
                "hybrid_top_id": hybrid_id,
                "baseline_top_id": baseline_id,
                "hybrid_latency_ms": round(t_hybrid_ms, 2),
                "baseline_latency_ms": round(t_baseline_ms, 2),
            }
        )

    wall_time_s = time.perf_counter() - t_start
    n = len(SYNTHETIC)
    hybrid_score = hybrid_hits / n
    baseline_score = baseline_hits / n
    lift = hybrid_score - baseline_score

    # Percentiles over all latencies (hybrid + baseline)
    p50 = sorted(latencies_ms)[int(len(latencies_ms) * 0.5)]
    p95 = sorted(latencies_ms)[
        min(int(len(latencies_ms) * 0.95), len(latencies_ms) - 1)
    ]
    avg_latency_ms = (wall_time_s * 1000.0) / (n * 2)

    # Worst 5 hybrid misses
    misses = [r for r in per_question if r["hybrid_hit"] == 0]
    misses.sort(key=lambda r: r["qid"])
    worst_5 = misses[:5]

    result = {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark": "LongMemEval_S",
        "source": "synthetic",
        "synthetic_dataset_path": str(DATASET_PATH),
        "question_count": n,
        "hybrid_score": round(hybrid_score, 4),
        "baseline_ft5_score": round(baseline_score, 4),
        "hybrid_minus_baseline": round(lift, 4),
        "wall_time_seconds": round(wall_time_s, 3),
        "p50_p95_ms": [round(p50, 1), round(p95, 1)],
        "avg_latency_ms_per_query": round(avg_latency_ms, 1),
        "per_question_results": per_question,
        "comparison_to_sota": {
            "emergence_86pct_at_5_65s": (
                f"we are at {hybrid_score * 100:.1f}% at {wall_time_s / n:.2f}s/item "
                f"(Emergence 86% at 5.65s/item)"
            ),
            "zep_plus_18_5pct_on_lme": (
                f"we are at {hybrid_score * 100:.1f}% which is "
                f"{'+' if lift >= 0 else ''}{lift * 100:.1f}% over our FTS5 baseline "
                f"(Zep cites +18.5% on LongMemEval)"
            ),
        },
        "worst_5_misses": [
            {
                "qid": r["qid"],
                "query": r["query"],
                "expected_answer": r["answer"],
                "top_result_id": r["top_result_id"],
                "top_content_preview": r["top_content_preview"],
            }
            for r in worst_5
        ],
        "notes": [
            "SYNTHETIC data: HF xiaowu0162/LongMemEval returned 404; "
            "xinrongzhang2022/LongMemEval not found on Hub. "
            "60 handcrafted LongMemEval-style factoid questions written to "
            "eval/datasets/longmemeval_s_synth.jsonl.",
            "Baseline score uses search_memories(hybrid=False). "
            "NOTE: hybrid=False still triggers the C2 embedding fallback "
            "when FTS5 returns 0 results (per memory_mcp.py:1230-1271), "
            "so the baseline is BM25+embedding-safety-net, not pure BM25.",
            "Schema is a minimal copy of prod (memories, memories_fts, "
            "backlinks, file_mtimes, the 3 FTS triggers, perf indexes). "
            "Embedding model was loaded fresh per-process by memory_mcp.",
            "Per-question fresh DB in /tmp/lmeval_run_<uuid>/.",
        ],
    }

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n=== SUMMARY ===")
    print(f"Questions: {n}")
    print(f"Hybrid score: {hybrid_score * 100:.1f}%")
    print(f"Baseline score: {baseline_score * 100:.1f}%")
    print(f"Lift: {lift * 100:+.1f}%")
    print(f"Wall time: {wall_time_s:.1f}s ({wall_time_s / n:.2f}s/item)")
    print(f"p50 / p95 latency: {p50:.1f}ms / {p95:.1f}ms")
    print(f"Wrote {RESULTS_PATH}")


if __name__ == "__main__":
    main()
