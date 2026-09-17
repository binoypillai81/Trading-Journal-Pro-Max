"""Questionnaire definitions, validation, specificity prompts and thesis snapshot.

The schema is served to the frontend so options exist in one place. Validation
is enforced on the server: the frontend cannot lock a thesis or complete a
review with missing required answers.
"""
from __future__ import annotations

import hashlib
import json
import re

SETUPS = ["Trend continuation", "Trend reversal", "Breakout", "Breakdown", "Pullback", "EMA interaction",
          "Pivot interaction", "Support/resistance", "Momentum", "Mean reversion", "Range trade", "Opening move",
          "Previous day high/low", "Previous swing high/low", "Other", "I don't know"]
EMOTIONS = ["Calm", "Confident", "Excited", "Fearful", "Anxious", "Frustrated", "Bored", "Revenge feeling", "FOMO",
            "Desire to recover losses", "Desire to make money", "Impatient", "Overconfident", "Underconfident",
            "Unsure", "None of these"]
TIMEFRAMES = ["< 15 minutes", "15–30 minutes", "30–60 minutes", "1–2 hours", "Rest of session", "Multiple sessions",
              "No specific expectation"]
TIMEFRAME_MINUTES = {"< 15 minutes": (0, 15), "15–30 minutes": (15, 30), "30–60 minutes": (30, 60),
                     "1–2 hours": (60, 120), "Rest of session": (0, None), "Multiple sessions": (None, None),
                     "No specific expectation": (None, None)}
DAY_PNL_INFLUENCE = ["Yes — trying to recover a loss", "Yes — trying to protect gains", "Yes — trying to reach a daily target",
                     "Yes — other", "No", "Unsure", "First trade of the day"]
CONFIRMATION_OPTIONS = ["Yes — it had completed", "No — I entered before it completed", "No confirmation needed for this setup", "Unsure"]
INFLUENCES = ["Previous loss", "Previous win", "News", "Fear of missing the move", "P&L", "Position sizing",
              "Other person's opinion", "Market suddenly moving", "Desire to trade", "Boredom", "Nothing outside my plan"]
PSYCH_FLAGS = ["FOMO", "Revenge trading", "Overtrading", "Fear of missing out", "Fear of loss", "Moving stop",
               "Premature exit", "Holding loser", "Increasing size", "Decreasing size because of fear",
               "Trading after emotional event", "Trading without setup", "Trying to recover previous loss",
               "Profit target interference", "Other"]

THESIS_SECTIONS = [
    {"id": "observe", "title": "The moment", "fields": [
        {"name": "chart_observation", "type": "text", "required": True, "specific": True,
         "label": "You are standing at the entry moment. What do you see on the chart?"},
    ]},
    {"id": "setup", "title": "A. Trade setup", "fields": [
        {"name": "setups", "type": "multi", "options": SETUPS, "required": True,
         "label": "What setup did you believe you were trading?"},
        {"name": "setup_other", "type": "text", "label": "If Other: describe the setup", "required_if": ["setups", "Other"]},
    ]},
    {"id": "direction", "title": "B. Trade direction", "fields": [
        {"name": "direction", "type": "single", "options": ["Long", "Short"], "required": True, "label": "Direction"},
    ]},
    {"id": "reason", "title": "C. Primary reason", "fields": [
        {"name": "primary_reason", "type": "text", "required": True, "specific": True,
         "label": "What was the single main reason you entered this trade?"},
        {"name": "one_sentence", "type": "text", "required": True, "specific": True,
         "label": "If you had to explain this trade to another trader in one sentence, what would you say?"},
    ]},
    {"id": "expectation", "title": "Expected market behaviour", "fields": [
        {"name": "expected_direction", "type": "single", "options": ["Up", "Down", "Sideways", "Unsure"], "required": True,
         "label": "Expected direction"},
        {"name": "expected_move_points", "type": "number", "label": "How much did you expect the market to move in your direction? (points)"},
        {"name": "expected_move_pct", "type": "number", "label": "…or as a percentage"},
        {"name": "target_price", "type": "number", "label": "Optional target price (chart price scale)"},
        {"name": "expected_timeframe", "type": "single", "options": TIMEFRAMES, "required": True,
         "label": "How quickly did you expect your thesis to play out?"},
        {"name": "invalidation", "type": "text", "required": True, "specific": True,
         "label": "What specific market behaviour would prove that your trade idea was wrong?"},
        {"name": "invalidation_price", "type": "number",
         "label": "Invalidation price level, if your invalidation was a price (lets the tool check it objectively)"},
    ]},
    {"id": "risk", "title": "Risk assessment", "fields": [
        {"name": "stop_price", "type": "number", "label": "Where was your logical stop? (price on the chart scale)"},
        {"name": "stop_points", "type": "number", "label": "…or stop distance in points"},
        {"name": "stop_pct", "type": "number", "label": "…or stop distance in %"},
        {"name": "stop_reason", "type": "text", "label": "Why was the stop placed there?"},
        {"name": "max_adverse_points", "type": "number", "label": "Maximum reasonable adverse movement you believed possible (points)"},
        {"name": "asymmetric", "type": "single", "options": ["Yes", "No", "Unsure"], "label": "Did you believe the trade had asymmetric reward/risk?"},
        {"name": "expected_reward", "type": "number", "label": "Expected reward (points)", "show_if": ["asymmetric", "Yes"]},
        {"name": "expected_risk", "type": "number", "label": "Expected risk (points)", "show_if": ["asymmetric", "Yes"]},
        {"name": "expected_rr", "type": "number", "label": "Expected R:R (computed if reward and risk are given)", "show_if": ["asymmetric", "Yes"]},
    ]},
    {"id": "confidence", "title": "Confidence", "fields": [
        {"name": "confidence", "type": "slider", "required": True, "label": "How confident were you in this trade? (0–100)"},
        {"name": "confidence_reason", "type": "text", "required": True, "specific": True,
         "label": "Why did you give yourself this confidence score?"},
        {"name": "setup_quality", "type": "slider", "required": True,
         "label": "How strong was the setup according to your own trading rules? (0–100, separate from emotional confidence)"},
    ]},
    {"id": "psychology", "title": "Psychology", "fields": [
        {"name": "emotions", "type": "multi", "options": EMOTIONS, "required": True,
         "label": "Before entering the trade, were you experiencing any of these?"},
        {"name": "pre_trade_thoughts", "type": "text", "required": True,
         "label": "What were you thinking immediately before pressing Buy/Sell?"},
        {"name": "had_to_take", "type": "single", "options": ["Yes", "No", "Unsure"], "required": True,
         "label": "Did you feel you HAD to take this trade?"},
        {"name": "if_not_taken", "type": "text", "label": "If you had not taken this trade, what would you have felt?"},
        {"name": "day_pnl_influence", "type": "single", "options": DAY_PNL_INFLUENCE,
         "label": "Was today's P&L on your mind when you entered this trade?"},
    ]},
    {"id": "process", "title": "Process compliance", "fields": [
        {"name": "predefined_setup", "type": "single", "options": ["Yes", "Partially", "No", "I don't know"], "required": True,
         "label": "Was this trade part of a predefined trading setup?"},
        {"name": "rules_followed", "type": "single", "options": ["Completely", "Mostly", "Partially", "Not at all", "I had no defined rules"],
         "required": True, "label": "Did you follow your trading rules?"},
        {"name": "confirmation_before_entry", "type": "single", "options": CONFIRMATION_OPTIONS,
         "label": "Had the confirmation you were waiting for (e.g. a candle closing beyond the level) already happened when you entered?"},
        {"name": "outside_influences", "type": "multi", "options": INFLUENCES,
         "label": "Did anything influence the trade that was NOT part of your trading plan?"},
        {"name": "outside_influence_text", "type": "text", "label": "Describe it"},
    ]},
]

POST_SECTIONS = [
    {"id": "reflection", "title": "Post-trade reflection", "fields": [
        {"name": "reflection", "type": "text", "required": True, "specific": True,
         "label": "Now that you know the outcome, what do you think about the trade?"},
        {"name": "hindsight_assessment", "type": "single", "required": True,
         "options": ["My original reasoning was genuinely visible", "Partially visible", "Mostly hindsight", "I cannot tell"],
         "label": "Looking back, was the original reasoning actually present in the chart at entry, or are you now explaining "
                  "the trade using information that was only visible afterwards?"},
        {"name": "flawed_part", "type": "text",
         "label": "Which part of your original decision process, if any, do you believe was flawed?"},
    ]},
    {"id": "psych_analysis", "title": "Psychology analysis", "fields": [
        {"name": "psychology_influenced", "type": "single", "options": ["No", "Possibly", "Yes"], "required": True,
         "label": "Looking back, did psychology influence this trade?"},
        {"name": "psychology_flags", "type": "multi", "options": PSYCH_FLAGS, "show_if": ["psychology_influenced", ["Possibly", "Yes"]],
         "required_if": ["psychology_influenced", ["Possibly", "Yes"]], "label": "Identify what applied"},
        {"name": "psychology_explanation", "type": "text", "label": "Explain"},
        {"name": "lesson", "type": "text", "label": "Lesson or observation to carry forward (optional — it is an observation, not a rule)"},
    ]},
]

# Vague-answer detection → specific follow-up prompts (journal quality, spec §41)
_VAGUE = [
    (r"\b(thought|think|felt|believed|expected)\b.{0,20}\b(it|price|market|nifty)\b.{0,15}\b(would|will|to)\b.{0,10}\b(go|move|rise|fall)\b.{0,6}\b(up|down|higher|lower)\b\.?$",
     "What specifically on the chart made you expect that movement?"),
    (r"^\s*(bad|good|poor|great|terrible|ok|okay|fine)\s+(trade|entry|decision)\.?\s*$",
     "Which part of your original decision process do you believe was flawed (or sound)?"),
    (r"\b(looked|look|seemed)\s+(good|nice|right|strong|weak|bad)\b", "What exactly did it look like? Name the price action, level or indicator."),
    (r"\b(gut|just felt|felt like it|no reason|dont know|don't know|random)\b", "Try to name anything observable that preceded the decision, even if it was a feeling triggered by a price move."),
    (r"^\s*(price|it)\s+(goes|went)\s+(against|below|above)\s*(me|it)?\.?\s*$",
     "Against which level or behaviour exactly? A price, a candle close, an EMA cross?"),
]
MIN_WORDS = {"chart_observation": 6, "primary_reason": 6, "one_sentence": 6, "invalidation": 5, "confidence_reason": 4,
             "reflection": 6}


def _all_fields(sections):
    return [f for s in sections for f in s["fields"]]


def schema() -> dict:
    return {"thesis": THESIS_SECTIONS, "post": POST_SECTIONS}


def _cond(answers, cond) -> bool:
    if not cond:
        return False
    name, val = cond
    got = answers.get(name)
    vals = val if isinstance(val, list) else [val]
    if isinstance(got, list):
        return any(v in got for v in vals)
    return got in vals


def _empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, list) and not v)


def specificity_prompt(field: str, text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip().lower()
    for pattern, prompt in _VAGUE:
        if re.search(pattern, t):
            return prompt
    if field in MIN_WORDS and len(re.findall(r"\w+", t)) < MIN_WORDS[field]:
        return {"chart_observation": "Describe what you see: trend, where price is relative to the EMA and pivots, the last few candles.",
                "primary_reason": "What specifically on the chart made you enter?",
                "one_sentence": "Say it as you would to another trader: setup, trigger and expectation.",
                "invalidation": "Which specific level or behaviour would prove the idea wrong?",
                "confidence_reason": "What made the score this number and not 20 points higher or lower?",
                "reflection": "Which part of your original decision process do you believe was sound or flawed?"}[field]
    return None


def _coerce(field, v):
    if field["type"] in ("number", "slider"):
        if _empty(v):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{field['label']}: must be a number")
    if field["type"] == "multi":
        if _empty(v):
            return []
        if not isinstance(v, list):
            v = [v]
        bad = [x for x in v if x not in field["options"]]
        if bad:
            raise ValueError(f"{field['label']}: invalid options {bad}")
        return v
    if field["type"] == "single":
        if _empty(v):
            return None
        if v not in field["options"]:
            raise ValueError(f"{field['label']}: invalid option '{v}'")
        return v
    return (v or "").strip() if isinstance(v, str) or v is None else str(v)


def validate(kind: str, answers: dict) -> dict:
    """Return {'answers': cleaned, 'errors': {field: msg}, 'prompts': {field: prompt}}."""
    sections = THESIS_SECTIONS if kind == "thesis" else POST_SECTIONS
    cleaned, errors, prompts = {}, {}, {}
    for f in _all_fields(sections):
        try:
            cleaned[f["name"]] = _coerce(f, answers.get(f["name"]))
        except ValueError as exc:
            errors[f["name"]] = str(exc)
    for f in _all_fields(sections):
        n, v = f["name"], cleaned.get(f["name"])
        if n in errors:
            continue
        if (f.get("required") or _cond(cleaned, f.get("required_if"))) and _empty(v):
            errors[n] = "Required"
        if f["type"] == "slider" and v is not None and not 0 <= v <= 100:
            errors[n] = "Must be between 0 and 100"
        if f.get("specific") and not _empty(v):
            p = specificity_prompt(n, v)
            if p:
                prompts[n] = p
    if kind == "thesis":
        em = cleaned.get("emotions") or []
        if "None of these" in em and len(em) > 1:
            errors["emotions"] = "'None of these' cannot be combined with other states"
        if cleaned.get("expected_reward") and cleaned.get("expected_risk") and not cleaned.get("expected_rr"):
            cleaned["expected_rr"] = round(cleaned["expected_reward"] / cleaned["expected_risk"], 2) if cleaned["expected_risk"] else None
        if cleaned.get("expected_direction") in ("Up", "Down") and cleaned.get("direction"):
            pass  # direction/expectation consistency is reported by analytics, not blocked
    cleaned["specificity_acknowledged"] = bool(answers.get("specificity_acknowledged"))
    return {"answers": cleaned, "errors": errors, "prompts": prompts}


def _fmt(v, suffix=""):
    if v is None or v == [] or v == "":
        return "—"
    if isinstance(v, float):
        v = int(v) if v.is_integer() else round(v, 2)
    if isinstance(v, list):
        return ", ".join(v)
    return f"{v}{suffix}"


def _day_line(day: dict | None) -> str:
    if not day:
        return "—"
    if day["first_trade_of_day"] and not day["closed_before_entry"]:
        return "First trade of the day"
    pnl = day["realised_day_pnl"]
    pnl_txt = "unknown" if pnl is None else f"{'+' if pnl > 0 else ''}{_fmt(float(pnl))}"
    extra = f"{day['closed_before_entry']} closed ({day['wins']}W/{day['losses']}L)"
    if day["consecutive_losses"] >= 2:
        extra += f", {day['consecutive_losses']} losses in a row"
    if day["open_at_entry"]:
        extra += f", {day['open_at_entry']} still open"
    return f"{pnl_txt} realised · {extra}"


def snapshot_text(answers: dict, position: int, total: int, trade: dict, day: dict | None = None) -> str:
    move = answers.get("expected_move_points")
    move_txt = _fmt(move, " points") if move is not None else (
        _fmt(answers.get("expected_move_pct"), "%") if answers.get("expected_move_pct") is not None else "—")
    if answers.get("target_price") is not None:
        move_txt += f" (target {_fmt(answers['target_price'])})"
    expect = answers.get("one_sentence") or ""
    return "\n".join([
        "MY ORIGINAL TRADE THESIS", "",
        "Chronological position:", f"TRADE {position} OF {total}  ·  {trade.get('instrument')}  ·  entry {trade.get('entry_local')} @ {_fmt(trade.get('entry_price'))}", "",
        "What I saw:", answers.get("chart_observation") or "—", "",
        "I entered because:", answers.get("primary_reason") or "—", "",
        "I expected:", expect or "—", "",
        "Setup:", _fmt(answers.get("setups")), "",
        "Trade direction:", _fmt(answers.get("direction")), "",
        "Expected direction:", _fmt(answers.get("expected_direction")), "",
        "Expected move:", move_txt, "",
        "Expected timeframe:", _fmt(answers.get("expected_timeframe")), "",
        "The trade would be invalidated if:", (answers.get("invalidation") or "—") +
        (f"  (level {_fmt(answers['invalidation_price'])})" if answers.get("invalidation_price") is not None else ""), "",
        "Stop:", _fmt(answers.get("stop_price")) if answers.get("stop_price") is not None else _fmt(answers.get("stop_points"), " points"), "",
        "My confidence:", f"{_fmt(answers.get('confidence'))}/100", "",
        "Day P&L before this entry (fact):", _day_line(day), "",
        "Today's P&L on my mind:", _fmt(answers.get("day_pnl_influence")), "",
        "My emotional state:", _fmt(answers.get("emotions")), "",
        "I believed the setup was:", f"{_fmt(answers.get('setup_quality'))}/100", "",
        "I followed my rules:", _fmt(answers.get("rules_followed")),
    ])


def canonical_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
