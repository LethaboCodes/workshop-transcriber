"""Conservative context for language labels and pre-translation sentence rows.

Pure Python: no model calls, cache writes, or edits to imported transcripts.
Recognition wording is immutable here; uncertainty is recorded rather than guessed.
"""
from bisect import bisect_left, bisect_right
from copy import deepcopy
from difflib import SequenceMatcher
import math
import json
import re


UNKNOWN = "SPEAKER_UNKNOWN"


def _tokens(text):
    return re.findall(r"[^\W_]+", str(text).casefold(), re.UNICODE)


def _sentence_end(text):
    return bool(re.search(r"[.!?…][\"')\]]*$", str(text).strip()))


def _flags(row):
    return set(row.get("quality_flags") or [])


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


class _EvidenceIndex:
    def __init__(self, lanes, allowed):
        self.lanes = []
        self.unaligned = {}
        unique = {}
        for lane in lanes:
            atoms = sorted((atom for atom in lane.get("atoms", [])
                            if _finite(atom.get("start")) and _finite(atom.get("end"))
                            and atom["end"] >= atom["start"]),
                           key=lambda atom: (atom["start"] + atom["end"]) / 2)
            self.lanes.append((lane, atoms, [(atom["start"] + atom["end"]) / 2 for atom in atoms]))
            untimed = sorted((atom for atom in atoms if not atom.get("aligned")), key=lambda atom: atom["start"])
            self.unaligned[id(lane)] = (untimed, [atom["start"] for atom in untimed],
                                        max((atom["end"] - atom["start"] for atom in untimed), default=0))
            for item in lane.get("evidence", []):
                if not (_finite(item.get("start")) and _finite(item.get("end"))
                        and item["end"] > item["start"]):
                    continue
                probabilities = {language: float(value) for language, value in item.get("probabilities", {}).items()
                                 if language in allowed and _finite(value) and 0 <= value <= 1}
                if probabilities:
                    # Forced passes repeat the same independent acoustic probe.
                    key = (lane.get("model"), lane.get("audio_version"), item["start"], item["end"],
                           tuple(sorted(probabilities.items())))
                    unique[key] = {"start": item["start"], "end": item["end"], "probabilities": probabilities}
        self.probes = sorted(unique.values(), key=lambda item: item["start"])
        self.starts = [item["start"] for item in self.probes]
        self.max_probe = max((item["end"] - item["start"] for item in self.probes), default=0)

    def probes_for(self, start, end):
        left = bisect_left(self.starts, start - self.max_probe)
        right = bisect_left(self.starts, end)
        return [item for item in self.probes[left:right] if item["end"] > start]

    def probabilities(self, start, end):
        sums, weights = {}, {}
        for item in self.probes_for(start, end):
            weight = max(0, min(end, item["end"]) - max(start, item["start"]))
            for language, probability in item["probabilities"].items():
                sums[language] = sums.get(language, 0) + probability * weight
                weights[language] = weights.get(language, 0) + weight
        return {language: round(value / weights[language], 4) for language, value in sums.items() if weights[language]}

    def atoms_for(self, atoms, midpoints, start, end, padding=0):
        return atoms[bisect_left(midpoints, start - padding):bisect_right(midpoints, end + padding)]

    def unaligned_for(self, lane, start, end):
        atoms, starts, longest = self.unaligned[id(lane)]
        return [atom for atom in atoms[bisect_left(starts, start - longest):bisect_left(starts, end)] if atom["end"] > start]

    def support(self, text, start, end, target, preferred, minimum_ratio=.85, padding=.18):
        wanted = _tokens(text)
        models, native = set(), False
        best = 0.0
        if not wanted:
            return models, native, best
        for lane, atoms, midpoints in self.lanes:
            selected = self.atoms_for(atoms, midpoints, start, end, padding)
            selected = [atom for atom in selected if atom.get("aligned")]
            actual = _tokens(" ".join(atom.get("text", atom.get("word", "")) for atom in selected))
            if not actual:
                continue
            ratio = SequenceMatcher(None, wanted, actual, autojunk=False).ratio()
            best = max(best, ratio)
            if ratio >= minimum_ratio:
                models.add(lane.get("model"))
                if lane.get("language") == target and lane.get("model") == preferred.get(target):
                    native = True
        models.discard(None)
        return models, native, best


def _rank(probabilities):
    ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        return None, 0.0, 0.0
    language, probability = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    return language, probability, second


def _decisive(probabilities):
    language, first, second = _rank(probabilities)
    return language if first >= .65 and first - second >= .25 else None


def _context_span(rows, index, seconds):
    row = rows[index]
    start, end, text = row["start"], row["end"], row.get("text", "")
    left = index
    while left > 0:
        previous = rows[left - 1]
        if (start - previous["end"] > .8 or previous["end"] - start > .08
                or row["start"] - previous["start"] > seconds or _sentence_end(previous.get("text", ""))):
            break
        start = previous["start"]
        text = previous.get("text", "").strip() + " " + text
        left -= 1
    right = index + 1
    while right < len(rows) and not _sentence_end(text):
        following = rows[right]
        if (following["start"] - end > .8 or end - following["start"] > .08
                or following["end"] - row["end"] > seconds):
            break
        end = following["end"]
        text = text + " " + following.get("text", "").strip()
        right += 1
    return start, end, text.strip()


def contextualize_rows(rows, lanes, preferred, context_seconds=6):
    """Corroborate language independently of a forced decoder's source label.

    Strong local acoustic evidence protects real language switches. A weak probe
    can relabel wording only with bounded phrase context, a matching preferred
    decoder for the proposed language, and agreement from two distinct models.
    No language is inferred from a fixed vocabulary or a recording-wide majority.
    """
    output = deepcopy(rows)
    evidence = _EvidenceIndex(lanes, preferred)
    for index, row in enumerate(output):
        if not _tokens(row.get("text", "")):
            continue
        original = row.get("language")
        start, end = row["start"], row["end"]
        local = evidence.probabilities(start, end)
        context_start, context_end, context_text = _context_span(rows, index, context_seconds)
        contextual = evidence.probabilities(context_start, context_end)
        target, probability, second = _rank(contextual)
        local_target, local_probability, local_second = _rank(local)
        local_decisive = _decisive(local)
        decisive_inside = {_decisive(probe["probabilities"]) for probe in evidence.probes_for(start, end)} - {None}
        flags = _flags(row) - {"language_uncertain"}
        changed = False
        reason = "insufficient_independent_language_evidence"
        support_models, native, agreement = set(), False, 0.0

        if len(decisive_inside) > 1:
            flags.update({"language_uncertain", "language_switch_unresolved"})
            reason = "conflicting_local_language_probes"
        elif local_decisive == original:
            reason = "confirmed_by_local_acoustics"
        else:
            # A local switch can override the surrounding phrase, never vice versa.
            if local_decisive:
                target, probability, second = local_target, local_probability, local_second
                context_start, context_end, context_text = start, end, row["text"]
            if target:
                support_models, native, agreement = evidence.support(
                    context_text, context_start, context_end, target, preferred)
            acoustic_support = (target is not None and probability >= .25 and probability - second >= .15
                                and (second <= 0 or probability >= 2.5 * second))
            local_agrees = (local_target == target and local.get(target, 0) >= .20
                            and (not local_decisive or local_decisive == target))
            enough_context = len(_tokens(context_text)) >= (2 if local_decisive else 6)
            # The actual short row must also agree with the language's own decoder.
            local_padding = .04 if len(_tokens(row["text"])) <= 2 else .12
            _, local_native, _ = evidence.support(row["text"], start, end, target, preferred, .8, local_padding) if target else (set(), False, 0)
            corroborated = (acoustic_support and local_agrees and enough_context and native and local_native
                            and len(support_models) >= 2)
            if corroborated:
                row["language"] = target
                row["suggested_language"] = target
                changed = target != original
                reason = "context_acoustics_and_independent_wording"
                if changed:
                    row["recognition_language"] = original
            else:
                flags.add("language_uncertain")
        row["quality_flags"] = sorted(flags)
        row["language_context"] = {
            "policy": "context-language-v1", "original_language": original,
            "language": row.get("language"), "changed": changed, "reason": reason,
            "local_probabilities": local, "context_probabilities": contextual,
            "context_start": context_start, "context_end": context_end,
            "support_models": sorted(support_models), "wording_agreement": round(agreement, 4),
            "uncertain": "language_uncertain" in flags,
        }
    return output


def _confident_speakers(row):
    explicit = set(row.get("confident_speakers") or []) - {UNKNOWN, "", None}
    if explicit:
        return explicit
    if row.get("speaker") not in {UNKNOWN, "", None} and not row.get("speaker_uncertain") and "speaker_uncertain" not in _flags(row):
        return {row["speaker"]}
    return {word["speaker"] for word in row.get("words", [])
            if word.get("speaker") not in {UNKNOWN, "", None} and not word.get("speaker_uncertain")}


def _short_uncertain(row):
    return (row.get("speaker") in {UNKNOWN, None, ""} or row.get("speaker_uncertain") or "speaker_uncertain" in _flags(row)) and len(_tokens(row.get("text", ""))) <= 3 and row["end"] - row["start"] <= 1.2


def translation_units(rows, max_seconds=30, max_gap=.8):
    """Plan exact source-row units, never proportional target-language slices."""
    units = []
    for index, row in enumerate(rows):
        speakers = _confident_speakers(row)
        flags = _flags(row)
        uncertain = bool(row.get("speaker_uncertain") or "speaker_uncertain" in flags or row.get("speaker") in {UNKNOWN, None, ""})
        eligible = bool(row.get("words")) and not flags.intersection({"language_uncertain", "language_switch_unresolved", "word_alignment_unavailable", "competing_transcription"})
        previous = units[-1] if units else None
        join = (previous is not None and eligible and previous["eligible"]
                and previous["language"] == row.get("language")
                and -.08 <= row["start"] - previous["end"] <= max_gap
                and row["end"] - previous["start"] <= max_seconds
                and len(_tokens(previous["text"])) + len(_tokens(row.get("text", ""))) <= 120
                and not _sentence_end(previous["text"])
                and len(set(previous["confident_speakers"]) | speakers) <= 1
                and bool(previous["confident_speakers"])
                and (not uncertain or _short_uncertain(row)))
        if join:
            previous["row_indices"].append(index)
            previous["text"] = (previous["text"].rstrip() + " " + row.get("text", "").lstrip()).strip()
            previous["end"] = max(previous["end"], row["end"])
            previous["confident_speakers"] = sorted(set(previous["confident_speakers"]) | speakers)
            previous["speaker_uncertain"] = previous["speaker_uncertain"] or uncertain
            if previous["speaker_uncertain"]:
                previous["speaker"] = UNKNOWN
        else:
            units.append({"row_indices": [index], "start": row["start"], "end": row["end"],
                          "text": row.get("text", ""), "language": row.get("language"),
                          "speaker": row.get("speaker", UNKNOWN), "speaker_uncertain": uncertain,
                          "confident_speakers": sorted(speakers), "eligible": eligible})
    for unit in units:
        unit["context_limited"] = unit["language"] != "en" and len(_tokens(unit["text"])) < 3
    return units


def _source_rows(rows):
    originals = []
    for row in rows:
        if row.get("source_rows"):
            originals.extend(deepcopy(row["source_rows"]))
        else:
            originals.append({key: deepcopy(row[key]) for key in (
                "start", "end", "text", "language", "speaker", "model", "audio_version", "confidence",
                "words", "alternates", "recognition_attempt", "quality_flags", "speaker_uncertain",
                "confident_speakers", "language_context", "recognition_language", "selection", "unresolved_spans") if key in row})
    return originals


def _alternative_spans(evidence, start, end, source_rows, selected_text, selected_language):
    complete, partial = [], []
    target_tokens = _tokens(selected_text)
    for lane, atoms, midpoints in evidence.lanes:
        selected = evidence.atoms_for(atoms, midpoints, start, end)
        selected = [atom for atom in selected if atom.get("aligned")]
        # Untimed phrases may extend beyond this sentence. Preserve the entire
        # phrase as unresolved provenance, never clip it by estimated characters.
        unaligned = evidence.unaligned_for(lane, start, end)
        for atom in unaligned:
            partial.append({**{key: lane.get(key) for key in ("language", "model", "audio_version", "attempt")},
                            "text": atom.get("text", ""), "start": atom["start"], "end": atom["end"],
                            "words": [], "partial": True, "replaceable": False, "source": atom.get("source"),
                            "reason": "unaligned_competing_span"})
        if not selected:
            continue
        text = " ".join(atom.get("text", atom.get("word", "")).strip() for atom in selected).strip()
        if not _tokens(text):
            continue
        candidate = {**{key: lane.get(key) for key in ("language", "model", "audio_version", "attempt")},
                     "text": text, "start": start, "end": end,
                     "replaceable": True,
                     "words": [{"word": atom.get("text", atom.get("word", "")), "start": atom["start"], "end": atom["end"],
                                "probability": atom.get("probability", 0)} for atom in selected],
                     "confidence": round(sum(atom.get("probability", 0) for atom in selected) / len(selected), 4)}
        covered = all(any(atom["start"] < row["end"] and atom["end"] > row["start"] for atom in selected) for row in source_rows if _tokens(row.get("text", "")))
        enough_text = len(_tokens(text)) >= max(1, len(target_tokens) * .5)
        spans_ends = selected[0]["start"] <= start + .6 and selected[-1]["end"] >= end - .6
        if covered and enough_text and spans_ends:
            if not (_tokens(text) == target_tokens and lane.get("language") == selected_language):
                complete.append(candidate)
        else:
            partial.append({**candidate, "partial": True, "replaceable": False,
                            "start": min(atom["start"] for atom in selected), "end": max(atom["end"] for atom in selected),
                            "reason": "incomplete_sentence_coverage"})
    return complete, partial


def _unique(items):
    unique = {}
    for item in items:
        # Reference identity preserves bounds and provenance, not just the words.
        key = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        unique.setdefault(key, item)
    return list(unique.values())


def regroup_rows(rows, lanes, max_seconds=30, max_gap=.8):
    """Build full pre-translation rows from exact phrase units.

    One translation belongs to one resulting row. Confident different speakers
    and language switches never merge. Absorbing a short ambiguous word makes
    the WHOLE row speaker-uncertain while retaining every word's attribution and
    all source rows. Incomplete alternative spans remain provenance, not an
    unsafe whole-row replacement.
    """
    evidence = _EvidenceIndex(lanes, {row.get("language") for row in rows})
    output = []
    for unit in translation_units(rows, max_seconds, max_gap):
        parts = [rows[index] for index in unit["row_indices"]]
        result = deepcopy(parts[0])
        if len(parts) > 1:
            result.update(start=unit["start"], end=unit["end"], text=unit["text"],
                          speaker=unit["speaker"], speaker_uncertain=unit["speaker_uncertain"],
                          confident_speakers=unit["confident_speakers"])
            words = []
            for row in parts:
                for old in row.get("words", []):
                    word = deepcopy(old)
                    word.setdefault("speaker", row.get("speaker", UNKNOWN))
                    word.setdefault("speaker_uncertain", bool(row.get("speaker_uncertain") or "speaker_uncertain" in _flags(row) or row.get("speaker") in {UNKNOWN, None, ""}))
                    word.setdefault("model", row.get("model"))
                    word.setdefault("audio_version", row.get("audio_version"))
                    word.setdefault("recognition_language", row.get("recognition_language", row.get("language")))
                    words.append(word)
            result["words"] = words
            models = sorted({row.get("model", "") for row in parts})
            versions = sorted({row.get("audio_version", "original") for row in parts})
            result["model"] = models[0] if len(models) == 1 else "contextual-ensemble"
            result["audio_version"] = versions[0] if len(versions) == 1 else "both"
            result["recognition_models"] = models
            result["recognition_audio_versions"] = versions
            result["confidence"] = round(sum(row.get("confidence", 0) * max(1, len(_tokens(row.get("text", "")))) for row in parts) / sum(max(1, len(_tokens(row.get("text", "")))) for row in parts), 4)
            flags = set().union(*(_flags(row) for row in parts))
            if unit["speaker_uncertain"]:
                flags.add("speaker_uncertain")
            result["quality_flags"] = sorted(flags)
            result["source_rows"] = _source_rows(parts)
            result["unresolved_spans"] = _unique([deepcopy(item) for row in parts for item in row.get("unresolved_spans", [])])
            result["alternates"], result["unresolved_alternates"] = _alternative_spans(
                evidence, unit["start"], unit["end"], parts, unit["text"], unit["language"])
            references = [deepcopy(alternate) for row in parts for alternate in row.get("alternates", [])
                          if alternate.get("replaceable") is False]
            result["unresolved_alternates"] = _unique(result["unresolved_alternates"] + references)
            result["alternates"] = _unique(result["alternates"] + result["unresolved_alternates"])
        else:
            result["source_rows"] = _source_rows(parts)
        result["translation_context"] = {"policy": "sentence-rows-v1", "source_row_indices": unit["row_indices"],
                                         "start": unit["start"], "end": unit["end"],
                                         "context_limited": unit["context_limited"]}
        if unit["context_limited"]:
            result["quality_flags"] = sorted(_flags(result) | {"translation_context_limited"})
        output.append(result)
    return output
