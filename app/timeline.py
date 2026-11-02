"""Full-timeline recognition, overlap reconciliation, evidence selection and coverage audit.

Pure Python: these operations do not load models or alter an imported transcript.
"""
from difflib import SequenceMatcher
import math
import re

VERSION = "full-coverage-v2"


def pipeline_options(catalog):
    options = {"version":VERSION,"core_seconds":24,"context_seconds":3,
               "language_probe_seconds":6,"selection":"evidence-consensus-v1","recovery_fraction":.25,
               **catalog.get("pipeline",{})}
    if options["version"] != VERSION or options["selection"] != "evidence-consensus-v1":
        raise ValueError("Unsupported pipeline version or selection policy.")
    if not (0 < options["language_probe_seconds"] <= 12 and 0 <= options["recovery_fraction"] <= 1):
        raise ValueError("Language probes must be 0-12 seconds and recovery fraction must be 0-1.")
    windows(1,core=options["core_seconds"],context=options["context_seconds"])
    return options


def tokens(text):
    return re.findall(r"[^\W_]+", str(text).casefold(), re.UNICODE)


def union(intervals, tolerance=0):
    result = []
    for a, b in sorted((float(a), float(b)) for a, b in intervals if b > a):
        if result and a <= result[-1][1] + tolerance:
            result[-1][1] = max(result[-1][1], b)
        else:
            result.append([a, b])
    return result


def subtract(intervals, covered, minimum=0):
    result = []
    covered = union(covered)
    for a, b in union(intervals):
        cursor = a
        for left, right in covered:
            if right <= cursor:
                continue
            if left >= b:
                break
            if left - cursor >= minimum and left > cursor:
                result.append([cursor, min(left, b)])
            cursor = max(cursor, right)
            if cursor >= b:
                break
        if b - cursor >= minimum and b > cursor:
            result.append([cursor, b])
    return result


def windows(duration, regions=(), core=24, context=3, intervals=None):
    """Disjoint ownership cores cover EVERY sample; context never exceeds 30 seconds."""
    if not (0 < core <= 30 and 0 <= context and core + 2 * context <= 30):
        raise ValueError("Recognition core plus two contexts must fit within 30 seconds.")
    result = []
    pauses = [b for _, b in regions]
    for begin, stop in (intervals if intervals is not None else [(0, duration)]):
        cursor = max(0, begin)
        stop = min(duration, stop)
        while cursor < stop - 0.00001:
            end = min(stop, cursor + core)
            # Pause guidance may move a cut earlier, but can never remove audio.
            choices = [p for p in pauses if end - 2 <= p <= end and p >= cursor + core / 2]
            if end < stop and choices:
                end = max(choices)
            result.append({"start": round(max(0, cursor - context), 6),
                           "end": round(min(duration, end + context), 6),
                           "core_start": round(cursor, 6), "core_end": round(end, 6)})
            cursor = end
    return result


def valid_word(word, window):
    a, b = word.get("start"), word.get("end")
    return (isinstance(a, (int, float)) and isinstance(b, (int, float))
            and math.isfinite(a) and math.isfinite(b)
            and window["start"] - .05 <= a <= b <= window["end"] + .05
            and bool(tokens(word.get("word", ""))))


def hypothesis_atoms(candidate, window, source):
    """Keep unaligned model phrases as phrases; never invent per-word timestamps."""
    atoms = []
    if not candidate:
        return atoms
    for piece in candidate.get("pieces") or [candidate]:
        if not tokens(piece.get("text", "")):
            continue
        words = piece.get("words", [])
        aligned = (words and all(valid_word(w, window) for w in words)
                   and tokens(" ".join(w["word"] for w in words)) == tokens(piece["text"]))
        if aligned:
            atoms.extend({**w, "text": w["word"], "aligned": True, "source": source} for w in words)
        else:
            a = max(window["start"], float(piece.get("start", window["core_start"])))
            b = min(window["end"], float(piece.get("end", window["core_end"])))
            if b > a:
                atoms.append({"start": a, "end": b, "text": piece["text"], "aligned": False,
                              "probability": candidate.get("confidence", 0), "source": source})
    return sorted(atoms, key=lambda w: (w["start"], w["end"]))


def midpoint(atom):
    return (atom["start"] + atom["end"]) / 2


def stitch(left, right, seam):
    """Choose a shared, time-consistent word as the seam, not an arbitrary character.

    Only an ordered run of at least two words can anchor a merge. Repeated spoken
    words inside either decoder output are never globally deduplicated.
    """
    if not left:
        return right
    if not right:
        return left
    a = [w for w in left if w["end"] >= seam - 4 and w["start"] <= seam + 4 and w["aligned"]]
    b = [w for w in right if w["end"] >= seam - 4 and w["start"] <= seam + 4 and w["aligned"]]
    anchors = []
    # Tuple keys are hashable, unlike token lists.
    matcher = SequenceMatcher(None, [tuple(tokens(w["text"])) for w in a], [tuple(tokens(w["text"])) for w in b], autojunk=False)
    for block in matcher.get_matching_blocks():
        if block.size < 2:
            continue
        for n in range(block.size):
            x, y = a[block.a + n], b[block.b + n]
            if abs(midpoint(x) - midpoint(y)) <= .8:
                anchors.append((abs(midpoint(x) - seam), x, y))
    if anchors:
        _, x, y = min(anchors, key=lambda v: v[0])
        i, j = next(i for i,w in enumerate(left) if w is x), next(i for i,w in enumerate(right) if w is y)
        return left[:i + 1] + right[j + 1:]
    return [w for w in left if midpoint(w) < seam] + [w for w in right if midpoint(w) >= seam]


def tracks(plan, candidates, attempt="primary"):
    lanes = {}
    for index, (window, options) in enumerate(zip(plan, candidates)):
        for item in options:
            key = (item["model"], item["language"], item["audio_version"])
            lane = lanes.setdefault(key, {"model": key[0], "language": key[1], "audio_version": key[2],
                                          "atoms": [], "evidence": [], "attempt": attempt})
            lane["evidence"].extend(item.get("language_evidence", []))
            atoms = hypothesis_atoms(item, window, f"{attempt}:{index}:{key}")
            # Recovery may contain multiple disjoint intervals.
            lane["atoms"] = stitch(lane["atoms"], atoms, window["core_start"])
    return list(lanes.values())


def speech_overlap(start, end, regions):
    return sum(max(0, min(end,b) - max(start,a)) for a,b in union(regions))


def similarity(a, b):
    return SequenceMatcher(None, tokens(a), tokens(b), autojunk=False).ratio()


def select(options, evidence, preferred):
    """Evidence tiers, not an uncalibrated cross-model weighted confidence score."""
    if not options:
        return None
    baseline = max(options, key=lambda x:x["confidence"])
    pool = options
    reason = "baseline_fallback_ambiguous"
    language_decisive = False
    ranked = sorted(evidence.items(), key=lambda p:p[1], reverse=True)
    if ranked and ranked[0][1] >= .65 and (len(ranked) < 2 or ranked[0][1] - ranked[1][1] >= .25):
        compatible = [x for x in pool if x["language"] == ranked[0][0]]
        if compatible:
            pool = compatible
            reason = "independent_acoustic_language"
            language_decisive = True
    for item in pool:
        item["agreement"] = len({other["model"] for other in options
                                  if similarity(item["text"], other["text"]) >= .75})
    support = max(x["agreement"] for x in pool)
    if support >= 2:
        pool = [x for x in pool if x["agreement"] == support]
        reason += "+independent_model_agreement"
    plausible = [x for x in pool if x.get("timing_plausible",True)]
    if plausible and reason != "baseline_fallback_ambiguous":
        pool = plausible
        reason += "+plausible_timing"
    # With no decisive independent evidence, retain the baseline rule and flag it.
    if reason == "baseline_fallback_ambiguous":
        winner = baseline
    else:
        defaults = [x for x in pool if x["model"] == preferred.get(x["language"])]
        if defaults:
            pool = defaults
        # Probabilities only break ties within the same model and language.
        identities = {(x["model"],x["language"]) for x in pool}
        winner = max(pool, key=lambda x:x["confidence"]) if len(identities)==1 else next((x for x in pool if x is baseline), pool[0])
    winner = {**winner, "selection": {"policy": "evidence-consensus-v1", "reason": reason,
                "language_evidence": evidence, "baseline_model": baseline["model"],
                "baseline_language": baseline["language"], "changed": winner is not baseline,
                "ambiguous":not language_decisive and len({x["language"] for x in options})>1}}
    return winner


def assign_speaker(start, end, turns):
    scores = {}
    for a,b,speaker in turns:
        if a >= end:
            break
        if b > start:
            scores[speaker] = scores.get(speaker,0) + max(0,min(end,b)-max(start,a))
    return max(scores, key=scores.get) if scores else "SPEAKER_UNKNOWN"


def alternate_span(option, start, end):
    atoms = [w for w in option["atoms"] if start <= midpoint(w) <= end]
    words = [{"word":w["text"],"start":round(w["start"],3),"end":round(w["end"],3),
              "probability":w.get("probability",0)} for w in atoms] if all(w["aligned"] for w in atoms) else []
    return {**{k:option[k] for k in ("language","model","audio_version","confidence","attempt")},
            "text":" ".join(w["text"].strip() for w in atoms),"words":words}


def assemble(lanes, turns, duration, preferred, unit_seconds=6):
    """Select short language spans AFTER long-window recognition, then label words."""
    rows = []
    decisions = []
    # Do not let a fixed selection cell split an unaligned phrase into copies.
    unaligned_seen = set()
    emitted = []
    for start in (n * unit_seconds for n in range(math.ceil(duration / unit_seconds))):
        end = min(duration, start + unit_seconds)
        options, evidence_samples = [], []
        for lane in lanes:
            atoms = [w for w in lane["atoms"] if start <= midpoint(w) < end]
            if atoms:
                options.append({**{k:lane[k] for k in ("model","language","audio_version","attempt")},
                    "text": " ".join(w["text"].strip() for w in atoms), "atoms": atoms,
                    "timing_plausible":all(w["aligned"] and w["end"]>w["start"] for w in atoms)
                        and len(atoms)/max(.1,atoms[-1]["end"]-atoms[0]["start"])<=12,
                    "confidence": round(sum(w.get("probability",0) for w in atoms)/len(atoms),4)})
            evidence_samples.extend(x for x in lane["evidence"] if x["start"] < end and x["end"] > start)
        evidence = {}
        # The same acoustic probe is stored on both forced passes; count it once.
        unique = {(x["start"],x["end"],tuple(sorted(x["probabilities"].items()))):x for x in evidence_samples}
        for language in preferred:
            samples = [(x["probabilities"][language], max(0,min(end,x["end"])-max(start,x["start"])))
                       for x in unique.values() if language in x["probabilities"]]
            if samples:
                evidence[language] = round(sum(p*n for p,n in samples)/sum(n for _,n in samples),4)
        winner = select(options,evidence,preferred)
        if winner is None:
            continue
        decisions.append({"start":start,"end":end,**winner["selection"]})
        alternates = [x for x in options if (x["model"],x["language"],x["audio_version"],x["attempt"])
                      != (winner["model"],winner["language"],winner["audio_version"],winner["attempt"])]
        for atom in winner["atoms"]:
            duplicate = any(old["source"] != atom["source"] and tokens(old["text"]) == tokens(atom["text"])
                and min(old["end"],atom["end"])-max(old["start"],atom["start"]) > .5*max(.02,min(old["end"]-old["start"],atom["end"]-atom["start"]))
                for old in emitted[-8:])
            if duplicate:
                continue
            emitted.append(atom)
            identity = (atom["source"],atom["start"],atom["end"],atom["text"])
            if not atom["aligned"] and identity in unaligned_seen:
                continue
            unaligned_seen.add(identity)
            speaker = assign_speaker(atom["start"],atom["end"],turns)
            # Preserve a word which straddles a speaker/language boundary in full.
            join = (rows and rows[-1]["speaker"]==speaker and rows[-1]["language"]==winner["language"]
                    and rows[-1]["model"]==winner["model"] and rows[-1]["audio_version"]==winner["audio_version"]
                    and atom["start"]-rows[-1]["end"] <= .8
                    and atom["end"]-rows[-1]["start"] <= 30
                    and rows[-1]["_unit"]==start)
            if not join:
                rows.append({"start":atom["start"],"end":atom["end"],"speaker":speaker,
                    "language":winner["language"],"suggested_language":winner["language"],"text":"",
                    "model":winner["model"],"audio_version":winner["audio_version"],"words":[],
                    "alternates":[],"selection":winner["selection"],"confidence":winner["confidence"],"_unit":start,
                    "timing_verified":True,"recognition_attempt":winner["attempt"]})
            row=rows[-1]
            row["text"]=(row["text"]+" "+atom["text"].strip()).strip()
            row["end"]=max(row["end"],atom["end"])
            if atom["aligned"]:
                row["words"].append({"word":atom["text"],"start":round(atom["start"],3),"end":round(atom["end"],3),"probability":atom.get("probability",0)})
            else:
                row["timing_verified"]=False
            # Alternatives are restricted to this speaker row, not copied from the whole cell.
            row["alternates"]=[alternate_span(option,row["start"],row["end"]) for option in alternates]
    rows.sort(key=lambda row:(row["start"],row["end"]))
    for row in rows:
        row.pop("_unit",None)
        row["start"],row["end"]=round(row["start"],3),round(row["end"],3)
        if not row.pop("timing_verified"):
            row["words"]=[]
            row["quality_flags"]=["word_alignment_unavailable"]
        row["alternates"]=[a for a in row["alternates"] if tokens(a["text"])]
    return rows,decisions


def audit(duration, plan, candidates, rows, regions, turns):
    text_coverage = [(max(0,w["start"]-.4),min(duration,w["end"]+.4)) for row in rows
                     for w in (row["words"] or [{"start":row["start"],"end":row["end"]}])]
    activity = union([*regions,*[(a,b) for a,b,_ in turns]])
    missing = subtract(activity,text_coverage,minimum=1.5)
    gaps = subtract([(0,duration)],text_coverage,minimum=15)
    issues = [{"kind":"speech_without_words","start":a,"end":b} for a,b in missing]
    issues += [{"kind":"unexplained_gap","start":a,"end":b} for a,b in gaps
               if speech_overlap(a,b,missing)<.1]
    for row in rows:
        for flag in row.get("quality_flags",[]):
            issues.append({"kind":flag,"start":row["start"],"end":row["end"]})
    for window,options in zip(plan,candidates):
        for option in options:
            selected = any(row["start"] < window["core_end"] and row["end"] > window["core_start"]
                           and row.get("recognition_attempt","primary")=="primary"
                           and all(row[k] == option[k] for k in ("model","language","audio_version")) for row in rows)
            if not selected and (any(x.get("text", "").strip() for x in options) or any(row["start"] < window["core_end"] and row["end"] > window["core_start"] for row in rows)):
                continue
            for flag in option.get("quality_flags",[]):
                if flag in {"repetition","word_alignment_unavailable","suspect_timing"}:
                    issues.append({"kind":flag,"start":window["core_start"],"end":window["core_end"]})
            for word in option.get("words",[]):
                on_edge = ((word["start"] <= window["start"]+.08 and window["start"]>0)
                           or (word["end"]>=window["end"]-.08 and window["end"]<duration))
                retained = any(tokens(w["word"])==tokens(word["word"]) and abs(w["start"]-word["start"])<.1
                               for row in rows if row["start"] <= word["end"] and row["end"] >= word["start"] for w in row["words"])
                if on_edge and retained:
                    issues.append({"kind":"boundary_clipping","start":max(0,word["start"]-.5),"end":min(duration,word["end"]+.5)})
                    break
    return {"version":VERSION,"duration":duration,"input_coverage_gaps":subtract([(0,duration)],[(w["core_start"],w["core_end"]) for w in plan]),
            "speech_evidence_seconds":round(sum(b-a for a,b in activity),3),
            "speech_without_words_seconds":round(sum(b-a for a,b in missing),3),"issues":issues}


def recovery_windows(report, regions, fraction=.25):
    duration=report["duration"]
    priority={"speech_without_words":0,"repetition":1,"word_alignment_unavailable":2,"suspect_timing":2,"boundary_clipping":3,"unexplained_gap":4}
    budget=min(duration,max(60,duration*fraction))
    intervals=[]
    deferred=[]
    for issue in sorted(report["issues"],key=lambda x:(priority.get(x["kind"],5),x["start"])):
        a,b=max(0,issue["start"]-1),min(duration,issue["end"]+1)
        for left,right in subtract([(a,b)],intervals):
            remaining=budget-sum(y-x for x,y in intervals)
            if remaining<=.01:
                deferred.append([left,right]);continue
            stop=min(right,left+remaining)
            intervals=union([*intervals,[left,stop]])
            if stop<right:deferred.append([stop,right])
    return windows(duration,regions,core=12,context=4,intervals=intervals),union(deferred)
