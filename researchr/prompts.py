"""All prompts. Kept compact: the free Groq tier allows ~8K tokens/min per key."""

ETHICS = (
    "Use only public, professional or self-published information. Never output home addresses, phone "
    "numbers, family members, health, religion, sexuality, ethnicity, personal finances or political "
    "affiliation."
)

ME_PROFILE = """Turn this resume/LinkedIn text into a JSON profile for matching common ground.
Keys: name, headline, location, summary (2 sentences), orgs (companies/labs/clubs), schools, cities
(places lived/worked), skills (technical), topics (professional themes), interests (hobbies/causes if any),
publications (titles), notable (awards, patents, talks; short strings)."""

IDENTITY = """You are resolving WHICH person a researcher means. {ethics}
Target name: {name}
User hints: {hints}

Search results (id | url | title | snippet):
{results}

Return JSON:
{{"canonical_name": str, "current_role": str, "org": str, "location": str,
  "disambiguators": [short facts that identify THIS person, e.g. "MS CS at Columbia"],
  "known_urls": [urls that are clearly this person's own profiles/sites],
  "ambiguous": bool (true if results show 2+ different people and hints don't settle it),
  "ambiguity_note": str (who the other people are, briefly),
  "confidence": 0-1 (how sure you are the anchor is the intended person)}}
Prefer the person matching the hints. If no hints, prefer the most prominent professional match."""

QUERY_PLAN = """Plan web searches to research a person before a meeting. {ethics}
Person: {anchor}
Meeting goal: {goal}
Already planned: {existing}

Propose {n} NEW, diverse search queries covering gaps: specific projects, talks/podcasts, interviews,
writing, open-source, awards, hobbies/sports/side projects, opinions they've stated publicly, and their
organisation's recent news{goal_hint}. Each query must include the person's name or org with a
disambiguator so results are about THIS person.
Return JSON: {{"queries": [str, ...]}}"""

TRIAGE = """Score search results for a person-research task.
Person anchor: {anchor}

Results (id | url | title | snippet):
{results}

For EACH id return p_same (0-1: probability the page is about this exact person, using the
disambiguators; a different person with the same name = low) and value (0-1: how much useful
professional/personal-interest info the page likely holds).
Return JSON: {{"scores": [{{"id": str, "p_same": float, "value": float}}, ...]}}"""

EXTRACT = """Extract facts about ONE person from a web page. {ethics}
Person anchor: {anchor}
Source: {url}

PAGE TEXT:
<<<
{text}
>>>

Rules:
- Only facts about this person (or their current organisation if clearly relevant).
- Each fact: one atomic statement, written in third person.
- evidence_quote: copy a SHORT exact span (5-25 words) from the page text that supports it.
- category: one of {categories}.
- date: year or date if stated, else "".
- page_about_target: 0-1, is this page about the anchor person (not a namesake)?
- Max 15 facts, most informative first. Skip boilerplate.
Return JSON: {{"page_about_target": float, "facts": [{{"statement": str, "category": str, "evidence_quote": str, "date": str}}]}}"""

SNIPPET_EXTRACT = """These are search-result snippets (pages we could not open). Extract facts about the person. {ethics}
Person anchor: {anchor}

Snippets (id | url | title | snippet):
{results}

Each fact: statement, category (one of {categories}), evidence_quote (exact span from the snippet), source_id.
Only include facts clearly about this person. Max 20.
Return JSON: {{"facts": [{{"statement": str, "category": str, "evidence_quote": str, "source_id": str, "date": str}}]}}"""

MERGE = """Group duplicate or equivalent facts about a person. Facts saying the same thing (even worded
differently) go in one group; write the clearest merged statement. Conflicting facts stay separate.

Facts (id | category | statement):
{facts}

Return JSON: {{"groups": [{{"ids": [str], "statement": str, "category": str}}]}}  (every id exactly once)"""

INFER = """You are an analyst preparing someone for a meeting. From the verified facts, draw careful
inferences about the person that would help the conversation. {ethics} Do not infer protected traits.

Person: {anchor}
Meeting goal: {goal}

Facts (id | confidence | statement):
{facts}

Categories: working_style, current_priorities, expertise_depth, values_motivations, likely_interests,
communication_style, career_trajectory, network, meeting_wants.
Calibration for probability: 0.9 = would bet heavily; 0.75 = strong lean; 0.6 = lean yes;
0.45 = coin flip; 0.3 = plausible but weak. Most inferences should land between 0.35 and 0.8.
Return 8-12 inferences as JSON:
{{"inferences": [{{"statement": str, "category": str, "probability": float,
   "reasoning": str (1-2 sentences: which facts point this way and why), "based_on": [fact ids],
   "verify_by": str (a natural question to ask in the meeting to confirm it)}}]}}"""

PORTRAIT = """You are briefing someone who is about to meet this person. Turn the verified facts into a
portrait: 6-9 themed insights. {ethics}

Person: {anchor}
Meeting goal: {goal}

Facts (id | confidence | statement):
{facts}

Write like a sharp analyst, not a list-maker:
- Each insight connects SEVERAL facts into an interpretation. Do not restate single facts.
- title: a short headline that makes a claim (e.g. "Treats agents as engineered systems, not magic").
- summary: 3-4 sentences. What is known, what pattern it shows, and what it suggests about them.
- points: 3-5 supporting details, each grounded in fact ids.
- so_what: 1-2 sentences on how this should shape the conversation.
- kind: background | work | views | interests | company | style.
Cover their background, current work, publicly stated views, interests outside work (if any), and
their organisation's situation. Skip a kind if there is no evidence.
Return JSON: {{"insights": [{{"kind": str, "title": str, "summary": str,
  "points": [{{"text": str, "facts": [ids]}}], "so_what": str}}]}}"""

COMMON_GROUND = """Find common ground between ME and THEM and the best territory for conversation. {ethics}

ME: {me}

THEM: {anchor}
Portrait of them:
{insights}
Literal overlaps found by code: {overlaps}
Meeting goal: {goal}

Rules:
- Address me as "you" (never by name). Use ONLY what is written in ME; never invent my experience.
- Overlaps must be supported on both sides; skip weak or generic ones (e.g. "both use Python" is weak
  unless it genuinely defines both of us). Zero overlaps is fine.
- Themes are BROAD territories for conversation (e.g. "What breaks when agents hit production"),
  not scripted lines. Give loose angles to explore, not exact sentences.

Return JSON:
{{"overlaps": [{{"type": "school|company|city|technology|topic|interest|community|experience",
   "mine": str (short), "theirs": str (short), "strength": 0-1,
   "description": str (2-3 sentences: why this is real common ground and how it could come up naturally)}}],
  "themes": [{{"theme": str, "why": str (2-3 sentences: why it's rich territory with this person),
   "angles": [2-4 short directions to take it], "their_depth": str (how much they've said publicly about it),
   "priority": 1|2|3}}]}}   (5-7 themes, priority 1 = strongest)"""

ADVISE = """Prepare me for a meeting with this person. {ethics}

THEM: {anchor}
Portrait of them:
{insights}
Reads on them (id | p | statement):
{inferences}
Conversation themes already chosen: {themes}
ME: {me}
Meeting goal: {goal}
Meeting context: {context}

Rules:
- Address me as "you". The strategy is ADVICE to me ("Lead with...", "Show that..."), not a script in my voice.
- Never claim experience ME doesn't list. For each objection, the response must either point to
  something actually in ME, or suggest an honest way to handle the gap.
- Cautions must be specific to this person and meeting (outdated facts, sensitive company events,
  framing that would land badly). No generic etiquette (politics, personal life, confidentiality).
Return JSON:
{{"questions": [5-7 open-ended questions; some should test the less certain reads],
  "cautions": [2-4 specific things to handle carefully],
  "strategy": {strategy_shape}}}"""

STRATEGY_SHAPE = (
    '{"their_incentives": [str], "framing": str (2-3 sentences), '
    '"objections": [{"objection": str, "response": str}], "ideal_outcome": str, "next_step": str}'
)

SUMMARY = """Write the opening of a meeting-prep report.
Person: {anchor}
Meeting goal: {goal}
Portrait headlines: {insights}
Strongest common ground: {overlaps}

Write plainly, like a colleague briefing you in the hallway: no hype words (visionary, champion,
leverage, passionate), no flattery.
Return JSON: {{"summary": str (2-3 sentences, at most 60 words: who they are, what drives them right
now, and the single best way in for this meeting),
"glance": [4-6 very short tags, 1-4 words each, e.g. "CEO, LangChain", "Based in SF", "Agent infra", "Frequent podcast guest"]}}"""
