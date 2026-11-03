"""Full-timeline recognition, overlap reconciliation, evidence selection and coverage audit.

Pure Python: these operations do not load models or alter an imported transcript.
"""
from bisect import bisect_left
from difflib import SequenceMatcher
import math
import re

VERSION = "full-coverage-v3"


def pipeline_options(catalog):
    options = {"version":VERSION,"core_seconds":24,"context_seconds":3,
               "language_probe_seconds":6,"selection":"span-consensus-v2","recovery_fraction":.25,
               **catalog.get("pipeline",{})}
    if options["version"] != VERSION or options["selection"] != "span-consensus-v2":
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
    """Validate lexical words; canonical punctuation never invalidates their timing."""
    atoms = []
    if not candidate:
        return atoms
    for piece in candidate.get("pieces") or [candidate]:
        text = str(piece.get("text", ""))
        matches = list(re.finditer(r"[^\W_]+",text,re.UNICODE))
        if not matches:
            continue
        words = [word for word in (piece.get("words") or []) if tokens(word.get("word",""))]
        aligned = (words and all(valid_word(word,window) for word in words)
                   and tokens(" ".join(word["word"] for word in words))==tokens(text)
                   and all(left["start"]<=right["start"] for left,right in zip(words,words[1:])))
        if aligned:
            # Preserve canonical punctuation on the correct side of whitespace.
            # Decoder fragments joined by an apostrophe/hyphen form one whole
            # orthographic word with the union of their observed timings.
            groups=[];token_index=0;last_token_end=0
            for word in words:
                first=matches[token_index].start()
                token_index+=len(tokens(word["word"]))
                lexical_end=matches[token_index-1].end()
                gap=text[last_token_end:first]
                separators=list(re.finditer(r"\s+",gap))
                if groups and not separators:
                    groups[-1]["words"].append(word)
                else:
                    boundary=last_token_end+separators[-1].end() if groups else 0
                    if groups:groups[-1]["right"]=boundary
                    groups.append({"left":boundary,"words":[word]})
                last_token_end=lexical_end
            groups[-1]["right"]=len(text)
            for group in groups:
                parts=group["words"]
                atoms.append({"start":min(word["start"] for word in parts),
                    "end":max(word["end"] for word in parts),
                    "probability":sum(word.get("probability",0) for word in parts)/len(parts),
                    "text":text[group["left"]:group["right"]].strip(),
                    "aligned":True,"source":source})
        else:
            a=piece.get("start",window["core_start"]);b=piece.get("end",window["core_end"])
            if not all(isinstance(x,(int,float)) and math.isfinite(x) for x in (a,b)):
                a,b=window["start"],window["end"]
            a,b=max(window["start"],float(a)),min(window["end"],float(b))
            if b>a:
                atoms.append({"start":a,"end":b,"text":text,"aligned":False,
                              "probability":candidate.get("confidence",0),"source":source})
    return sorted(atoms,key=lambda word:(word["start"],word["end"]))


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
        result=left[:i + 1]+right[j + 1:]
    else:
        result=[w for w in left if w["aligned"] and midpoint(w)<seam]+[w for w in right if w["aligned"] and midpoint(w)>=seam]
    identities={(w["source"],w["start"],w["end"],w["text"]) for w in result}
    for word in [*left,*right]:
        identity=(word["source"],word["start"],word["end"],word["text"])
        if not word["aligned"] and identity not in identities:
            result.append(word);identities.add(identity)
    return sorted(result,key=lambda word:(word["start"],word["end"]))


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
    winner = {**winner, "selection": {"policy": "span-consensus-v2", "reason": reason,
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
    atoms=[word for word in option["atoms"] if
           (start<=midpoint(word)<end if word["aligned"] else word["start"]<end and word["end"]>start)]
    aligned=all(word["aligned"] for word in atoms)
    words=[{"word":word["text"],"start":round(word["start"],3),"end":round(word["end"],3),
            "probability":word.get("probability",0)} for word in atoms] if aligned else []
    return {**{key:option.get(key) for key in ("language","model","audio_version","confidence","attempt")},
            "text":" ".join(word["text"].strip() for word in atoms),"words":words,
            "start":min((word["start"] for word in atoms),default=start),
            "end":max((word["end"] for word in atoms),default=end),"replaceable":aligned,
            "sources":sorted({word["source"] for word in atoms})}


def assemble(lanes, turns, duration, preferred, unit_seconds=6):
    """Select word readings, reconcile full competing spans, then label speakers."""
    from .reconciliation import reconcile
    from .speaker_assignment import assign_word_speakers
    records=[];decisions=[];seen=set()
    indexed=[];probes={}
    for lane in lanes:
        atoms=sorted(lane["atoms"],key=midpoint)
        indexed.append((lane,atoms,[midpoint(word) for word in atoms]))
        for item in lane["evidence"]:
            key=(item["start"],item["end"],tuple(sorted(item["probabilities"].items())))
            probes[key]=item
    probes=sorted(probes.values(),key=lambda item:item["start"])
    probe_starts=[item["start"] for item in probes]
    longest_probe=max((item["end"]-item["start"] for item in probes),default=0)
    for start in (n*unit_seconds for n in range(math.ceil(duration/unit_seconds))):
        end=min(duration,start+unit_seconds);options=[];evidence_samples=[]
        for lane,all_atoms,midpoints in indexed:
            atoms=all_atoms[bisect_left(midpoints,start):bisect_left(midpoints,end)]
            if atoms:
                options.append({**{key:lane[key] for key in ("model","language","audio_version","attempt")},
                    "text":" ".join(word["text"].strip() for word in atoms),"atoms":atoms,
                    "timing_plausible":all(word["aligned"] and word["end"]>word["start"] for word in atoms)
                        and len(atoms)/max(.1,atoms[-1]["end"]-atoms[0]["start"])<=12,
                    "confidence":round(sum(word.get("probability",0) for word in atoms)/len(atoms),4)})
        evidence_samples=[item for item in probes[bisect_left(probe_starts,start-longest_probe):bisect_left(probe_starts,end)] if item["end"]>start]
        unique={(item["start"],item["end"],tuple(sorted(item["probabilities"].items()))):item for item in evidence_samples}
        evidence={}
        for language in preferred:
            samples=[(item["probabilities"][language],max(0,min(end,item["end"])-max(start,item["start"])))
                     for item in unique.values() if language in item["probabilities"]]
            if samples:evidence[language]=round(sum(p*weight for p,weight in samples)/sum(weight for _,weight in samples),4)
        winner=select(options,evidence,preferred)
        if winner is None:continue
        # Recovery must not create a new >=1.5s hole in already timed primary
        # wording. Keep the primary reading in that cell and report the conflict.
        primary_options=[option for option in options if option["attempt"]=="primary"]
        if winner["attempt"]=="recovery" and primary_options:
            primary=select(primary_options,evidence,preferred)
            before=[(max(start,w["start"]-.4),min(end,w["end"]+.4)) for w in primary["atoms"] if w["aligned"]]
            after=[(max(start,w["start"]-.4),min(end,w["end"]+.4)) for w in winner["atoms"] if w["aligned"]]
            if subtract(before,after,minimum=1.5):
                winner={**primary,"quality_flags":["recovery_coverage_regression"],
                        "selection":{**primary["selection"],"reason":primary["selection"]["reason"]+"+recovery_coverage_guard"}}
        # A long untimed winner remains a full-span reference, while timed
        # alternatives compete for the cell's actual words. Reconciliation below
        # resolves these choices across all cells, including empty midpoint cells.
        unaligned_winner=winner if any(not word["aligned"] for word in winner["atoms"]) else None
        timed_options=[option for option in options if all(word["aligned"] for word in option["atoms"])]
        if unaligned_winner and timed_options:
            for atom in unaligned_winner["atoms"]:
                if atom["aligned"]:continue
                identity=(atom["source"],atom["start"],atom["end"],atom["text"])
                if identity not in seen:
                    seen.add(identity);records.append({"atom":atom,"winner":unaligned_winner,"alternatives":options,"unit":start})
            winner=select(timed_options,evidence,preferred)
            winner={**winner,"selection":{**winner["selection"],"reason":winner["selection"]["reason"]+"+unaligned_span_conflict"}}
        decisions.append({"start":start,"end":end,**winner["selection"]})
        alternatives=[option for option in options if (option["model"],option["language"],option["audio_version"],option["attempt"])
                      !=(winner["model"],winner["language"],winner["audio_version"],winner["attempt"])]
        for atom in winner["atoms"]:
            identity=(atom["source"],atom["start"],atom["end"],atom["text"])
            if identity in seen:continue
            seen.add(identity)
            records.append({"atom":atom,"winner":winner,"alternatives":alternatives,"unit":start})
    # Reconcile only after ALL cells have selected their readings. An early
    # paragraph cannot escape conflicts with words selected in a later cell.
    records=reconcile(records,turns)
    assignments=assign_word_speakers([record["atom"] for record in records],turns)
    rows=[]
    for record,assignment in zip(records,assignments):
        atom,winner=record["atom"],record["winner"]
        if atom.get("speaker") and atom["speaker"]!="SPEAKER_UNKNOWN":
            assignment={**assignment,"speaker":atom["speaker"],"uncertain":bool(atom.get("speaker_uncertain",False))}
        speaker=assignment["speaker"]
        flags=set(record.get("quality_flags",[]))|set(winner.get("quality_flags",[]))
        if assignment["uncertain"]:flags.add("speaker_uncertain")
        if not atom["aligned"]:flags.add("word_alignment_unavailable")
        join=(rows and rows[-1]["speaker"]==speaker and rows[-1]["language"]==winner["language"]
              and rows[-1]["model"]==winner["model"] and rows[-1]["audio_version"]==winner["audio_version"]
              and -.05<=atom["start"]-rows[-1]["end"]<=.8
              and atom["end"]-rows[-1]["start"]<=30 and rows[-1]["_unit"]==record["unit"]
              and bool(rows[-1]["words"])==bool(atom["aligned"]))
        if not join:
            rows.append({"start":atom["start"],"end":atom["end"],"speaker":speaker,
                "language":winner["language"],"suggested_language":winner["language"],"text":"",
                "model":winner["model"],"audio_version":winner["audio_version"],"words":[],
                "alternates":[],"selection":winner["selection"],"confidence":winner["confidence"],
                "recognition_attempt":winner["attempt"],"quality_flags":[],"unresolved_spans":[],
                "_unit":record["unit"],"_alternatives":{},"_references":[]})
        row=rows[-1];row["text"]=(row["text"]+" "+atom["text"].strip()).strip()
        row["end"]=max(row["end"],atom["end"])
        row["quality_flags"]=sorted(set(row["quality_flags"])|flags)
        row["speaker_uncertain"]="speaker_uncertain" in row["quality_flags"]
        row["unresolved_spans"].extend(record.get("unresolved_spans",[]))
        row["_references"].extend(record.get("references",[]))
        if atom["aligned"]:
            row["words"].append({"word":atom["text"],"start":round(atom["start"],3),"end":round(atom["end"],3),
                "probability":atom.get("probability",0),"speaker":speaker,"speaker_uncertain":assignment["uncertain"],
                "speaker_candidates":assignment["candidates"],"speaker_reason":assignment["reason"],
                "model":winner["model"],"audio_version":winner["audio_version"],"source":atom["source"],"recognition_language":winner["language"]})
        for option in record["alternatives"]:
            key=tuple(option[key] for key in ("model","language","audio_version","attempt"))
            previous=row["_alternatives"].get(key)
            if previous:
                atoms={ (word["source"],word["start"],word["end"],word["text"]):word for word in [*previous["atoms"],*option["atoms"]] }
                previous["atoms"]=sorted(atoms.values(),key=lambda word:(word["start"],word["end"]))
            else:row["_alternatives"][key]={**option,"atoms":list(option["atoms"])}
    for row in rows:
        row.pop("_unit")
        row["start"],row["end"]=round(row["start"],3),round(row["end"],3)
        alternatives=[alternate_span(option,row["start"],row["end"]) for option in row.pop("_alternatives").values()]
        alternatives.extend(row.pop("_references"))
        unique={}
        for option in alternatives:
            if tokens(option.get("text","")):
                key=(option.get("source"),option.get("model"),option.get("language"),option.get("audio_version"),
                     option.get("attempt"),option.get("start"),option.get("end"),option["text"])
                unique[key]=option
        row["alternates"]=list(unique.values())
    return rows,decisions



def audit(duration, plan, candidates, rows, regions, turns):
    # Estimated phrase bounds never stand in for verified word coverage.
    text_coverage=[(max(0,word["start"]-.4),min(duration,word["end"]+.4)) for row in rows for word in row.get("words",[])]
    estimated=union([(max(0,row["start"]),min(duration,row["end"])) for row in rows if not row.get("words")])
    activity = union([*regions,*[(a,b) for a,b,_ in turns]])
    missing = subtract(activity,text_coverage,minimum=1.5)
    gaps = subtract([(0,duration)],text_coverage,minimum=15)
    issues = [{"kind":"speech_without_words","start":a,"end":b} for a,b in missing]
    issues += [{"kind":"unexplained_gap","start":a,"end":b} for a,b in gaps
               if speech_overlap(a,b,missing)<.1]
    for row in rows:
        issues.extend(row.get("unresolved_spans",[]))
        for flag in row.get("quality_flags",[]):
            if flag=="unresolved_overlap" and any(issue.get("kind")==flag for issue in row.get("unresolved_spans",[])):
                continue
            issues.append({"kind":flag,"start":row["start"],"end":row["end"]})
    origins=[origin for row in rows for origin in (row.get("source_rows") or [row])]
    for window,options in zip(plan,candidates):
        for option in options:
            selected=any(origin["start"]<window["core_end"] and origin["end"]>window["core_start"]
                and origin.get("recognition_attempt","primary")=="primary"
                and origin.get("model")==option["model"] and origin.get("audio_version")==option["audio_version"]
                and origin.get("recognition_language",(origin.get("language_context") or {}).get("original_language",origin.get("language")))==option["language"]
                for origin in origins)
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
    unique={}
    for issue in issues:
        key=(issue["kind"],round(issue["start"],3),round(issue["end"],3),issue.get("source"))
        unique[key]=issue
    issues=list(unique.values())
    return {"aligned_word_coverage_seconds":round(sum(b-a for a,b in union(text_coverage)),3),
            "estimated_unaligned_seconds":round(sum(b-a for a,b in estimated),3),
            "unresolved_overlap_count":sum(issue["kind"]=="unresolved_overlap" for issue in issues),
            "version":VERSION,"duration":duration,"input_coverage_gaps":subtract([(0,duration)],[(w["core_start"],w["core_end"]) for w in plan]),
            "speech_evidence_seconds":round(sum(b-a for a,b in activity),3),
            "speech_without_words_seconds":round(sum(b-a for a,b in missing),3),"issues":issues}


def recovery_windows(report, regions, fraction=.25):
    duration=report["duration"]
    priority={"speech_without_words":0,"repetition":1,"word_alignment_unavailable":2,"unresolved_overlap":2,"suspect_timing":2,"boundary_clipping":3,"unexplained_gap":4}
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
