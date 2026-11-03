"""Conservative attribution of whole words to diarization turns.

The turn track can be exclusive. Overlapping candidates are attribution
evidence, not a claim that crosstalk was detected. Inputs are never modified.
"""
from bisect import bisect_left, bisect_right
import math


UNKNOWN = "SPEAKER_UNKNOWN"
_EPSILON = 1e-9
_NEIGHBOR_GAP = 0.5
_TIMING_TOLERANCE = 0.05


def _interval(item):
    try:
        a, b = float(item[0]), float(item[1])
    except (TypeError, ValueError, IndexError, KeyError, OverflowError):
        return None
    return (a, b) if math.isfinite(a) and math.isfinite(b) and b >= a else None


def _union_length(intervals):
    total = 0.0
    end = -math.inf
    for a, b in sorted(intervals):
        total += max(0.0, b - max(a, end))
        end = max(end, b)
    return total


def assign_word_speakers(atoms, turns, margin_seconds=0.08, minimum_share=0.6):
    """Return one attribution per input atom, without splitting any word.

    A confident winner must cover ``minimum_share`` of the word and beat any
    competing speaker by ``margin_seconds``. A sole well-covered speaker does
    not need that absolute margin, so short words can still be attributed.

    A near-boundary ambiguity may inherit a label only from two agreeing,
    nearby, directly confident neighbours. Such a contextual assignment keeps
    ``uncertain=True`` so downstream output retains the uncertainty. Resolved
    neighbours never propagate a label through a chain of ambiguous words.
    """
    margin_seconds = float(margin_seconds)
    minimum_share = float(minimum_share)
    if not math.isfinite(margin_seconds) or margin_seconds < 0:
        raise ValueError("Speaker ambiguity margin must be finite and non-negative.")
    if not math.isfinite(minimum_share) or not 0 <= minimum_share <= 1:
        raise ValueError("Speaker minimum share must be between zero and one.")

    atoms = list(atoms)
    cleaned = []
    for turn in turns:
        bounds = _interval(turn)
        try:
            speaker = turn[2]
        except (TypeError, IndexError, KeyError):
            continue
        if not bounds or bounds[1] <= bounds[0] or not speaker or speaker == UNKNOWN:
            continue
        cleaned.append((*bounds, str(speaker)))
    cleaned.sort()
    starts = [turn[0] for turn in cleaned]
    furthest_ends = []
    for _, end, _ in cleaned:
        furthest_ends.append(max(end, furthest_ends[-1] if furthest_ends else -math.inf))

    intervals = []
    assignments = []
    for atom in atoms:
        bounds = _interval((atom.get("start"), atom.get("end")))
        intervals.append(bounds)
        result = {"speaker": UNKNOWN, "uncertain": True, "candidates": [],
                  "reason": "invalid_timing"}
        assignments.append(result)
        if bounds is None:
            continue
        a, b = bounds
        if b <= a:
            result["reason"] = "zero_duration"
            continue

        by_speaker = {}
        left = bisect_right(furthest_ends, a)
        right = bisect_left(starts, b)
        for start, end, speaker in cleaned[left:right]:
            if end > a:
                by_speaker.setdefault(speaker, []).append((max(a, start), min(b, end)))
        scores = sorted(((speaker, _union_length(spans)) for speaker, spans in by_speaker.items()),
                        key=lambda item: (-item[1], item[0]))
        result["candidates"] = [{"speaker": speaker, "overlap": round(overlap, 6),
                                 "share": round(min(1.0, overlap / (b - a)), 6)}
                                for speaker, overlap in scores]
        if not scores:
            result["reason"] = "no_speaker_evidence"
            continue
        if atom.get("aligned") is False and len(scores) > 1:
            result["reason"] = "unaligned_multiple_speakers"
            continue
        speaker, coverage = scores[0]
        if coverage / (b - a) + _EPSILON < minimum_share:
            result["reason"] = "insufficient_turn_coverage"
        elif len(scores) > 1 and (coverage - scores[1][1] <= _EPSILON
                                 or coverage - scores[1][1] + _EPSILON < margin_seconds):
            result["reason"] = "boundary_ambiguous"
        else:
            result.update(speaker=speaker, uncertain=False, reason="dominant_turn")

    # Use only the original direct decisions, never an earlier repaired word.
    direct = [dict(result) for result in assignments]
    for index in range(1, len(atoms) - 1):
        current = assignments[index]
        if current["reason"] not in {"boundary_ambiguous", "insufficient_turn_coverage"}:
            continue
        previous, following = direct[index - 1], direct[index + 1]
        if previous["uncertain"] or following["uncertain"] or previous["speaker"] != following["speaker"]:
            continue
        if any(atoms[i].get("aligned") is False for i in (index - 1, index, index + 1)):
            continue
        before, here, after = intervals[index - 1:index + 2]
        if not before or not here or not after:
            continue
        if not (-_TIMING_TOLERANCE <= here[0] - before[1] <= _NEIGHBOR_GAP
                and -_TIMING_TOLERANCE <= after[0] - here[1] <= _NEIGHBOR_GAP):
            continue
        speaker = previous["speaker"]
        if not any(candidate["speaker"] == speaker for candidate in current["candidates"]):
            continue
        current.update(speaker=speaker, reason="neighboring_words_agree")
    return assignments
