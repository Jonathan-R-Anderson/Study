#!/usr/bin/env python3
"""
SM20 scheduling helpers for the flashcard app.

This module keeps the reverse-engineered interval math separate from the
Tkinter UI and adds a small amount of application-specific state management
around it so each card can persist its own scheduling inputs and review log.
"""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime, timedelta


RELEARN_DELAY_MINUTES = 10
MAX_HISTORY_ENTRIES = 40
DEFAULT_SM20_VERSION = 2
DEFAULT_SM20_FLAGS = 4
CORRECT_QUALITY = 0.95
WRONG_QUALITY = 0.15


def exp2_clamped(x):
    return 2.0 ** max(-38.0, min(38.0, x))


def exp2_full(x):
    return 2.0 ** x


def pow2(x, y):
    if x <= 0:
        return 0.0
    return x ** y


def weight(x, y):
    total = x + y
    if total == 0:
        return 0.0
    return x / total


def sign_flip_xor(value, xor_key):
    return -value


def clamp(value, lower, upper):
    return max(lower, min(upper, value))


V2 = {
    "c1": 9.29,
    "c2": 1.3,
    "c3": 1.0,
    "c4": -0.08,
    "c5": -0.31,
    "c6": 1.04,
    "c7": 0.07,
    "c8": -1.88,
    "c9": 1.58,
    "c10": 600.0,
}

INIT = {
    "iv1": 15.0,
    "iv2": 3.0,
    "iv3": 1.0,
    "iv4": -0.08,
    "iv5": -0.35,
    "iv6": -2.0,
    "iv7": 2.25,
    "iv8": 600.0,
}

STAB_PRE = {
    "lower": -1.0,
    "cap1": 0.7,
    "cap2": 44530.0,
}

ROUND = {
    "flag0_upper": 2.0,
    "flag0_lower": 0.5,
    "flag2_upper": 20.0,
    "flag2_lower": 0.8,
}

IDX = {
    "stability_offset": 2.0,
    "stability_min": 0.0,
    "stability_power": 1.0,
    "stability_denom": 2.90396936502257,
    "a_factor_offset": 2.0,
    "a_factor_power": 2.90396936502257,
    "difficulty_scale": 19,
    "difficulty_max": 1.0,
    "difficulty_min": 0.0,
    "repetition_divisor": 19.0,
    "retrievability_scale": 20.0,
    "retrievability_min": 0.0,
}

V4 = {
    "extra": 1.0,
}

V6 = {
    "xor_key": -0.0,
}


def safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def round_float(value, digits=4):
    return round(float(value), digits)


def stability_pretransform(stability):
    if math.isnan(stability) or math.isinf(stability):
        return STAB_PRE["cap2"], True
    if stability <= STAB_PRE["lower"]:
        return stability, False
    if stability < STAB_PRE["cap1"]:
        return STAB_PRE["cap1"], True
    if stability <= STAB_PRE["cap2"]:
        return stability, False
    return STAB_PRE["cap2"], True


def difficulty_to_index(difficulty):
    if difficulty < IDX["difficulty_min"]:
        return 10
    if difficulty > IDX["difficulty_max"]:
        return 10
    return math.floor(difficulty * IDX["difficulty_scale"]) + 1


def repetition_to_index(rep):
    return clamp(rep - 1, 0, 19) / IDX["repetition_divisor"]


def stability_to_index(stability):
    stability, _error = stability_pretransform(stability)
    diff = max(IDX["stability_min"], stability - IDX["stability_offset"])
    exponent = IDX["stability_power"] / IDX["stability_denom"]
    result = diff ** exponent
    return clamp(math.floor(result) + 1, 1, 20)


def a_factor_to_value(a_idx):
    return pow2(a_idx - 1, IDX["a_factor_power"]) + IDX["a_factor_offset"]


def retrievability_to_index(retrievability):
    result = math.floor(exp2_full(max(IDX["retrievability_min"], retrievability) * IDX["retrievability_scale"]))
    return clamp(result, 0, 20)


def apply_rounding(interval, flags):
    if flags >= 4 or (flags & 2) != 0:
        if interval > ROUND["flag2_upper"]:
            interval = ROUND["flag2_upper"]
        if interval <= ROUND["flag2_lower"] and interval != ROUND["flag2_lower"]:
            interval = ROUND["flag2_lower"]
    else:
        if interval > ROUND["flag0_upper"]:
            interval = ROUND["flag0_upper"]
        if interval <= ROUND["flag0_lower"] and interval != ROUND["flag0_lower"]:
            interval = ROUND["flag0_lower"]
    return interval


def compute_interval_v2(rep_fraction, stability_transformed, difficulty_fraction):
    stability_scale = V2["c2"] + (V2["c1"] - V2["c2"]) * (V2["c3"] - rep_fraction)
    rep_power = V2["c4"] + rep_fraction * (V2["c5"] - V2["c4"])
    rep_factor = stability_transformed ** rep_power
    base = (stability_scale - V2["c6"]) * rep_factor + V2["c7"]
    penalty = min(V2["c10"], rep_fraction * V2["c8"] + V2["c9"])
    exponent = sign_flip_xor(penalty, V6["xor_key"]) * difficulty_fraction
    return base * exp2_clamped(exponent)


def compute_initial_interval(rep_fraction, stability_transformed, difficulty_fraction):
    stability_scale = INIT["iv2"] + (INIT["iv1"] - INIT["iv2"]) * (INIT["iv3"] - rep_fraction)
    rep_power = INIT["iv4"] + rep_fraction * (INIT["iv5"] - INIT["iv4"])
    rep_factor = stability_transformed ** rep_power
    base = (stability_scale - INIT["iv3"]) * rep_factor + INIT["iv3"]
    penalty = min(INIT["iv8"], rep_fraction * INIT["iv6"] + INIT["iv7"])
    exponent = sign_flip_xor(penalty, V6["xor_key"]) * difficulty_fraction
    interval = base * exp2_clamped(exponent)
    interval = apply_rounding(interval, DEFAULT_SM20_FLAGS)
    return max(1.0, interval)


def compute_interval_v4(p1, p2, p3, p4, p5, p6, p7):
    return (p3 * p5 + V4["extra"]) * (p1 * p7 + p2) + p4


def compute_interval_v6(p1, p2, p3, p4, p5, p6):
    return p4 + p1 * exp2_full(p6) * exp2_full(sign_flip_xor(p3, V6["xor_key"]) * p5)


def default_sm20_state():
    return {
        "version": DEFAULT_SM20_VERSION,
        "stability": 1.0,
        "difficulty": 0.35,
        "retrievability": 0.0,
        "quality": 0.0,
        "repetition": 0,
        "a_factor": 2.0,
        "flags": DEFAULT_SM20_FLAGS,
        "last_interval_days": 0.0,
        "next_interval_days": 1.0,
        "last_result": "new",
        "history": [],
    }


def estimate_initial_difficulty(difficulty_score=0.0, difficulty_label="Easy", review_count=0, missed_count=0):
    label_bias = {
        "Easy": 0.0,
        "Medium": 0.12,
        "Hard": 0.24,
    }.get(str(difficulty_label or "Easy"), 0.0)
    miss_ratio = (missed_count / review_count) if review_count else 0.0
    score_bias = clamp(safe_float(difficulty_score, 0.0) / 60.0, 0.0, 0.30)
    return clamp(0.22 + label_bias + miss_ratio * 0.35 + score_bias, 0.05, 0.95)


def estimate_initial_stability(interval_days=0.0, repetitions=0, correct_count=0):
    base_interval = max(1.0, safe_float(interval_days, 0.0))
    repetitions = max(0, safe_int(repetitions, 0))
    correct_count = max(0, safe_int(correct_count, 0))
    estimate = base_interval * (1.0 + min(repetitions, 10) * 0.08) + min(correct_count, 20) * 0.2
    return clamp(estimate, 1.0, STAB_PRE["cap2"])


def normalize_history(history):
    normalized = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        result = str(item.get("result") or "new").lower()
        if result not in {"correct", "wrong", "new"}:
            result = "correct" if result in {"right", "good", "easy"} else "wrong"
        normalized.append(
            {
                "reviewed_at": str(item.get("reviewed_at") or ""),
                "result": result,
                "quality": round_float(clamp(safe_float(item.get("quality"), 0.0), 0.0, 1.0), 3),
                "retrievability": round_float(clamp(safe_float(item.get("retrievability"), 0.0), 0.0, 1.0), 3),
                "scheduled_days": round_float(max(0.0, safe_float(item.get("scheduled_days"), 0.0))),
                "interval_days": round_float(max(0.0, safe_float(item.get("interval_days"), 0.0))),
                "stability": round_float(clamp(safe_float(item.get("stability"), 1.0), 1.0, STAB_PRE["cap2"]), 3),
                "difficulty": round_float(clamp(safe_float(item.get("difficulty"), 0.35), 0.0, 1.0), 3),
            }
        )
    return normalized[-MAX_HISTORY_ENTRIES:]


def normalize_sm20_state(
    existing=None,
    *,
    difficulty_score=0.0,
    difficulty_label="Easy",
    review_count=0,
    missed_count=0,
    repetitions=0,
    interval_days=0.0,
    correct_count=0,
):
    state = default_sm20_state()
    existing = existing if isinstance(existing, dict) else {}

    state["version"] = safe_int(existing.get("version"), DEFAULT_SM20_VERSION)
    if state["version"] not in {2, 4, 6}:
        state["version"] = DEFAULT_SM20_VERSION

    derived_difficulty = estimate_initial_difficulty(
        difficulty_score=difficulty_score,
        difficulty_label=difficulty_label,
        review_count=review_count,
        missed_count=missed_count,
    )
    derived_stability = estimate_initial_stability(
        interval_days=interval_days,
        repetitions=repetitions,
        correct_count=correct_count,
    )

    state["difficulty"] = clamp(safe_float(existing.get("difficulty"), derived_difficulty), 0.0, 1.0)
    state["stability"] = clamp(safe_float(existing.get("stability"), derived_stability), 1.0, STAB_PRE["cap2"])
    state["retrievability"] = clamp(safe_float(existing.get("retrievability"), 0.0), 0.0, 1.0)
    state["quality"] = clamp(safe_float(existing.get("quality"), 0.0), 0.0, 1.0)
    state["repetition"] = max(0, safe_int(existing.get("repetition"), repetitions))
    state["a_factor"] = clamp(safe_float(existing.get("a_factor"), 1.3 + (1.0 - state["difficulty"]) * 1.7), 1.3, 3.0)
    state["flags"] = max(0, safe_int(existing.get("flags"), DEFAULT_SM20_FLAGS))
    state["last_interval_days"] = max(0.0, safe_float(existing.get("last_interval_days"), interval_days))
    default_next = state["last_interval_days"] or 1.0
    state["next_interval_days"] = max(0.0, safe_float(existing.get("next_interval_days"), default_next))
    state["last_result"] = str(existing.get("last_result") or "new").lower()
    if state["last_result"] not in {"new", "correct", "wrong"}:
        state["last_result"] = "new"
    state["history"] = normalize_history(existing.get("history"))
    return state


def serialize_sm20_state(state):
    return {
        "version": safe_int(state.get("version"), DEFAULT_SM20_VERSION),
        "stability": round_float(clamp(safe_float(state.get("stability"), 1.0), 1.0, STAB_PRE["cap2"]), 3),
        "difficulty": round_float(clamp(safe_float(state.get("difficulty"), 0.35), 0.0, 1.0), 3),
        "retrievability": round_float(clamp(safe_float(state.get("retrievability"), 0.0), 0.0, 1.0), 3),
        "quality": round_float(clamp(safe_float(state.get("quality"), 0.0), 0.0, 1.0), 3),
        "repetition": max(0, safe_int(state.get("repetition"), 0)),
        "a_factor": round_float(clamp(safe_float(state.get("a_factor"), 2.0), 1.3, 3.0), 3),
        "flags": max(0, safe_int(state.get("flags"), DEFAULT_SM20_FLAGS)),
        "last_interval_days": round_float(max(0.0, safe_float(state.get("last_interval_days"), 0.0))),
        "next_interval_days": round_float(max(0.0, safe_float(state.get("next_interval_days"), 1.0))),
        "last_result": str(state.get("last_result") or "new"),
        "history": normalize_history(state.get("history")),
    }


def elapsed_days_since(last_reviewed, now=None):
    if not last_reviewed:
        return 0.0
    now = now or datetime.now()
    if isinstance(last_reviewed, str):
        try:
            last_reviewed = datetime.fromisoformat(last_reviewed)
        except ValueError:
            return 0.0
    delta = now - last_reviewed
    return max(0.0, delta.total_seconds() / 86400.0)


def backfill_sm20_state(
    state,
    *,
    correct_count=0,
    missed_count=0,
    review_count=0,
    interval_days=0.0,
    last_reviewed=None,
    due_at=None,
    now=None,
):
    state = serialize_sm20_state(state)
    if review_count <= 0:
        return state

    now = now or datetime.now()
    interval_days = max(0.0, safe_float(interval_days, 0.0))
    state["last_interval_days"] = max(state["last_interval_days"], interval_days)
    state["next_interval_days"] = max(state["next_interval_days"], interval_days)

    if state["last_result"] == "new":
        state["last_result"] = "correct" if correct_count >= missed_count else "wrong"

    if state["quality"] <= 0.0:
        state["quality"] = CORRECT_QUALITY if state["last_result"] == "correct" else WRONG_QUALITY

    if state["retrievability"] <= 0.0:
        reviewed_at = None
        due_at_dt = None
        try:
            reviewed_at = datetime.fromisoformat(last_reviewed) if last_reviewed else None
        except ValueError:
            reviewed_at = None
        try:
            due_at_dt = datetime.fromisoformat(due_at) if due_at else None
        except ValueError:
            due_at_dt = None

        if reviewed_at and due_at_dt and due_at_dt > reviewed_at:
            total = (due_at_dt - reviewed_at).total_seconds()
            elapsed = clamp((now - reviewed_at).total_seconds(), 0.0, total)
            remaining_ratio = 1.0 - (elapsed / total if total else 1.0)
            state["retrievability"] = round_float(clamp(0.35 + remaining_ratio * 0.64, 0.0, 0.99), 3)
        else:
            state["retrievability"] = 0.99 if state["last_result"] == "correct" else 0.0

    if not state["history"] and last_reviewed:
        scheduled_days = max(interval_days, state["next_interval_days"], state["last_interval_days"])
        state["history"] = [
            {
                "reviewed_at": str(last_reviewed),
                "result": state["last_result"],
                "quality": round_float(state["quality"], 3),
                "retrievability": round_float(state["retrievability"], 3),
                "scheduled_days": round_float(scheduled_days),
                "interval_days": round_float(scheduled_days),
                "stability": round_float(state["stability"], 3),
                "difficulty": round_float(state["difficulty"], 3),
            }
        ]

    return serialize_sm20_state(state)


def estimate_retrievability(state, elapsed_days):
    if elapsed_days <= 0:
        if safe_int(state.get("repetition"), 0) <= 0:
            return 0.0
        return clamp(safe_float(state.get("retrievability"), 0.95), 0.0, 0.9999)
    stability = max(1.0, safe_float(state.get("stability"), 1.0))
    return clamp(exp2_clamped(-(elapsed_days / stability)), 0.0, 0.9999)


def format_interval_label(days):
    days = max(0.0, safe_float(days, 0.0))
    total_minutes = round(days * 1440)
    if total_minutes <= 0:
        return "now"
    if total_minutes < 60:
        return f"{total_minutes}m"
    if total_minutes < 1440:
        hours = max(1, round(total_minutes / 60))
        return f"{hours}h"
    if abs(days - round(days)) < 0.05:
        return f"{int(round(days))}d"
    return f"{days:.1f}d"


def _sm20_transforms(state):
    repetition = max(1, min(20, safe_int(state.get("repetition"), 0) + 1))
    rep_fraction = repetition_to_index(repetition)
    stability_index = stability_to_index(safe_float(state.get("stability"), 1.0))
    stability_transformed = a_factor_to_value(stability_index)
    difficulty_fraction = clamp(safe_float(state.get("difficulty"), 0.35), 0.0, 1.0)
    return repetition, rep_fraction, stability_transformed, difficulty_fraction


def _correct_interval_days(state):
    repetition, rep_fraction, stability_transformed, difficulty_fraction = _sm20_transforms(state)
    scheduled_days = max(
        1.0,
        safe_float(state.get("next_interval_days"), 0.0),
        safe_float(state.get("last_interval_days"), 0.0),
    )

    if safe_int(state.get("repetition"), 0) <= 0:
        return compute_initial_interval(rep_fraction, stability_transformed, difficulty_fraction)

    ufactor = compute_interval_v2(rep_fraction, stability_transformed, difficulty_fraction)
    return max(1.0, scheduled_days * max(1.05, ufactor))


def _updated_difficulty(state, was_correct, retrievability):
    difficulty = clamp(safe_float(state.get("difficulty"), 0.35), 0.0, 1.0)
    if was_correct:
        difficulty -= 0.04
        difficulty += max(0.0, 0.55 - retrievability) * 0.03
    else:
        difficulty += 0.10
        difficulty += max(0.0, 0.70 - retrievability) * 0.06
    return clamp(difficulty, 0.0, 1.0)


def _updated_stability(state, was_correct, interval_days, retrievability):
    stability = clamp(safe_float(state.get("stability"), 1.0), 1.0, STAB_PRE["cap2"])
    difficulty = clamp(safe_float(state.get("difficulty"), 0.35), 0.0, 1.0)
    repetition = max(0, safe_int(state.get("repetition"), 0))
    if was_correct:
        growth = 1.10 + (1.0 - difficulty) * 0.40 + min(repetition, 12) * 0.04
        growth += retrievability * 0.15
        stability = stability * growth + interval_days * 0.10
    else:
        stability = stability * (0.45 - difficulty * 0.15) + 0.75
    return clamp(stability, 1.0, STAB_PRE["cap2"])


def score_sm20_review(state, was_correct, *, now=None, elapsed_days=None, record_history=True):
    now = now or datetime.now()
    current = serialize_sm20_state(state)
    if elapsed_days is None:
        history = current.get("history") or []
        last_reviewed = history[-1].get("reviewed_at") if history else None
        elapsed_days = elapsed_days_since(last_reviewed, now)
    elapsed_days = max(0.0, safe_float(elapsed_days, 0.0))
    retrievability = estimate_retrievability(current, elapsed_days)
    quality = CORRECT_QUALITY if was_correct else WRONG_QUALITY

    next_interval_days = (
        RELEARN_DELAY_MINUTES / 1440.0 if not was_correct else _correct_interval_days(current)
    )
    updated_state = deepcopy(current)
    updated_state["quality"] = quality
    updated_state["retrievability"] = 0.99 if was_correct else 0.0
    updated_state["last_interval_days"] = next_interval_days
    updated_state["next_interval_days"] = next_interval_days
    updated_state["last_result"] = "correct" if was_correct else "wrong"
    updated_state["difficulty"] = _updated_difficulty(current, was_correct, retrievability)
    updated_state["stability"] = _updated_stability(current, was_correct, next_interval_days, retrievability)
    updated_state["a_factor"] = clamp(1.3 + (1.0 - updated_state["difficulty"]) * 1.7, 1.3, 3.0)
    updated_state["repetition"] = current["repetition"] + 1 if was_correct else 0

    if record_history:
        history = list(updated_state.get("history") or [])
        history.append(
            {
                "reviewed_at": now.isoformat(timespec="seconds"),
                "result": "correct" if was_correct else "wrong",
                "quality": round_float(quality, 3),
                "retrievability": round_float(retrievability, 3),
                "scheduled_days": round_float(max(
                    safe_float(current.get("next_interval_days"), 0.0),
                    safe_float(current.get("last_interval_days"), 0.0),
                )),
                "interval_days": round_float(next_interval_days),
                "stability": round_float(updated_state["stability"], 3),
                "difficulty": round_float(updated_state["difficulty"], 3),
            }
        )
        updated_state["history"] = history[-MAX_HISTORY_ENTRIES:]

    return {
        "state": serialize_sm20_state(updated_state),
        "quality": round_float(quality, 3),
        "retrievability": round_float(retrievability, 3),
        "interval_days": round_float(next_interval_days),
        "interval_label": format_interval_label(next_interval_days),
        "due_at": (now + timedelta(days=next_interval_days)).isoformat(timespec="seconds"),
        "last_reviewed": now.isoformat(timespec="seconds"),
        "result": "correct" if was_correct else "wrong",
    }


def preview_sm20_review(state, was_correct, *, now=None, elapsed_days=None):
    return score_sm20_review(
        state,
        was_correct,
        now=now,
        elapsed_days=elapsed_days,
        record_history=False,
    )
