"""Transactional editor undo and small incremental responses; no audio/model work."""
import copy
import time
from sqlalchemy import event, func, inspect, select
from sqlalchemy.orm import defer
from fastapi import HTTPException
from .db import SessionLocal, Segment, Speaker, Recording, Job, EditAction, new_id, serialize_segment, serialize_job

MODELS = {'segment':Segment,'speaker':Speaker,'recording':Recording,'job':Job}
RECORDING_FIELDS = {'title','notes','workshop','date','settings'}


def undo_state(session, recording_id):
    row=session.execute(select(EditAction.id,EditAction.label).where(EditAction.recording_id==recording_id,EditAction.undone==False).order_by(EditAction.id.desc()).limit(1)).first()
    return {'available':bool(row),'id':row.id if row else None,'label':row.label if row else ''}


@event.listens_for(SessionLocal.class_, 'before_flush')
def capture(session, context, instances):
    if not session.info.get('edit_label') or session.info.get('writing_history'):
        return
    changes=session.info.setdefault('edit_changes',{})
    new_objects=session.info.setdefault('edit_new_objects',{})
    for obj in set(session.new).union(session.dirty,session.deleted):
        table=getattr(obj,'__tablename__','')
        if table not in MODELS:
            continue
        if table=='job' and (obj not in session.new or obj.kind not in {'translate','retranscribe'}):
            continue
        if obj in session.new and not obj.id:
            obj.id=new_id()
        key=f'{table}:{obj.id}'
        columns=list(obj.__table__.columns)
        if table=='recording':
            if obj in session.new or obj in session.deleted:
                continue
            columns=[c for c in columns if c.name in RECORDING_FIELDS]
        state=inspect(obj)
        fields=[c for c in columns if c.name!='revision' and state.attrs[c.name].history.has_changes()]
        if obj not in session.new and obj not in session.deleted and not fields:
            continue
        if obj in session.new:
            before=None
            after={c.name:copy.deepcopy(getattr(obj,c.name)) for c in columns}
            new_objects[key]=obj
        else:
            selected=columns if obj in session.deleted else fields
            old=session.connection().execute(select(*selected).where(obj.__table__.c.id==obj.id)).mappings().first()
            before=copy.deepcopy(dict(old)) if old is not None else None
            after=None if obj in session.deleted else {c.name:copy.deepcopy(getattr(obj,c.name)) for c in fields}
        if key in changes:
            entry=changes[key]
            if entry['after'] is None:
                continue
            if entry['before'] is not None and before is not None:
                for name,value in before.items():
                    entry['before'].setdefault(name,value)
            if after is None:
                entry['after']=None
            else:
                entry['after'].update(after)
        else:
            changes[key]={'table':table,'id':obj.id,'recording_id':obj.id if table=='recording' else obj.recording_id,
                          'before':before,'after':after}


@event.listens_for(SessionLocal.class_, 'after_flush')
def capture_defaults(session, context):
    for key,obj in session.info.get('edit_new_objects',{}).items():
        entry=session.info['edit_changes'][key]
        if entry['after'] is not None:
            entry['after']={c.name:copy.deepcopy(getattr(obj,c.name)) for c in obj.__table__.columns}


@event.listens_for(SessionLocal.class_, 'before_commit')
def save_history(session):
    if not session.info.get('edit_label') or session.info.get('writing_history'):
        return
    session.flush()
    changes=list(session.info.pop('edit_changes',{}).values())
    new_objects=session.info.pop('edit_new_objects',{})
    if not changes:
        return
    recording_id=changes[0]['recording_id']
    if any(row['recording_id']!=recording_id for row in changes):
        raise ValueError('An editor action must belong to one recording.')
    session.info['writing_history']=True
    try:
        parent=session.get(EditAction,session.info.get('edit_parent')) if session.info.get('edit_parent') else None
        if parent and parent.undone:
            raise ValueError('This edit was undone before its background result could be saved.')
        if parent:
            merged={f"{row['table']}:{row['id']}":copy.deepcopy(row) for row in parent.changes}
            for change in changes:
                key=f"{change['table']}:{change['id']}"
                if key not in merged:
                    merged[key]=change
                else:
                    existing=merged[key]
                    if existing['before'] is not None and change['before'] is not None:
                        for field,value in change['before'].items():existing['before'].setdefault(field,value)
                    if existing['after'] is not None and change['after'] is not None:existing['after'].update(change['after'])
                    else:existing['after']=change['after']
            parent.changes=list(merged.values());parent.updated_at=time.time();action=parent
        else:
            action=EditAction(recording_id=recording_id,label=session.info['edit_label'],changes=changes,updated_at=time.time())
            session.add(action)
        session.flush()
        for obj in new_objects.values():
            if isinstance(obj,Job):obj.payload={**(obj.payload or {}),'edit_action_id':action.id}
        session.flush()
        session.info['last_edit']={'id':action.id,'recording_id':recording_id,'changes':changes}
    finally:
        session.info['writing_history']=False


def job_edit(session,job):
    parent=(job.payload or {}).get('edit_action_id')
    action=session.get(EditAction,parent) if parent else None
    if action and action.undone:
        return False
    session.info.update(edit_label='Translate text' if job.kind=='translate' else 'Re-transcribe text',edit_parent=parent if action else None)
    return True


def editor_delta(session, recording_id, changes, cursor=None):
    cursor = time.time() if cursor is None else cursor
    ids={row['id'] for row in changes if row['table']=='segment'}
    selected=session.execute(select(Segment,func.json_array_length(Segment.alternates)).where(Segment.recording_id==recording_id,Segment.id.in_(ids))
        .options(defer(Segment.alternates),defer(Segment.machine_history),defer(Segment.machine_text),defer(Segment.machine_english))).all() if ids else []
    # Compact serialization avoids loading alternate JSON just to count it.
    rows=[]
    for row,count in selected:
        row._alternate_count=count or 0
        rows.append(row)
    speakers=session.scalars(select(Speaker).where(Speaker.recording_id==recording_id).order_by(Speaker.id)).all()
    rec=session.get(Recording,recording_id)
    jobs=session.scalars(select(Job).where(Job.recording_id==recording_id).options(defer(Job.log)).order_by(Job.created_at.desc())).all()
    return {'upsert':[serialize_segment(row,compact=True) for row in rows if not row.is_deleted],
            'removed':[row.id for row in rows if row.is_deleted]+list(ids-{row.id for row in rows}),
            'removed_revisions':{row.id:row.revision for row in rows if row.is_deleted},
            'speakers':[{'id':s.id,'name':s.name,'recording_id':s.recording_id} for s in speakers],
            'metadata':{key:getattr(rec,key) for key in RECORDING_FIELDS},
            'jobs':[serialize_job(job,include_log=False) for job in jobs],
            'undo':undo_state(session,recording_id),'editor_cursor':cursor}


def edit_response(session, recording_id, result):
    if not session.info.get('editor_delta'):
        return result
    changes=session.info.get('last_edit',{}).get('changes',[])
    small={k:v for k,v in result.items() if k in {'id','job','count','replacements','changed_segments','deleted','name','recording_id'}}
    if 'segments' in result:small['segments']=[{'id':row['id']} for row in result['segments']]
    return {**small,'editor':editor_delta(session,recording_id,changes,session.info.get('editor_started'))}


def undo(session,recording_id,expected_id):
    action=session.scalar(select(EditAction).where(EditAction.recording_id==recording_id,EditAction.undone==False).order_by(EditAction.id.desc()).limit(1))
    if not action:
        raise HTTPException(409,'There are no saved edits to undo.')
    if expected_id and action.id!=expected_id:
        raise HTTPException(409,'A newer edit was saved. Refresh the undo action and try again.')
    # Only fields changed by this operation are checked/restored. Revision numbers
    # always increase so an in-flight model result cannot overwrite an undo.
    for change in action.changes:
        if change['table']=='job':continue
        obj=session.get(MODELS[change['table']],change['id'])
        expected=change['after']
        if expected is None:
            if obj is not None:raise HTTPException(409,'An affected item was recreated; undo would overwrite it.')
        elif obj is None or any(getattr(obj,k)!=v for k,v in expected.items() if k!='revision'):
            raise HTTPException(409,'An affected item changed after this edit. Undo was not applied.')
    # Restore deleted speakers before restoring segment foreign keys.
    for change in action.changes:
        if change['table']=='speaker' and change['after'] is None and change['before']:
            session.add(Speaker(**change['before']))
    session.flush()
    for change in action.changes:
        table=change['table'];obj=session.get(MODELS[table],change['id']);before=change['before']
        if table in {'job','speaker'} and before is None:continue
        if table=='speaker' and change['after'] is None:continue
        if before is None:
            if table=='segment':obj.is_deleted=True;obj.revision+=1
        elif obj is not None:
            for key,value in before.items():
                if key!='revision':setattr(obj,key,copy.deepcopy(value))
            if table=='segment':obj.revision+=1
    for change in action.changes:
        if change['table']=='job':
            job=session.get(Job,change['id'])
            if job:
                if job.status in {'queued','running','processing'}:job.status='cancelled';job.step='Undone in editor'
                payload=job.payload or {}
                for segment_id in payload.get('segment_ids',[payload.get('segment_id')]):
                    segment=session.get(Segment,segment_id) if segment_id else None
                    if segment:segment.revision+=1
        elif change['table']=='speaker' and change['before'] is None:
            speaker=session.get(Speaker,change['id'])
            if speaker:session.delete(speaker)
    action.undone=True;action.updated_at=time.time()
    session.commit()
    return {'label':action.label,'editor':editor_delta(session,recording_id,action.changes,session.info.get('editor_started'))}
