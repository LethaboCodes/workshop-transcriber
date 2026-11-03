"""Resolve competing recognition spans without inventing word timestamps."""
from collections import defaultdict
from difflib import SequenceMatcher
import re

from .speaker_assignment import assign_word_speakers


def tokens(value):
    return re.findall(r"[^\W_]+", str(value).casefold(), re.UNICODE)


def _mid(record):
    atom = record['atom']
    return (atom['start'] + atom['end']) / 2


def _reference(record):
    atom, winner = record['atom'], record['winner']
    return {key:winner.get(key) for key in ('model','language','audio_version','confidence','attempt')} | {
        'text':atom['text'], 'words':[], 'start':atom['start'], 'end':atom['end'],
        'source':atom['source'], 'replaceable':False, 'quality_flags':['word_alignment_unavailable']}


def reconcile(records, turns):
    """Timed readings own their words; competing untimed readings stay reachable.

    A full untimed phrase may be selected by its midpoint, but cannot then be
    emitted across other cells' timed readings. Its full text and real source
    bounds remain a reference, with unresolved coverage until its wording is
    accounted for. This is a structural conflict decision, not an accuracy claim.
    """
    records = sorted(records, key=lambda row:(row['atom']['start'],row['atom']['end']))
    timed = [row for row in records if row['atom']['aligned']]
    untimed = [row for row in records if not row['atom']['aligned']]
    dropped = set()
    # Two time-consistent ordered words are required to establish a duplicate.
    # A repeated word alone, or evidence for different speakers, is insufficient.
    assignments = assign_word_speakers([row['atom'] for row in timed],turns)
    for index,row in enumerate(timed):
        atom=row['atom']
        if atom.get('speaker') and atom['speaker']!='SPEAKER_UNKNOWN':
            assignments[index]={**assignments[index],'speaker':atom['speaker'],
                                'uncertain':bool(atom.get('speaker_uncertain',False))}
    by_source = defaultdict(list)
    for index,row in enumerate(timed):
        by_source[row['atom']['source']].append(index)
    groups = sorted(by_source.values(),key=lambda ids:timed[ids[0]]['atom']['start'])
    active = []
    for ids in groups:
        start = min(timed[i]['atom']['start'] for i in ids)
        end = max(timed[i]['atom']['end'] for i in ids)
        active = [(old,stop) for old,stop in active if stop >= start-.35]
        for old,_ in active:
            matcher = SequenceMatcher(None,[tuple(tokens(timed[i]['atom']['text'])) for i in old],
                                      [tuple(tokens(timed[i]['atom']['text'])) for i in ids],autojunk=False)
            for block in matcher.get_matching_blocks():
                if block.size < 2:
                    continue
                pairs = list(zip(old[block.a:block.a+block.size],ids[block.b:block.b+block.size]))
                matching = []
                for left,right in pairs:
                    a,b = timed[left]['atom'],timed[right]['atom']
                    overlap = min(a['end'],b['end'])-max(a['start'],b['start'])
                    speakers = assignments[left],assignments[right]
                    if (left not in dropped and abs(_mid(timed[left])-_mid(timed[right]))<=.35
                            and overlap>.5*max(.02,min(a['end']-a['start'],b['end']-b['start']))
                            and not any(s['uncertain'] for s in speakers)
                            and speakers[0]['speaker']==speakers[1]['speaker']):
                        matching.append(right)
                # Do not join separate one-word coincidences into a phrase.
                if len(matching)==block.size:
                    dropped.update(matching)
        active.append((ids,end))
    timed = [row for index,row in enumerate(timed) if index not in dropped]
    retained = []
    for row in untimed:
        atom = row['atom']
        overlapping = [item for item in timed if item['atom']['start']<atom['end']
                       and item['atom']['end']>atom['start']]
        if not overlapping:
            # No timed replacement exists: keep every original word verbatim.
            retained.append(row)
            continue
        anchor = min(overlapping,key=lambda item:abs(_mid(item)-_mid(row)))
        anchor.setdefault('references',[]).append(_reference(row))
        represented = tokens(' '.join(item['atom']['text'] for item in overlapping))
        expected = tokens(atom['text'])
        # Only exact ordered agreement resolves the untimed text automatically.
        if represented!=expected:
            issue = {'kind':'unresolved_overlap','start':atom['start'],'end':atom['end'],
                     'source':atom['source']}
            anchor.setdefault('unresolved_spans',[]).append(issue)
            anchor.setdefault('quality_flags',set()).add('unresolved_overlap')
    # Untimed alternatives cannot be clipped safely. Preserve their full phrases
    # and flag conflicts still requiring recovery instead of deleting unique text.
    for index,row in enumerate(retained):
        for other in retained[:index]:
            a,b = row['atom'],other['atom']
            if a['start']<b['end'] and a['end']>b['start']:
                issue = {'kind':'unresolved_overlap','start':min(a['start'],b['start']),
                         'end':max(a['end'],b['end']),'source':a['source']}
                row.setdefault('unresolved_spans',[]).append(issue)
                row.setdefault('quality_flags',set()).add('unresolved_overlap')
                other.setdefault('quality_flags',set()).add('unresolved_overlap')
                other.setdefault('unresolved_spans',[]).append(issue)
    return sorted([*timed,*retained],key=lambda row:(row['atom']['start'],row['atom']['end']))
