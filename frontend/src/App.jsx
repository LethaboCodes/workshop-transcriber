import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import WaveSurfer from 'wavesurfer.js';
import { api, CHUNK_SIZE, LANGUAGES, time, duration, size, date, asArray, pendingUploads, rememberUpload, forgetUpload, fileStore } from './api.js';

const ICONS = {
  wave: 'M3 10v4m4-8v12m5-16v20m5-16v12m4-8v4',
  recordings: 'M4 4h6l2 3h8v13H4zM8 12h8m-8 4h5',
  search: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  chart: 'M4 3v17h17M8 15V9m5 6V5m5 10v-4',
  settings: 'M4 7h16M4 17h16M9 4v6m6 4v6',
  plus: 'M12 5v14M5 12h14', close: 'M6 6l12 12M6 18L18 6',
  arrow: 'M5 12h14m-6-6 6 6-6 6', back: 'M19 12H5m6-6-6 6 6 6',
  play: 'm8 4 12 8-12 8z', pause: 'M8 5v14m8-14v14',
  upload: 'M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6',
  download: 'M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4',
  chevron: 'm9 5 7 7-7 7', check: 'm5 12 4 4L20 5',
  clock: 'M12 8v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  refresh: 'M20 7a9 9 0 1 0 1 9M20 2v6h-6',
  trash: 'M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7',
  edit: 'm15 4 5 5M3 21l5-1L21 7l-5-5L3 15z',
  flag: 'M5 22V3c5-4 9 5 14 0v11c-5 5-9-4-14 0',
  split: 'M6 3v4c0 3 12 7 12 10v4M18 3v4c0 3-12 7-12 10v4M3 18l3 3 3-3m6 0 3 3 3-3',
  merge: 'M6 3v4c0 4 6 5 6 8v6M18 3v4c0 4-6 5-6 8m-3 3 3 3 3-3',
  loop: 'M18 3l4 4-4 4M2 7h20M6 21l-4-4 4-4m16 4H2',
  volume: 'M3 9v6h4l5 4V5L7 9zM16 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14',
  help: 'M9 8a3 3 0 0 1 6 0c0 3-3 2-3 5m0 4h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  logout: 'M9 4H3v16h6M9 12h12m-5-5 5 5-5 5',
  users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M17 4a4 4 0 0 1 0 8m5 9v-2a4 4 0 0 0-3-4M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  file: 'M14 2H4v20h16V8zM14 2v6h6M8 13h8m-8 4h6',
  alert: 'm12 3 10 18H2zM12 9v5m0 3h.01',
  menu: 'M3 6h18M3 12h18M3 18h18', stop: 'M6 6h12v12H6z',
  compare: 'M3 4h7v16H3zm11 0h7v16h-7',
};
function Icon({ name, size: iconSize = 18, ...props }) { return <svg width={iconSize} height={iconSize} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}><path d={ICONS[name] || ICONS.file}/></svg>; }
function Button({ icon, children, className = '', busy, ...props }) { return <button className={`button ${className}`} {...props} disabled={busy || props.disabled}>{busy ? <span className="spinner small"/> : icon && <Icon name={icon}/>} {children}</button>; }
function Badge({ status, children }) { return <span className={`badge ${status || ''}`}><i/>{children || status || 'unreviewed'}</span>; }
function Empty({ icon = 'wave', title, children, action }) { return <div className="empty"><span className="empty-icon"><Icon name={icon} size={31}/></span><h2>{title}</h2><p>{children}</p>{action}</div>; }
function Loading({ children = 'Opening your workspace…' }) { return <div className="loading"><span className="spinner"/><span>{children}</span></div>; }
function ErrorNotice({ message, retry }) { return <div className="error-notice" role="alert"><Icon name="alert"/><span>{message}</span>{retry && <Button onClick={retry}>Try again</Button>}</div>; }
function Modal({ title, subtitle, children, close, wide = false }) {
  const panel = useRef(null);
  useEffect(() => {
    const previous = document.activeElement;
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    panel.current?.querySelector('input, button, select, textarea')?.focus();
    const handler = event => {
      if (event.key === 'Escape') { event.preventDefault(); close(); }
      if (event.key === 'Tab') {
        const elements = Array.from(panel.current?.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]') || []).filter(element => element.offsetParent !== null);
        const first = elements[0], last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
      event.stopPropagation();
    };
    panel.current?.addEventListener('keydown', handler);
    const element = panel.current;
    return () => { document.body.style.overflow = oldOverflow; element?.removeEventListener('keydown', handler); previous?.focus(); };
  }, []);
  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && close()}><section className={`modal ${wide ? 'wide' : ''}`} ref={panel} role="dialog" aria-modal="true" aria-label={title}><header className="modal-head"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div><button className="icon-button" aria-label="Close dialog" onClick={close}><Icon name="close"/></button></header>{children}</section></div>;
}
function useRoute() {
  const read = () => { const [path, query = ''] = (location.hash.slice(1) || '/recordings').split('?'); return { path, query: new URLSearchParams(query) }; };
  const [route, setRoute] = useState(read);
  useEffect(() => { const handler = () => setRoute(read()); window.addEventListener('hashchange', handler); return () => window.removeEventListener('hashchange', handler); }, []);
  return [route, useCallback(path => { location.hash = path; }, [])];
}

export default function App() {
  const [session, setSession] = useState(null);
  const [sessionError, setSessionError] = useState('');
  const [route, navigate] = useRoute();
  const [recordings, setRecordings] = useState([]);
  const [settings, setSettings] = useState(null);
  const [models, setModels] = useState([]);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(true);
  const [newRecording, setNewRecording] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [toast, setToast] = useState(null);
  const [uploads, setUploads] = useState({});
  const activeUploads = useRef(new Map());
  const toastTimer = useRef(null);
  const notify = useCallback((message, error = false) => { setToast({ message, error }); clearTimeout(toastTimer.current); toastTimer.current = setTimeout(() => setToast(null), error ? 9000 : 4500); }, []);
  const load = useCallback(async () => {
    try {
      const [recordingData, settingData, modelData] = await Promise.all([api('/recordings'), api('/settings'), api('/models')]);
      setRecordings(asArray(recordingData, 'recordings')); setSettings(settingData); setModels(asArray(modelData, 'models')); setLoadError('');
    } catch (error) { setLoadError(error.message); } finally { setLoading(false); }
  }, []);
  const checkSession = useCallback(() => api('/session').then(data => { setSession(data.authenticated); setSessionError(''); }).catch(error => setSessionError(error.message)), []);
  useEffect(() => { checkSession(); const expired = () => setSession(false); window.addEventListener('session-expired', expired); return () => window.removeEventListener('session-expired', expired); }, [checkSession]);
  useEffect(() => { if (session) load(); }, [session, load]);
  useEffect(() => { if (!session) return; const refresh = () => { if (!document.hidden && recordings.some(row => ['queued', 'running', 'processing', 'uploading'].includes(row.status) || ['queued', 'running', 'processing'].includes(row.latest_job?.status))) load(); }; const timer = setInterval(refresh, 12000); window.addEventListener('focus', load); return () => { clearInterval(timer); window.removeEventListener('focus', load); }; }, [session, recordings, load]);
  useEffect(() => setMobileNav(false), [route.path]);
  const beginUpload = useCallback(async (recordingId, file) => {
    if (activeUploads.current.has(String(recordingId))) return;
    const controller = new AbortController();
    activeUploads.current.set(String(recordingId), controller);
    const update = patch => setUploads(current => ({ ...current, [recordingId]: { ...current[recordingId], name: file.name, ...patch } }));
    rememberUpload(recordingId, file);
    update({ progress: 0, status: 'uploading', error: '' });
    try {
      const state = await api(`/recordings/${recordingId}/upload`);
      const received = new Set(state.received || state.chunks_received || []);
      const total = Math.ceil(file.size / CHUNK_SIZE);
      if (state.total_chunks && total !== state.total_chunks) throw new Error('This file does not match the interrupted upload. Choose the original recording.');
      update({ progress: received.size / total });
      for (let index = 0; index < total; index++) {
        if (controller.signal.aborted) throw new DOMException('Upload paused', 'AbortError');
        if (received.has(index)) continue;
        let sent = false;
        for (let attempt = 0; attempt < 3 && !sent; attempt++) {
          try { await api(`/recordings/${recordingId}/upload/${index}`, { method: 'PUT', body: file.slice(index * CHUNK_SIZE, (index + 1) * CHUNK_SIZE), headers: { 'Content-Type': 'application/octet-stream' }, signal: controller.signal }); sent = true; }
          catch (error) { if (controller.signal.aborted || attempt === 2) throw error; await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1))); }
        }
        received.add(index); update({ progress: received.size / total });
      }
      update({ status: 'finalizing', progress: 1 });
      await api(`/recordings/${recordingId}/upload/complete`, { method: 'POST' });
      await forgetUpload(recordingId);
      setUploads(current => { const next = { ...current }; delete next[recordingId]; return next; });
      notify('Upload complete. Your recording is queued for transcription.');
      await load();
      window.dispatchEvent(new Event('recording-updated'));
    } catch (error) {
      update({ status: controller.signal.aborted ? 'paused' : 'error', error: controller.signal.aborted ? '' : error.message });
      if (!controller.signal.aborted) notify(`Upload paused: ${error.message}`, true);
    } finally { activeUploads.current.delete(String(recordingId)); }
  }, [load, notify]);
  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    Object.entries(pendingUploads()).forEach(async ([id, meta]) => {
      try { const file = await fileStore('get', id); if (cancelled) return; if (file) beginUpload(id, file); else setUploads(current => ({ ...current, [id]: { name: meta.name, status: 'needs-file', progress: 0 } })); }
      catch { if (!cancelled) setUploads(current => ({ ...current, [id]: { name: meta.name, status: 'needs-file', progress: 0 } })); }
    });
    return () => { cancelled = true; };
  }, [session, beginUpload]);
  const resumeUpload = async (id, selectedFile) => {
    let file = selectedFile;
    if (!file) { try { file = await fileStore('get', id); } catch { /* File chooser remains available. */ } }
    if (!file) { setUploads(current => ({ ...current, [id]: { ...current[id], status: 'needs-file' } })); return; }
    const meta = pendingUploads()[id];
    if (meta && (meta.name !== file.name || meta.size !== file.size)) { notify(`Choose the original file: ${meta.name} (${size(meta.size)}).`, true); return; }
    try { await fileStore('put', id, file); } catch { /* Server chunks persist even when browser storage is full. */ }
    beginUpload(id, file);
  };
  const removeUpload = id => { activeUploads.current.get(String(id))?.abort(); forgetUpload(id); setUploads(current => { const next = { ...current }; delete next[id]; return next; }); };
  if (session === null) return <div className="boot"><Brand/>{sessionError ? <ErrorNotice message={sessionError} retry={checkSession}/> : <Loading/>}</div>;
  if (!session) return <Login onLogin={() => setSession(true)}/>;
  const recordingId = route.path.match(/^\/recordings\/([^/]+)/)?.[1];
  const isEditor = !!recordingId;
  return <div className={`app ${isEditor ? 'editor-layout' : ''}`}>
    {mobileNav && <button className="sidebar-scrim" aria-label="Close navigation" onClick={() => setMobileNav(false)}/>}
    <aside className={`sidebar ${mobileNav ? 'open' : ''}`}><Brand/>
      <div className="sidebar-caption">YOUR WORKSPACE</div>
      <nav aria-label="Main navigation">{[['/recordings', 'recordings', 'Recordings'], ['/search', 'search', 'Search transcripts'], ['/accuracy', 'chart', 'Accuracy']].map(([path, icon, label]) => <a key={path} href={`#${path}`} className={route.path.startsWith(path) ? 'active' : ''}><Icon name={icon}/><span>{label}</span>{path === '/recordings' && <small>{recordings.length}</small>}</a>)}</nav>
      <div className="sidebar-note"><span className="language-dots"><i>En</i><i>Xh</i><i>Af</i></span><p>Every voice.<br/>In its own words.</p><span>English · isiXhosa · Afrikaans</span></div>
      <div className="sidebar-bottom">{settings && <div className="quota-mini"><span><Icon name="clock" size={14}/> GPU this week <b>{Number(settings.gpu_hours || 0).toFixed(1)} <em>/ {settings.weekly_gpu_limit || 30}h</em></b></span><div className="progress"><i style={{ width: `${Math.min(100, (settings.gpu_hours || 0) / (settings.weekly_gpu_limit || 30) * 100)}%` }}/></div><small>Last 7 days · {settings.mode === 'local' ? 'Local worker' : 'Kaggle worker'}</small></div>}
        <nav><a href="#/settings" className={route.path === '/settings' ? 'active' : ''}><Icon name="settings"/>Settings</a><button onClick={async () => { try { await api('/logout', { method: 'POST' }); setSession(false); } catch (error) { notify(error.message, true); } }}><Icon name="logout"/>Lock workspace</button></nav><div className="private-note"><span/>Private workspace · Local storage</div>
      </div>
    </aside>
    <div className="main-shell"><div className="mobile-bar"><button className="icon-button" onClick={() => setMobileNav(true)} aria-label="Open navigation"><Icon name="menu"/></button><Brand compact/></div>
      <main>
        {route.path === '/search' ? <SearchPage navigate={navigate} notify={notify}/> : route.path === '/accuracy' ? <AccuracyPage/> : route.path === '/settings' ? <SettingsPage settings={settings} onSave={load} notify={notify}/> : recordingId ? <Editor key={recordingId} id={recordingId} initialTime={Number(route.query.get('t') || 0)} initialSegment={route.query.get('segment')} navigate={navigate} models={models} settings={settings} notify={notify} uploads={uploads} resumeUpload={resumeUpload} pauseUpload={id => activeUploads.current.get(String(id))?.abort()} onDelete={id => { removeUpload(id); load(); }} onUpdated={load}/> : <RecordingsPage recordings={recordings} loading={loading} error={loadError} load={load} openNew={() => setNewRecording(true)} navigate={navigate} uploads={uploads} resumeUpload={resumeUpload} pauseUpload={id => activeUploads.current.get(String(id))?.abort()}/>}
      </main>
    </div>
    {newRecording && <NewRecording close={() => setNewRecording(false)} notify={notify} create={async (values, file) => {
      const recording = await api('/recordings', { method: 'POST', body: { ...values, filename: file.name, size: file.size, last_modified: file.lastModified, total_chunks: Math.ceil(file.size / CHUNK_SIZE) } });
      rememberUpload(recording.id, file);
      try { await fileStore('put', recording.id, file); } catch { notify('Browser storage is limited. If interrupted, reselect this file to resume saved chunks.'); }
      setNewRecording(false); navigate(`/recordings/${recording.id}`); load(); beginUpload(recording.id, file);
    }}/>} 
    {toast && <div className={`toast ${toast.error ? 'error' : ''}`} role={toast.error ? 'alert' : 'status'}><Icon name={toast.error ? 'alert' : 'check'}/><span>{toast.message}</span><button className="icon-button" onClick={() => setToast(null)} aria-label="Dismiss notification"><Icon name="close" size={16}/></button></div>}
  </div>;
}

function Brand({ compact = false }) { return <a href="#/recordings" className={`brand ${compact ? 'compact' : ''}`}><span className="brand-mark"><Icon name="wave" size={24}/></span><span>Workshop{!compact && <small>TRANSCRIPTION STUDIO</small>}</span></a>; }
function Login({ onLogin }) {
  const [password, setPassword] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('');
  return <div className="login"><section className="login-story"><Brand/><div><span className="eyebrow">LISTEN CLOSELY. KEEP EVERY VOICE.</span><h1>Good conversations<br/>deserve a clear record.</h1><p>Your private studio for multilingual workshop recordings. Listen, refine and bring every voice into focus.</p><div className="login-wave">{Array.from({ length: 57 }, (_, index) => <i key={index} style={{ height: `${14 + Math.abs(Math.sin(index * 0.57) * Math.sin(index * 0.17)) * 100}px` }}/>)}</div><span className="login-languages">ENGLISH <span>·</span> ISIXHOSA <span>·</span> AFRIKAANS</span></div><small>Original voices. Thoughtfully reviewed.</small></section><section className="login-form"><form onSubmit={async event => { event.preventDefault(); setBusy(true); setError(''); try { await api('/login', { method: 'POST', body: { password } }); onLogin(); } catch (e) { setError(e.message); } finally { setBusy(false); } }}><span className="eyebrow">WELCOME BACK</span><h2>Open your workspace</h2><p>Enter your workspace password to continue.</p><label>Password<input type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" autoFocus required placeholder="Your workspace password"/></label>{error && <ErrorNotice message={error}/>}<Button className="primary full" busy={busy} type="submit" icon="arrow">Unlock workspace</Button><small>One workspace. Your recordings stay on your server.</small></form></section></div>;
}

function PageHeader({ eyebrow, title, subtitle, action }) { return <header className="page-header"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>{action}</header>; }
function ReviewProgress({ recording }) {
  const total = recording.segment_count ?? recording.total_segments ?? recording.segments_count ?? recording.segments?.length ?? 0;
  const reviewed = recording.reviewed_count ?? recording.reviewed_segments ?? recording.segments?.filter(segment => segment.review_status === 'reviewed').length ?? 0;
  const proportion = recording.review_progress != null ? (recording.review_progress > 1 ? recording.review_progress / 100 : recording.review_progress) : total ? reviewed / total : 0;
  return <div className="review-progress"><div className="progress"><i style={{ width: `${proportion * 100}%` }}/></div><span>{Math.round(proportion * 100)}% reviewed</span></div>;
}
function RecordingsPage({ recordings, loading, error, load, openNew, navigate, uploads, resumeUpload, pauseUpload }) {
  const [filter, setFilter] = useState('all'), [query, setQuery] = useState('');
  const ready = recordings.filter(recording => recording.status === 'ready').length;
  const working = recordings.filter(recording => ['uploading', 'queued', 'processing', 'running'].includes(recording.status)).length;
  const visible = recordings.filter(recording => (filter === 'all' || (filter === 'in-progress' ? ['queued', 'processing', 'uploading', 'running'].includes(recording.status) : recording.status === filter)) && `${recording.title} ${recording.workshop || ''}`.toLowerCase().includes(query.toLowerCase()));
  return <div className="page recordings-page"><PageHeader eyebrow="YOUR LIBRARY" title="Every conversation, captured." subtitle="Turn workshop recordings into transcripts you can trust." action={<Button className="primary" icon="plus" onClick={openNew}>New recording</Button>}/>
    <div className="library-summary"><div><span className="summary-icon"><Icon name="recordings" size={22}/></span><strong>{recordings.length}</strong><span>Total recordings</span></div><div><span className="summary-icon green"><Icon name="check" size={22}/></span><strong>{ready}</strong><span>Ready to review</span></div><div><span className="summary-icon gold"><Icon name="wave" size={22}/></span><strong>{working}</strong><span>In progress</span></div><p>A little listening.<br/><b>A lasting record.</b></p></div>
    {error && <ErrorNotice message={error} retry={load}/>}
    {Object.entries(uploads).filter(([, upload]) => ['error', 'paused', 'needs-file'].includes(upload.status)).map(([id, upload]) => <UploadNotice key={id} id={id} upload={upload} resume={resumeUpload} pause={pauseUpload}/>)}
    <div className="library-toolbar"><div className="tabs">{[['all', 'All recordings'], ['ready', 'Ready to review'], ['in-progress', 'In progress'], ['failed', 'Failed']].map(([key, label]) => <button key={key} className={filter === key ? 'selected' : ''} onClick={() => setFilter(key)}>{label}</button>)}</div><div className="search-input"><Icon name="search" size={17}/><input aria-label="Filter recordings" placeholder="Find a recording…" value={query} onChange={event => setQuery(event.target.value)}/></div></div>
    {loading ? <Loading/> : visible.length ? <div className="recording-table"><div className="recording-table-head"><span>RECORDING</span><span>DURATION</span><span>STATUS</span><span>REVIEW PROGRESS</span><span/></div>{visible.map((recording, index) => <a href={`#/recordings/${recording.id}`} className="recording-row" key={recording.id}><span className="recording-main"><span className={`recording-symbol tone-${index % 3}`}><Icon name="wave" size={24}/></span><span><strong>{recording.title}</strong><small>{recording.workshop || 'Workshop recording'}<i>·</i>{date(recording.date || recording.created_at)}</small></span></span><span className="recording-duration">{duration(recording.duration)}</span><span><Badge status={recording.status}/>{uploads[recording.id]?.status === 'uploading' && <small className="upload-percent">{Math.round(uploads[recording.id].progress * 100)}% uploaded</small>}</span><ReviewProgress recording={recording}/><Icon name="chevron" size={18}/></a>)}</div> : recordings.length ? <Empty icon="search" title="No recordings match">Try another search or change the status filter.</Empty> : <Empty title="A space for your first conversation" action={<Button className="primary" icon="upload" onClick={openNew}>Upload a recording</Button>}>Drop in a workshop recording. We’ll prepare the audio, separate the speakers and create a multilingual draft for you to review.</Empty>}
    <footer className="library-footer"><span><Icon name="file" size={15}/> Your original audio is always preserved.</span><span>English · isiXhosa · Afrikaans</span></footer>
  </div>;
}

function RecordingSettings({ value, onChange }) {
  const change = patch => onChange({ ...value, ...patch });
  return <><fieldset><legend>Languages spoken</legend><div className="language-options">{Object.entries(LANGUAGES).map(([code, label]) => <label key={code} className={value.allowed_languages?.includes(code) ? 'checked' : ''}><input type="checkbox" checked={value.allowed_languages?.includes(code) || false} onChange={event => change({ allowed_languages: event.target.checked ? [...(value.allowed_languages || []), code] : value.allowed_languages.filter(language => language !== code) })}/><span>{label}</span><small>{code.toUpperCase()}</small></label>)}</div><small className="field-hint">Each segment is compared across the selected languages. You make the final call.</small></fieldset><div className="form-grid"><label>Minimum speakers <span className="optional">optional</span><input type="number" min="1" max="100" value={value.min_speakers ?? ''} placeholder="Auto-detect" onChange={event => change({ min_speakers: event.target.value ? Number(event.target.value) : null })}/></label><label>Maximum speakers <span className="optional">optional</span><input type="number" min={value.min_speakers || 1} max="100" value={value.max_speakers ?? ''} placeholder="Auto-detect" onChange={event => change({ max_speakers: event.target.value ? Number(event.target.value) : null })}/></label></div><label>Audio used for transcription<select value={value.asr_input || 'original'} onChange={event => change({ asr_input: event.target.value })}><option value="original">Original audio (recommended)</option><option value="enhanced">Enhanced audio</option><option value="both">Both — keep alternate transcripts</option></select><small className="field-hint">Noise reduction creates a listening copy. Comparing both versions uses more processing time.</small></label><label>Custom word list <span className="optional">optional</span><textarea rows="3" placeholder={'Names, places and project terms\nOne per line'} value={Array.isArray(value.word_list) ? value.word_list.join('\n') : value.word_list || ''} onChange={event => change({ word_list: event.target.value.split('\n') })}/></label><label>Export redaction list <span className="optional">optional</span><textarea rows="2" placeholder="Names or sensitive terms, one per line" value={Array.isArray(value.redaction_list) ? value.redaction_list.join('\n') : value.redaction_list || ''} onChange={event => change({ redaction_list: event.target.value.split('\n') })}/><small className="field-hint">Anonymised exports replace these terms with [REDACTED].</small></label></>;
}
function NewRecording({ close, create }) {
  const [file, setFile] = useState(null), [drag, setDrag] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [values, setValues] = useState({ title: '', workshop: '', date: new Date().toLocaleDateString('en-CA'), notes: '', settings: { allowed_languages: ['en', 'xh', 'af'], min_speakers: null, max_speakers: null, asr_input: 'original', word_list: [], redaction_list: [] } });
  const selectFile = chosen => { if (!chosen) return; if (!/\.(m4a|mp3|wav|aac|ogg|flac|mp4|webm)$/i.test(chosen.name)) { setError('Choose an audio file: M4A, MP3, WAV, AAC, OGG, FLAC, MP4 or WebM.'); return; } setFile(chosen); setError(''); setValues(current => ({ ...current, title: current.title || chosen.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ') })); };
  const field = key => ({ value: values[key], onChange: event => setValues(current => ({ ...current, [key]: event.target.value })) });
  return <Modal title="New recording" subtitle="Give your conversation a home. The upload can resume if interrupted." close={close} wide><form onSubmit={async event => { event.preventDefault(); if (!file) { setError('Choose an audio file first.'); return; } if (!values.settings.allowed_languages.length) { setError('Select at least one spoken language.'); return; } setBusy(true); setError(''); try { await create(values, file); } catch (e) { setError(e.message); setBusy(false); } }}><div className="modal-body new-recording-grid"><section><label className={`dropzone ${drag ? 'drag' : ''} ${file ? 'has-file' : ''}`} onDragOver={event => { event.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={event => { event.preventDefault(); setDrag(false); selectFile(event.dataTransfer.files[0]); }}><input type="file" accept="audio/*,.m4a,.mp4,.webm" onChange={event => selectFile(event.target.files[0])}/><span className="drop-icon"><Icon name={file ? 'wave' : 'upload'} size={28}/></span><strong>{file ? file.name : 'Drop your recording here'}</strong><span>{file ? `${size(file.size)} · Click to change file` : <>or <b>browse files</b> on your computer</>}</span><small>M4A, MP3, WAV and other audio formats</small></label><label>Recording title<input required maxLength="250" placeholder="e.g. Community workshop · Day 1" {...field('title')}/></label><label>Workshop<input placeholder="e.g. Community voices project" {...field('workshop')}/></label><label>Recording date<input type="date" {...field('date')}/></label><label>Notes <span className="optional">optional</span><textarea rows="4" placeholder="Context for when you return to this conversation…" {...field('notes')}/></label></section><section className="processing-settings"><h3>Processing preferences</h3><RecordingSettings value={values.settings} onChange={settings => setValues(current => ({ ...current, settings }))}/></section></div><div className="modal-footer">{error ? <ErrorNotice message={error}/> : <span className="muted"><Icon name="check" size={15}/> Original audio stays untouched.</span>}<div className="button-group"><Button type="button" onClick={close} disabled={busy}>Cancel</Button><Button type="submit" className="primary" icon="upload" busy={busy}>Upload & transcribe</Button></div></div></form></Modal>;
}
function UploadNotice({ id, upload, resume, pause }) {
  const ref = useRef(null);
  const active = ['uploading', 'finalizing'].includes(upload.status);
  return <div className={`upload-notice ${upload.status === 'error' ? 'has-error' : ''}`}><span className="upload-notice-icon"><Icon name={active ? 'upload' : 'pause'} size={22}/></span><div><strong>{upload.status === 'finalizing' ? 'Preparing your recording…' : active ? `Uploading ${upload.name}` : `Upload paused · ${upload.name}`}</strong><span>{upload.error || (upload.status === 'needs-file' ? 'Reselect the same file to continue from the last saved chunk.' : active ? 'You can keep working. Completed chunks are saved automatically.' : 'Your completed chunks are saved. Resume whenever you’re ready.')}</span><div className="progress"><i style={{ width: `${(upload.progress || 0) * 100}%` }}/></div></div><strong className="upload-number">{Math.round((upload.progress || 0) * 100)}%</strong>{active ? <Button onClick={() => pause(id)} disabled={upload.status === 'finalizing'}>Pause</Button> : <Button icon="upload" onClick={() => upload.status === 'needs-file' ? ref.current?.click() : resume(id)}>Resume upload</Button>}<input ref={ref} hidden type="file" accept="audio/*,.m4a,.mp4" onChange={event => resume(id, event.target.files[0])}/></div>;
}

function Editor({ id, initialTime, initialSegment, navigate, models, settings, notify, uploads, resumeUpload, pauseUpload, onDelete, onUpdated }) {
  const [recording, setRecording] = useState(null), [error, setError] = useState('');
  const [activeId, setActiveId] = useState(initialSegment || null);
  const [currentTime, setCurrentTime] = useState(initialTime), [follow, setFollow] = useState(true), [loop, setLoop] = useState(false);
  const [threshold, setThreshold] = useState(0.65), [filter, setFilter] = useState({ status: 'all', speaker: 'all', language: 'all', text: '', low: false });
  const [modal, setModal] = useState(null), [busy, setBusy] = useState({}), [saveState, setSaveState] = useState('saved'), [logOpen, setLogOpen] = useState(false);
  const [cursor, setCursor] = useState({ id: null, position: 0 });
  const [page, setPage] = useState(0);
  const currentTimeRef = useRef(currentTime), cursorRef = useRef(cursor), visibleRef = useRef([]), tableTop = useRef(null);
  currentTimeRef.current = currentTime; cursorRef.current = cursor;
  const pending = useRef(new Map()), inFlight = useRef(new Map()), player = useRef(null), recordingRef = useRef(null), rowRefs = useRef(new Map()), version = useRef(0);
  recordingRef.current = recording;
  const load = useCallback(async () => {
    try {
      const fresh = await api(`/recordings/${id}`);
      fresh.segments = (fresh.segments || []).map(segment => ({ ...segment, ...(pending.current.get(segment.id)?.patch || {}) }));
      fresh.speakers ||= []; fresh.jobs ||= [];
      setRecording(fresh); setError('');
      setActiveId(current => current && fresh.segments.some(segment => segment.id === current) ? current : fresh.segments.find(segment => initialTime >= segment.start && initialTime < segment.end)?.id || fresh.segments[0]?.id || null);
      return fresh;
    } catch (e) { setError(e.message); return null; }
  }, [id, initialTime]);
  useEffect(() => { load(); const update = () => load(); window.addEventListener('recording-updated', update); const timer = setInterval(() => { const rec = recordingRef.current; if (rec && (['queued', 'processing', 'uploading', 'running'].includes(rec.status) || rec.jobs?.some(job => ['queued', 'running', 'processing'].includes(job.status)))) load(); }, 2500); return () => { clearInterval(timer); window.removeEventListener('recording-updated', update); }; }, [load]);
  const flush = useCallback(async segmentId => {
    if (inFlight.current.has(segmentId)) { await inFlight.current.get(segmentId); if (pending.current.has(segmentId)) return flush(segmentId); return; }
    const entry = pending.current.get(segmentId);
    if (!entry) return;
    clearTimeout(entry.timer);
    const promise = api(`/segments/${segmentId}`, { method: 'PATCH', body: entry.patch });
    inFlight.current.set(segmentId, promise);
    try {
      const updated = await promise;
      if (pending.current.get(segmentId)?.version === entry.version) pending.current.delete(segmentId);
      setRecording(current => current ? { ...current, segments: current.segments.map(segment => segment.id === segmentId ? { ...segment, ...updated, ...(pending.current.get(segmentId)?.patch || {}) } : segment) } : current);
      if (!pending.current.size) setSaveState('saved');
    } catch (e) { setSaveState('error'); notify(`Changes are still in this editor: ${e.message}. Retry saving before leaving.`, true); throw e; }
    finally { inFlight.current.delete(segmentId); }
  }, [notify]);
  const flushAll = useCallback(async () => { await Promise.all([...pending.current.keys()].map(flush)); }, [flush]);
  const save = useCallback((segmentId, patch, immediate = false) => {
    const previous = pending.current.get(segmentId);
    clearTimeout(previous?.timer);
    const entry = { patch: { ...previous?.patch, ...patch }, version: ++version.current };
    entry.timer = setTimeout(() => flush(segmentId).catch(() => {}), immediate ? 0 : 650);
    pending.current.set(segmentId, entry); setSaveState('saving');
    setRecording(current => ({ ...current, segments: current.segments.map(segment => segment.id === segmentId ? { ...segment, ...patch } : segment) }));
  }, [flush]);
  useEffect(() => { const leave = event => { if (pending.current.size) { event.preventDefault(); event.returnValue = ''; } }; window.addEventListener('beforeunload', leave); return () => { window.removeEventListener('beforeunload', leave); for (const entry of pending.current.values()) clearTimeout(entry.timer); flushAll().catch(() => {}); }; }, [flushAll]);
  const action = useCallback(async (key, path, body, method = 'POST', message) => {
    setBusy(current => ({ ...current, [key]: true }));
    try { await flushAll(); const result = await api(path, { method, ...(body !== undefined ? { body } : {}) }); await load(); if (message) notify(message); return result; }
    catch (e) { notify(e.message, true); return null; }
    finally { setBusy(current => ({ ...current, [key]: false })); }
  }, [flushAll, load, notify]);
  const segments = useMemo(() => recording?.segments || [], [recording?.segments]);
  const active = segments.find(segment => segment.id === activeId);
  const visible = useMemo(() => segments.filter(segment => (filter.status === 'all' || segment.review_status === filter.status) && (filter.speaker === 'all' || String(segment.speaker_id) === filter.speaker) && (filter.language === 'all' || segment.language === filter.language) && (!filter.low || (segment.words?.some(word => word.probability != null && word.probability < threshold) || (segment.confidence ?? 1) < threshold)) && (!filter.text || `${segment.edited_text ?? segment.text ?? ''} ${segment.edited_english ?? segment.english ?? ''}`.toLowerCase().includes(filter.text.toLowerCase()))), [segments, filter, threshold]);
  visibleRef.current = visible;
  const pageCount = Math.max(1, Math.ceil(visible.length / 30)), currentPage = Math.min(page, pageCount - 1);
  const pageSegments = useMemo(() => visible.slice(currentPage * 30, (currentPage + 1) * 30), [visible, currentPage]);
  useEffect(() => setPage(0), [filter, threshold]);
  useEffect(() => { const index = visibleRef.current.findIndex(segment => segment.id === activeId); if (index >= 0) setPage(Math.floor(index / 30)); }, [activeId]);
  useEffect(() => { const frame = requestAnimationFrame(() => rowRefs.current.get(activeId)?.scrollIntoView({ block: 'nearest', behavior: 'auto' })); return () => cancelAnimationFrame(frame); }, [activeId, currentPage]);
  const rowHandlers = useMemo(() => ({
    register: (segmentId, element) => element ? rowRefs.current.set(segmentId, element) : rowRefs.current.delete(segmentId),
    select: segmentId => setActiveId(segmentId),
    seek: (segmentId, value) => { setActiveId(segmentId); player.current?.seek(value); },
    save, flush: segmentId => flush(segmentId).catch(() => {}),
    cursor: (segmentId, position) => setCursor({ id: segmentId, position }),
    action: (segmentId, suffix, body, message) => action(segmentId, `/segments/${segmentId}${suffix}`, body, suffix === '' ? 'PATCH' : 'POST', message),
    compare: segment => setModal({ type: 'compare', segment }),
    split: (segment, position) => { const point = currentTimeRef.current, cursor = cursorRef.current; setActiveId(segment.id); setModal({ type: 'split', segment, position: position ?? (cursor.id === segment.id ? cursor.position : Math.floor((segment.edited_text || '').length / 2)), time: point > segment.start && point < segment.end ? point : null }); },
    remove: segment => setModal({ type: 'delete-segment', segment }),
  }), [save, flush, action]);
  const reviewed = useMemo(() => segments.filter(segment => segment.review_status === 'reviewed').length, [segments]);
  const outdated = useMemo(() => segments.filter(segment => segment.english_outdated).length, [segments]);
  const select = useCallback((segment, seek = false) => { if (!segment) return; setActiveId(segment.id); if (seek) { player.current?.seek(segment.start); setCurrentTime(segment.start); } rowRefs.current.get(segment.id)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }, []);
  const nextSegment = direction => { const index = segments.findIndex(segment => segment.id === activeId); select(segments[Math.max(0, Math.min(segments.length - 1, index + direction))], true); };
  const markReviewed = () => { if (!active) return; save(active.id, { review_status: 'reviewed', english_machine: false }, true); nextSegment(1); };
  const openSplit = () => { if (active) setModal({ type: 'split', segment: active, position: cursor.id === active.id ? cursor.position : Math.floor((active.edited_text || '').length / 2), time: currentTime > active.start && currentTime < active.end ? currentTime : null }); };
  const lowJump = direction => {
    const points = segments.flatMap(segment => segment.words?.length ? segment.words.filter(word => word.probability != null && word.probability < threshold).map(word => ({ time: word.start, segment })) : (segment.confidence ?? 1) < threshold ? [{ time: segment.start, segment }] : []).sort((a, b) => a.time - b.time);
    const point = direction > 0 ? points.find(item => item.time > currentTime + 0.05) || points[0] : [...points].reverse().find(item => item.time < currentTime - 0.1) || points[points.length - 1];
    if (!point) { notify('No words fall below this confidence threshold.'); return; }
    setActiveId(point.segment.id); player.current?.seek(point.time); rowRefs.current.get(point.segment.id)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  };
  useEffect(() => {
    const keyboard = event => {
      if (modal || !recording?.segments?.length || event.target.closest('[role="dialog"]')) return;
      const typing = event.target.matches('input,textarea,select,[contenteditable="true"]');
      if (event.key === 'Tab' && !event.shiftKey && !event.ctrlKey && !event.altKey && !typing) { event.preventDefault(); player.current?.toggle(); }
      else if (event.ctrlKey && event.key === 'Enter') { event.preventDefault(); markReviewed(); }
      else if (event.altKey && ['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); player.current?.seek(Math.max(0, currentTime + (event.key === 'ArrowLeft' ? -2 : 2))); }
      else if (event.ctrlKey && ['j', 'k'].includes(event.key.toLowerCase())) { event.preventDefault(); nextSegment(event.key.toLowerCase() === 'j' ? -1 : 1); }
      else if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === 's') { event.preventDefault(); openSplit(); }
      else if (event.ctrlKey && event.key.toLowerCase() === 'm') { event.preventDefault(); if (active) action(active.id, `/segments/${active.id}/merge`, {}, 'POST', 'Segments merged.'); }
      else if (!typing && event.key === '?') { event.preventDefault(); setModal({ type: 'help' }); }
    };
    document.addEventListener('keydown', keyboard);
    return () => document.removeEventListener('keydown', keyboard);
  });
  const seekPreview = useCallback(value => player.current?.seek(value), []);
  const onTime = useCallback(value => { setCurrentTime(value); if (follow && !document.activeElement?.matches('input,textarea,select')) { const current = recordingRef.current?.segments.find(segment => value >= segment.start && value < segment.end); if (current) setActiveId(previous => previous === current.id ? previous : current.id); } }, [follow]);
  if (!recording) return <div className="page">{error ? <ErrorNotice message={error} retry={load}/> : <Loading>Opening recording…</Loading>}</div>;
  const job = recording.jobs?.find(item => ['running', 'queued', 'processing'].includes(item.status)) || recording.jobs?.[0];
  const hasAudio = recording.status !== 'uploading';
  return <div className="editor-page"><header className="editor-header"><div className="editor-breadcrumb"><a href="#/recordings"><Icon name="back" size={16}/>Recordings</a><span>/</span><span>{recording.workshop || 'Workshop'}</span></div><div className="editor-title-row"><div><h1>{recording.title}</h1><p>{date(recording.date || recording.created_at)}<i>·</i>{duration(recording.duration)}<i>·</i>{recording.speakers.length || recording.preview?.speakers?.length || 0} speakers<Badge status={recording.status}/></p></div><div className="editor-header-actions"><button className={`save-state ${saveState}`} onClick={() => saveState === 'error' && flushAll().catch(() => {})} disabled={saveState !== 'error'}><Icon name={saveState === 'error' ? 'alert' : saveState === 'saving' ? 'refresh' : 'check'} size={14}/>{saveState === 'error' ? 'Retry saving' : saveState === 'saving' ? 'Saving…' : 'All changes saved'}</button><Button icon="settings" onClick={() => setModal({ type: 'recording-settings' })} aria-label="Recording settings" title="Recording settings"/><Button icon="download" onClick={() => setModal({ type: 'export' })} disabled={!segments.length && !recording.preview?.segments?.length}>Preview & export</Button></div></div></header>
    {error && <ErrorNotice message={error} retry={load}/>}
    {uploads[id] && <UploadNotice id={id} upload={uploads[id]} resume={resumeUpload} pause={pauseUpload}/>}
    {recording.status === 'uploading' && !uploads[id] && <UploadNotice id={id} upload={{ status: 'needs-file', name: recording.filename || 'your recording', progress: 0 }} resume={resumeUpload} pause={pauseUpload}/>}
    {(!segments.length || logOpen || recording.status === 'failed') && recording.status !== 'uploading' && <JobPanel recording={recording} job={job} settings={settings} busy={busy.job} action={async path => { await action('job', path, {}, 'POST'); onUpdated(); }}/>} 
    {hasAudio && <Player id={id} recording={recording} active={active} playerRef={player} initialTime={initialTime} onTime={onTime} follow={follow} setFollow={setFollow} loop={loop} setLoop={setLoop} notify={notify}/>}
    {!segments.length && recording.preview && <StagePreview preview={recording.preview} seek={seekPreview}/>}
    {segments.length > 0 ? <><div className="editor-toolbar"><div className="editor-tabs"><button className={!logOpen ? 'selected' : ''} onClick={() => setLogOpen(false)}>Transcript <span>{segments.length}</span></button><button className={logOpen ? 'selected' : ''} onClick={() => setLogOpen(value => !value)}>Processing log</button></div><div className="editor-tools"><span className="review-count"><i style={{ '--progress': `${reviewed / segments.length * 100}%` }}/><strong>{reviewed}</strong> / {segments.length} reviewed</span><button className="text-button" onClick={() => setModal({ type: 'speakers' })}><Icon name="users" size={16}/>Speakers</button><button className="icon-button" title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts" onClick={() => setModal({ type: 'help' })}><Icon name="help" size={18}/></button></div></div>
      <div className="transcript-controls"><div className="transcript-filters"><div className="search-input"><Icon name="search" size={15}/><input aria-label="Find in this transcript" placeholder="Find in transcript…" value={filter.text} onChange={event => setFilter({ ...filter, text: event.target.value })}/></div><select aria-label="Filter review status" value={filter.status} onChange={event => setFilter({ ...filter, status: event.target.value })}><option value="all">All statuses</option><option value="unreviewed">Unreviewed</option><option value="reviewed">Reviewed</option><option value="flagged">Flagged</option></select><select aria-label="Filter speaker" value={filter.speaker} onChange={event => setFilter({ ...filter, speaker: event.target.value })}><option value="all">All speakers</option>{recording.speakers.map(speaker => <option key={speaker.id} value={speaker.id}>{speaker.name}</option>)}</select><select aria-label="Filter language" value={filter.language} onChange={event => setFilter({ ...filter, language: event.target.value })}><option value="all">All languages</option>{Object.entries(LANGUAGES).map(([code, name]) => <option key={code} value={code}>{name}</option>)}</select><button className="text-button" onClick={() => setModal({ type: 'replace' })}>Find & replace</button></div><div className="confidence-controls"><label><input type="checkbox" checked={filter.low} onChange={event => setFilter({ ...filter, low: event.target.checked })}/>Low confidence only</label><span className="confidence-label">Highlight below <b>{Math.round(threshold * 100)}%</b></span><input type="range" min="0" max="1" step="0.05" value={threshold} aria-label="Low confidence threshold" onChange={event => setThreshold(Number(event.target.value))}/><div className="low-jumps"><button className="icon-button" onClick={() => lowJump(-1)} title="Previous low-confidence word" aria-label="Previous low-confidence word"><Icon name="back" size={15}/></button><button className="icon-button" onClick={() => lowJump(1)} title="Next low-confidence word" aria-label="Next low-confidence word"><Icon name="arrow" size={15}/></button></div><span className="controls-spacer"/>{outdated > 0 && <button className="text-button amber" disabled={busy.translate} onClick={() => action('translate', `/recordings/${id}/translate-outdated`, {}, 'POST', 'Refreshing outdated translations in the background.')}><Icon name="refresh" size={14}/>{busy.translate ? 'Queuing…' : `Refresh ${outdated} outdated translation${outdated === 1 ? '' : 's'}`}</button>}<button className="text-button" disabled={busy.add} onClick={async () => { const result = await action('add', `/recordings/${id}/segments`, { start: currentTime }, 'POST'); if (result?.id) setActiveId(result.id); }}><Icon name="plus" size={16}/>Add at playhead</button></div></div>
      <div className="transcript-table" ref={tableTop}><div className="transcript-head"><span>TIME / SPEAKER</span><span>ORIGINAL TRANSCRIPT <small>Click a word to listen</small></span><span>ENGLISH TRANSLATION</span><span>REVIEW</span></div>{visible.length ? pageSegments.map(segment => <SegmentRow key={segment.id} segment={segment} speakers={recording.speakers} active={segment.id === activeId} currentTime={segment.id === activeId ? currentTime : -1} threshold={threshold} busy={busy[segment.id]} handlers={rowHandlers} canMerge={segment.id !== segments[segments.length - 1]?.id}/>) : <Empty icon="search" title="No matching segments" action={<Button onClick={() => setFilter({ status: 'all', speaker: 'all', language: 'all', text: '', low: false })}>Clear filters</Button>}>Adjust the filters to see more of this conversation.</Empty>}</div><div className="transcript-pagination"><Button disabled={currentPage === 0} onClick={() => { setPage(currentPage - 1); tableTop.current?.scrollIntoView({ block: 'start' }); }}>Previous</Button><span>Page {currentPage + 1} of {pageCount} · Showing {visible.length ? currentPage * 30 + 1 : 0}–{Math.min((currentPage + 1) * 30, visible.length)} of {visible.length} matching segments</span><Button disabled={currentPage + 1 >= pageCount} onClick={() => { setPage(currentPage + 1); tableTop.current?.scrollIntoView({ block: 'start' }); }}>Next</Button></div><div className="editor-bottom"><span>{visible.length} of {segments.length} segments<span className="bottom-divider">·</span>Machine drafts need a human ear.</span><span><kbd>Tab</kbd> Play / pause <kbd>Ctrl ↵</kbd> Review & next</span></div>
    </> : recording.status === 'ready' ? <Empty title="No speech segments found" action={<Button icon="plus" onClick={() => action('add', `/recordings/${id}/segments`, { start: currentTime }, 'POST')}>Add a segment</Button>}>You can add a segment manually or adjust the processing preferences and retry the job.</Empty> : recording.status === 'uploading' ? <div className="upload-wait"><Icon name="wave" size={36}/><h2>Your next conversation starts here.</h2><p>When the upload finishes, transcription starts automatically.</p></div> : null}
    {modal?.type === 'help' && <ShortcutModal close={() => setModal(null)}/>}
    {modal?.type === 'export' && <ExportModal recording={recording} close={() => setModal(null)} notify={notify} flush={flushAll}/>}
    {modal?.type === 'speakers' && <SpeakersModal recording={recording} close={() => setModal(null)} action={action}/>}
    {modal?.type === 'replace' && <ReplaceModal id={id} close={() => setModal(null)} action={action}/>}
    {modal?.type === 'split' && <SplitModal {...modal} close={() => setModal(null)} split={async values => { const result = await action(modal.segment.id, `/segments/${modal.segment.id}/split`, values, 'POST', 'Segment split.'); if (result) setModal(null); }}/>} 
    {modal?.type === 'compare' && <CompareModal segment={recording.segments.find(segment => segment.id === modal.segment.id) || modal.segment} models={models} close={() => setModal(null)} action={async (suffix, body, message) => { const result = await action(modal.segment.id, `/segments/${modal.segment.id}${suffix}`, body, 'POST', message); if (result && suffix === '/choose-alternate') setModal(null); return result; }}/>} 
    {modal?.type === 'recording-settings' && <RecordingPreferences recording={recording} close={() => setModal(null)} save={async values => { const result = await action('preferences', `/recordings/${id}`, values, 'PATCH', 'Recording preferences saved.'); if (result) { setModal(null); onUpdated(); } }} remove={() => setModal({ type: 'delete-recording' })}/>} 
    {modal?.type === 'delete-segment' && <ConfirmDelete title="Delete this segment?" close={() => setModal(null)} busy={busy[modal.segment.id]} remove={async () => { const result = await action(modal.segment.id, `/segments/${modal.segment.id}`, undefined, 'DELETE'); if (result !== null) setModal(null); else { await load(); setModal(null); } }}>The transcript from {time(modal.segment.start)} to {time(modal.segment.end)} will be removed. The audio stays intact.</ConfirmDelete>}
    {modal?.type === 'delete-recording' && <ConfirmDelete title="Delete this recording?" close={() => setModal(null)} busy={busy.delete} remove={async () => { setBusy(current => ({ ...current, delete: true })); try { await api(`/recordings/${id}`, { method: 'DELETE' }); onDelete(id); navigate('/recordings'); notify('Recording and its files deleted.'); } catch (e) { notify(e.message, true); setBusy(current => ({ ...current, delete: false })); } }}>“{recording.title}”, its audio files, transcript and processing history will be permanently deleted.</ConfirmDelete>}
  </div>;
}

const StagePreview = React.memo(function StagePreview({ preview, seek }) {
  const [query, setQuery] = useState(''), [page, setPage] = useState(0);
  const speakers = Object.fromEntries((preview.speakers || []).map(speaker => [speaker.id, speaker.name]));
  const rows = (preview.segments || []).filter(row => !query || `${row.text} ${row.english} ${speakers[row.speaker] || ''}`.toLowerCase().includes(query.toLowerCase()));
  const pages = Math.max(1, Math.ceil(rows.length / 40)), current = Math.min(page, pages - 1);
  const complete = preview.recognition_total > 0 && preview.recognition_done === preview.recognition_total;
  const translated = (preview.segments || []).filter(row => row.translation_ready).length;
  return <section className="stage-preview" aria-label="Available processing results">
    <div className="stage-preview-heading"><div><h2>{complete ? 'Transcription draft available' : 'Partial transcription draft available'}</h2><p>Read-only preview. Listening, searching and draft exports are available now; editing becomes available after the final import.</p>{!complete && <p className="stage-preview-warning">This includes the completed model passes already received by this app. Additional model results may change the suggested text and language.</p>}</div></div>
    <div className="stage-preview-status"><span>{(preview.speakers || []).length} speakers</span><span>{preview.recognition_done || 0} / {preview.recognition_total || 0} recognition results received</span><span>{translated} / {(preview.segments || []).length} translations ready</span></div>
    <div className="stage-preview-speakers">{(preview.speakers || []).map(speaker => <span key={speaker.id}>{speaker.name}</span>)}</div>
    <label className="stage-preview-search">Search available draft<input type="search" value={query} onChange={event => { setQuery(event.target.value); setPage(0); }} placeholder="Find words or a speaker…"/></label>
    <div className="stage-preview-columns"><span>TIME / SPEAKER</span><span>ORIGINAL TRANSCRIPT</span><span>ENGLISH TRANSLATION</span></div>
    {rows.slice(current * 40, (current + 1) * 40).map(row => <article className="stage-preview-row" key={row.index}>
      <div><button className="timestamp" onClick={() => seek(row.start)}>{time(row.start)}</button><small>{speakers[row.speaker] || row.speaker}</small><small>{LANGUAGES[row.language] || row.language} · {row.model || 'Awaiting recognition'}</small></div>
      <div><p>{row.words?.length ? row.words.map((word, index) => <React.Fragment key={index}><button className="word" onClick={() => seek(word.start)}>{word.word}</button>{' '}</React.Fragment>) : row.text || <em>No usable text from the results received so far.</em>}</p>{row.alternates?.length > 0 && <details><summary>Other model drafts ({row.alternates.length})</summary>{row.alternates.map((alternate, index) => <div className="stage-preview-alternate" key={index}><small>{alternate.model} · {LANGUAGES[alternate.language] || alternate.language} · {Math.round((alternate.confidence || 0) * 100)}%</small><p>{alternate.text}</p></div>)}</details>}</div>
      <p>{row.translation_ready ? row.english || <em>No text to translate.</em> : <em>Translation not received yet.</em>}</p>
    </article>)}
    {!rows.length && <p className="stage-preview-empty">No matching text is available yet.</p>}
    <div className="stage-preview-pagination"><Button disabled={current === 0} onClick={() => setPage(current - 1)}>Previous</Button><span>Page {current + 1} of {pages} · {rows.length} segments</span><Button disabled={current + 1 >= pages} onClick={() => setPage(current + 1)}>Next</Button></div>
  </section>;
});

function Player({ id, recording, active, playerRef, initialTime, onTime, follow, setFollow, loop, setLoop, notify }) {
  const container = useRef(null), wavesurfer = useRef(null), playState = useRef({ time: initialTime, playing: false, speed: 1 }), activeRef = useRef(active), loopRef = useRef(loop), onTimeRef = useRef(onTime);
  const [version, setVersion] = useState('original'), [playing, setPlaying] = useState(false), [speed, setSpeed] = useState(1), [position, setPosition] = useState(initialTime), [length, setLength] = useState(recording.duration || 0), [ready, setReady] = useState(false), [error, setError] = useState('');
  activeRef.current = active; loopRef.current = loop; onTimeRef.current = onTime;
  const enhanced = !!(recording.enhanced_path || recording.enhanced_audio_path || recording.has_enhanced || recording.enhanced_file || recording.enhanced_url);
  useEffect(() => {
    let cancelled = false;
    const media = new Audio(); media.preload = 'metadata';
    const ws = WaveSurfer.create({ container: container.current, media, waveColor: '#a7b6ad', progressColor: '#345e50', cursorColor: '#bb7048', cursorWidth: 2, height: 63, barWidth: 2, barGap: 2, barRadius: 2, normalize: true, interact: true, hideScrollbar: true });
    wavesurfer.current = ws; setReady(false); setError('');
    const toggle = () => ws.playPause().catch(e => notify(e.message, true));
    const seek = seconds => { const next = Math.max(0, Math.min(seconds, ws.getDuration() || recording.duration || Infinity)); playState.current.time = next; if (ws.getDuration()) ws.setTime(next); setPosition(next); onTimeRef.current(next); };
    playerRef.current = { toggle, seek };
    ws.on('ready', total => { if (cancelled) return; setLength(total); setReady(true); ws.setPlaybackRate(playState.current.speed); ws.setTime(Math.min(playState.current.time, Math.max(0, total - 0.1))); if (playState.current.playing) ws.play().catch(() => {}); });
    let lastVisualUpdate = 0;
    ws.on('timeupdate', value => { if (cancelled) return; const segment = activeRef.current; if (loopRef.current && segment && ws.isPlaying() && (value >= segment.end || value < segment.start - 0.15)) { ws.setTime(segment.start); return; } playState.current.time = value; const now = performance.now(); if (now - lastVisualUpdate >= 100) { lastVisualUpdate = now; setPosition(value); onTimeRef.current(value); } });
    ws.on('play', () => { playState.current.playing = true; setPlaying(true); });
    ws.on('pause', () => { playState.current.playing = false; setPlaying(false); });
    ws.on('error', e => { if (!cancelled) { setError(e.message || 'Audio could not be loaded.'); setReady(false); } });
    (async () => {
      try { const data = await api(`/recordings/${id}/waveform?version=${version}`); if (!cancelled) await ws.load(`/api/recordings/${id}/audio?version=${version}`, Array.isArray(data.peaks?.[0]) ? data.peaks : [data.peaks], data.duration || recording.duration); }
      catch (e) { if (!cancelled) setError(`Audio preview is unavailable: ${e.message}`); }
    })();
    return () => { cancelled = true; const saved = { ...playState.current }; ws.destroy(); media.removeAttribute('src'); media.load(); playState.current = saved; wavesurfer.current = null; playerRef.current = null; };
  }, [id, version]);
  return <section className="player" aria-label="Audio player"><div className="player-top"><div className="player-source"><Icon name="volume" size={16}/><span>LISTENING COPY</span><div className="segmented"><button className={version === 'original' ? 'selected' : ''} onClick={() => setVersion('original')}>Original</button><button className={version === 'enhanced' ? 'selected' : ''} disabled={!enhanced} title={enhanced ? 'Listen to enhanced audio' : 'Available after enhancement finishes'} onClick={() => setVersion('enhanced')}>Enhanced</button></div></div><div className="player-options"><label><input type="checkbox" checked={follow} onChange={event => setFollow(event.target.checked)}/>Follow playback</label><button className={`text-button ${loop ? 'selected' : ''}`} disabled={!active} onClick={() => setLoop(!loop)} aria-pressed={loop}><Icon name="loop" size={16}/>Loop segment</button><select aria-label="Playback speed" value={speed} onChange={event => { const next = Number(event.target.value); setSpeed(next); playState.current.speed = next; wavesurfer.current?.setPlaybackRate(next); }}>{[0.5, 0.75, 1, 1.25, 1.5, 1.75, 2].map(value => <option key={value} value={value}>{value}×</option>)}</select></div></div><div className="player-main"><button className="play-button" aria-label={playing ? 'Pause audio' : 'Play audio'} disabled={!ready} onClick={() => playerRef.current?.toggle()}><Icon name={playing ? 'pause' : 'play'} size={23}/></button><div className="waveform-wrap"><div className="waveform" ref={container}/>{!ready && !error && <span className="waveform-loading"><span className="spinner small"/>Preparing waveform…</span>}{error && <span className="waveform-error">{error}</span>}<div className="waveform-labels"><span>00:00</span><span>{time(length / 4)}</span><span>{time(length / 2)}</span><span>{time(length * 3 / 4)}</span><span>{time(length)}</span></div></div><div className="player-time"><strong>{time(position)}</strong><span>/ {time(length)}</span></div></div><div className="player-seek"><button className="text-button" disabled={!ready} onClick={() => playerRef.current?.seek(position - 2)}>−2 sec</button><input type="range" min="0" max={length || 1} step="0.1" value={Math.min(position, length || 1)} aria-label="Audio position" disabled={!ready} onChange={event => playerRef.current?.seek(Number(event.target.value))}/><button className="text-button" disabled={!ready} onClick={() => playerRef.current?.seek(position + 2)}>+2 sec</button></div></section>;
}

const SegmentRow = React.memo(function SegmentRow({ segment, speakers, active, currentTime, threshold, busy, handlers, canMerge }) {
  const rowRef = useCallback(element => handlers.register(segment.id, element), [handlers, segment.id]);
  const select = () => handlers.select(segment.id), seek = value => handlers.seek(segment.id, value);
  const save = patch => handlers.save(segment.id, patch), flush = () => handlers.flush(segment.id);
  const setCursor = position => handlers.cursor(segment.id, position);
  const action = (suffix, body, message) => handlers.action(segment.id, suffix, body, message);
  const compare = () => handlers.compare(segment), split = position => handlers.split(segment, position), remove = () => handlers.remove(segment);
  const [editing, setEditing] = useState(null);
  const originalRef = useRef(null);
  const text = segment.edited_text ?? segment.text ?? '', english = segment.edited_english ?? segment.english ?? '';
  const words = segment.words || [];
  const textMatchesWords = text.trim().replace(/\s+/g, ' ') === words.map(word => word.word).join(' ').trim().replace(/\s+/g, ' ') || text.trim() === words.map(word => word.word).join('').trim();
  const activeWord = word => currentTime >= word.start && currentTime < word.end;
  const speakerIndex = speakers.findIndex(speaker => speaker.id === segment.speaker_id);
  return <article ref={rowRef} className={`segment-row ${active ? 'active' : ''} ${segment.review_status === 'reviewed' ? 'reviewed' : ''}`} onClick={select}>
    <div className="segment-meta"><button className="timestamp" onClick={() => seek(segment.start)} title="Play from this segment">{time(segment.start)}<Icon name="play" size={11}/></button><span className="end-time">{time(segment.end)}</span><div className={`speaker-select speaker-${Math.max(0, speakerIndex) % 5}`}><i/><select aria-label={`Speaker at ${time(segment.start)}`} value={segment.speaker_id ?? ''} onChange={event => save({ speaker_id: event.target.value || null })}><option value="">Unassigned</option>{speakers.map(speaker => <option key={speaker.id} value={speaker.id}>{speaker.name}</option>)}</select></div><select className="language-select" aria-label={`Language at ${time(segment.start)}`} value={segment.language || 'en'} disabled={busy} onChange={event => action('', { language: event.target.value }, 'Language updated. Re-transcribing this segment in the background.')} >{Object.entries(LANGUAGES).map(([code, name]) => <option key={code} value={code}>{name}</option>)}</select>{segment.suggested_language && segment.suggested_language !== segment.language && <span className="suggested-language">Suggested: {LANGUAGES[segment.suggested_language] || segment.suggested_language}</span>}</div>
    <div className="segment-original"><div className="text-cell-top"><span className="confidence-score">{segment.confidence != null ? `${Math.round(segment.confidence * 100)}% confidence` : 'Manual segment'}</span><button className="edit-text-button" onClick={() => setEditing(editing === 'original' ? null : 'original')} title="Edit original text"><Icon name="edit" size={13}/>{editing === 'original' ? 'Done' : 'Edit'}</button></div>{editing === 'original' ? <textarea className="inline-editor" ref={originalRef} autoFocus rows={Math.max(3, Math.ceil(text.length / 60))} aria-label={`Original text at ${time(segment.start)}`} value={text} onChange={event => { save({ edited_text: event.target.value, english_outdated: true }); setCursor(event.target.selectionStart); }} onSelect={event => setCursor(event.target.selectionStart)} onBlur={flush}/> : <p className="transcript-text" onDoubleClick={() => setEditing('original')}>{words.length && textMatchesWords ? words.map((word, index) => <React.Fragment key={index}><button className={`word ${(word.probability ?? 1) < threshold ? 'low' : ''} ${activeWord(word) ? 'playing' : ''}`} onClick={() => seek(word.start)} title={`${time(word.start)}${word.probability != null ? ` · ${Math.round(word.probability * 100)}% confidence` : ''}`}>{word.word.trim()}</button>{' '}</React.Fragment>) : text ? <button className={`segment-text-seek ${active && currentTime >= segment.start && currentTime < segment.end ? 'currently-playing' : ''}`} onClick={() => seek(segment.start)} title="Jump to segment start; word timing is unavailable for this edited text">{text}</button> : <button className="empty-text" onClick={() => setEditing('original')}>Add original text…</button>}</p>}{text && words.length > 0 && !textMatchesWords && editing !== 'original' && <span className="word-timing-note">Edited text · click to seek to segment start</span>}<div className="segment-actions"><button onClick={compare} className="text-button"><Icon name="compare" size={13}/>Compare{(segment.alternate_count ?? segment.alternates?.length) ? ` (${segment.alternate_count ?? segment.alternates.length})` : ''}</button><button onClick={() => split(editing === 'original' ? originalRef.current?.selectionStart : undefined)} className="text-button" title="Split segment"><Icon name="split" size={13}/>Split</button><button onClick={() => action('/merge', {}, 'Segments merged.')} className="text-button" disabled={!canMerge || busy} title="Merge with next segment"><Icon name="merge" size={13}/>Merge</button><button className="icon-button danger-hover" aria-label="Delete segment" title="Delete segment" onClick={remove}><Icon name="trash" size={13}/></button></div></div>
    <div className="segment-english"><div className="text-cell-top">{segment.english_outdated ? <span className="translation-label outdated"><span/>Outdated translation</span> : segment.english_machine !== false ? <span className="translation-label">Machine translation</span> : <span className="translation-label approved"><Icon name="check" size={12}/>Human reviewed</span>}<button className="edit-text-button" onClick={() => setEditing(editing === 'english' ? null : 'english')} title="Edit English translation"><Icon name="edit" size={13}/>{editing === 'english' ? 'Done' : 'Edit'}</button></div>{editing === 'english' ? <textarea className="inline-editor" autoFocus rows={Math.max(3, Math.ceil(english.length / 60))} aria-label={`English text at ${time(segment.start)}`} value={english} onChange={event => save({ edited_english: event.target.value, english_outdated: false, english_machine: false })} onBlur={flush}/> : <p className="transcript-text english-text" onDoubleClick={() => setEditing('english')}>{english || <button className="empty-text" onClick={() => setEditing('english')}>Add English translation…</button>}</p>}<div className="segment-actions"><button className={`text-button ${segment.english_outdated ? 'amber' : ''}`} disabled={busy} onClick={() => action('/translate', {}, 'Translation queued. It will appear here shortly.')}><Icon name="refresh" size={13}/>{busy ? 'Working…' : 'Re-translate'}</button>{segment.english_machine !== false && !segment.english_outdated && <button className="text-button" onClick={() => save({ english_machine: false })}><Icon name="check" size={13}/>Approve</button>}</div></div>
    <div className="segment-review"><button className={`review-button ${segment.review_status === 'reviewed' ? 'selected' : ''}`} aria-label={segment.review_status === 'reviewed' ? 'Mark unreviewed' : 'Mark reviewed'} title={segment.review_status === 'reviewed' ? 'Reviewed' : 'Mark reviewed'} onClick={() => save({ review_status: segment.review_status === 'reviewed' ? 'unreviewed' : 'reviewed', ...(segment.review_status !== 'reviewed' ? { english_machine: false } : {}) })}><Icon name="check" size={17}/></button><button className={`flag-button ${segment.review_status === 'flagged' ? 'selected' : ''}`} aria-label={segment.review_status === 'flagged' ? 'Remove flag' : 'Flag for review'} title="Flag for review" onClick={() => save({ review_status: segment.review_status === 'flagged' ? 'unreviewed' : 'flagged' })}><Icon name="flag" size={14}/></button><span>{segment.review_status === 'reviewed' ? 'Reviewed' : segment.review_status === 'flagged' ? 'Flagged' : 'To review'}</span></div>
  </article>;
});

function JobPanel({ recording, job, settings, busy, action }) {
  const logRef = useRef(null), [expanded, setExpanded] = useState(false);
  const [logs, setLogs] = useState(''), [logError, setLogError] = useState('');
  const status = job?.status || recording.status;
  useEffect(() => { if (!expanded || !job?.id) return; let cancelled = false; const loadLog = async () => { try { const data = await api(`/jobs/${job.id}`); if (!cancelled) { setLogs(data.log || ''); setLogError(''); } } catch (error) { if (!cancelled) setLogError(error.message); } }; loadLog(); const timer = ['queued', 'processing', 'running'].includes(status) ? setInterval(loadLog, 5000) : null; return () => { cancelled = true; if (timer) clearInterval(timer); }; }, [expanded, job?.id, status]);
  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, [logs]);
  const kaggleRef = job?.kaggle_reference || job?.kaggle_ref;
  const kaggleUrl = job?.kaggle_url || (kaggleRef ? `https://www.kaggle.com/code/${kaggleRef}` : null);
  return <section className="job-panel"><div className="job-summary"><span className={`job-icon ${status === 'failed' ? 'error' : ''}`}>{['processing', 'running', 'queued'].includes(status) ? <span className="spinner"/> : <Icon name={status === 'failed' ? 'alert' : status === 'cancelled' ? 'stop' : 'check'} size={24}/>}</span><div><h2>{status === 'queued' ? 'Your recording is in the queue' : ['processing', 'running'].includes(status) ? 'Bringing the conversation into focus' : status === 'failed' ? 'Processing needs your attention' : status === 'cancelled' ? 'Processing cancelled' : 'Processing complete'}</h2><p>{job?.step || (status === 'queued' ? 'The worker will start as soon as it is available.' : 'Your transcript and listening copy are ready to review.')}</p></div><div className="job-actions"><Badge status={status}/>{['failed', 'cancelled'].includes(status) && job && <Button className="primary" icon="refresh" busy={busy} onClick={() => action(`/jobs/${job.id}/retry`)}>Retry job</Button>}{['running', 'processing', 'queued'].includes(status) && job && <Button busy={busy} onClick={() => action(`/jobs/${job.id}/cancel`)}>Cancel</Button>}</div></div>{(job?.error || recording.error) && <ErrorNotice message={job?.error || recording.error}/>}<div className="job-stats"><span>Worker <b>{job?.mode || settings?.mode || 'Local'}</b></span><span>GPU usage <b>{Number(settings?.gpu_hours || 0).toFixed(2)} / {settings?.weekly_gpu_limit || 30} hours this week</b></span>{job?.gpu_seconds > 0 && <span>This job <b>{(job.gpu_seconds / 3600).toFixed(2)} GPU hours</b></span>}<button className="text-button" onClick={() => setExpanded(!expanded)}>{expanded ? 'Hide' : 'Show'} processing log <Icon name="chevron" size={14}/></button></div>{kaggleUrl && <p className="kaggle-note">{status === 'cancelled' ? 'The run was cancelled locally. Stop the remote kernel manually in Kaggle.' : 'Follow the remote worker in Kaggle.'} <a href={kaggleUrl} target="_blank" rel="noreferrer">Open Kaggle run ↗</a></p>}{expanded && <pre className="job-log" ref={logRef}>{logError || logs || 'Waiting for the worker to write its first log entry…'}</pre>}</section>;
}

function ConfirmDelete({ title, children, remove, close, busy }) { return <Modal title={title} close={close}><div className="modal-body"><p className="confirm-copy">{children}</p></div><div className="modal-footer"><span>This cannot be undone.</span><div className="button-group"><Button onClick={close} disabled={busy}>Keep it</Button><Button className="danger" icon="trash" busy={busy} onClick={remove}>Delete</Button></div></div></Modal>; }
function ShortcutModal({ close }) { return <Modal title="A quicker way to review" subtitle="Keep your hands on the keyboard and your ears on the conversation." close={close}><div className="modal-body shortcut-list">{[['Tab', 'Play / pause (outside text fields)'], ['Ctrl + Enter', 'Mark reviewed and go to the next segment'], ['Alt + ← / →', 'Seek backward / forward 2 seconds'], ['Ctrl + J / K', 'Previous / next segment'], ['Ctrl + Shift + S', 'Split the selected segment at the text cursor'], ['Ctrl + M', 'Merge the selected segment with the next'], ['?', 'Open this help']].map(([key, description]) => <div key={key}><span>{description}</span><kbd>{key}</kbd></div>)}<p className="field-hint">Click a word to hear it. Double-click a text block or use Edit to correct it. Changes save automatically.</p></div></Modal>; }
function ExportModal({ recording, close, notify, flush }) {
  const [values, setValues] = useState({ format: 'docx', language: 'both', speakers: true, timestamps: true, anonymize: false, layout: 'columns', font_size: 11 });
  const [busy, setBusy] = useState(false), [previewUrl, setPreviewUrl] = useState(''), [loading, setLoading] = useState(true), [error, setError] = useState(''), [exactPdf, setExactPdf] = useState(false), [revision, setRevision] = useState(0);
  const pdfPreview = values.format === 'pdf' && exactPdf;
  const update = patch => setValues(previous => ({ ...previous, ...patch }));
  const params = new URLSearchParams(Object.entries(values).map(([key, value]) => [key, String(key === 'timestamps' && ['srt', 'vtt'].includes(values.format) ? true : value)])).toString();
  useEffect(() => {
    const controller = new AbortController(); let url = '', cancelled = false;
    setLoading(true); setError(''); setPreviewUrl('');
    const timer = setTimeout(async () => {
      try {
        await flush();
        if (cancelled) return;
        const query = new URLSearchParams(params); query.set('format', pdfPreview ? 'pdf' : 'html'); query.set('inline', 'true');
        const response = await fetch(`/api/recordings/${recording.id}/export?${query}`, { credentials: 'same-origin', signal: controller.signal });
        if (!response.ok) { let message; try { message = (await response.json()).detail; } catch {} throw new Error(message || 'The preview could not be created.'); }
        const blob = await response.blob();
        if (!cancelled) { url = URL.createObjectURL(blob); setPreviewUrl(url); }
      } catch (failure) { if (!cancelled) setError(failure.message); }
      finally { if (!cancelled) setLoading(false); }
    }, 300);
    return () => { cancelled = true; clearTimeout(timer); controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [recording.id, params, pdfPreview, revision, flush]);
  const count = recording.segments?.length || recording.preview?.segments?.length || 0;
  const styled = ['docx', 'pdf'].includes(values.format);
  const subtitles = ['srt', 'vtt'].includes(values.format);
  const redactions = recording.settings?.redaction_list;
  const hasRedactions = (Array.isArray(redactions) ? redactions : String(redactions || '').split('\n')).some(term => term.trim());
  return <Modal wide title="Preview & export" subtitle="Read the entire transcript. Your choices apply to the preview and download." close={close}>
    <form className="export-workspace" onSubmit={async event => {
      event.preventDefault(); setBusy(true);
      try {
        await flush();
        const response = await fetch(`/api/recordings/${recording.id}/export?${params}`, { credentials: 'same-origin' });
        if (!response.ok) { let detail; try { detail = (await response.json()).detail; } catch {} throw new Error(detail || 'The export could not be created.'); }
        const blob = await response.blob(), url = URL.createObjectURL(blob), link = document.createElement('a');
        const filename = response.headers.get('Content-Disposition')?.match(/filename\*=UTF-8''(.+)/i)?.[1];
        link.href = url; link.download = filename ? decodeURIComponent(filename) : `transcript.${values.format}`;
        document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000); notify('Transcript exported.');
      } catch (failure) { notify(failure.message, true); } finally { setBusy(false); }
    }}>
      <aside className="export-settings">
        <fieldset><legend>File format</legend><div className="export-formats">{[['docx', 'Word', '.docx'], ['pdf', 'PDF', '.pdf'], ['txt', 'Plain text', '.txt'], ['srt', 'Subtitles', '.srt'], ['vtt', 'Web captions', '.vtt'], ['json', 'Full data', '.json']].map(([key, label, extension]) => <label key={key} className={values.format === key ? 'selected' : ''}><input type="radio" name="format" value={key} checked={values.format === key} onChange={() => update({ format: key })}/><Icon name="file" size={20}/><strong>{label}</strong><span>{extension}</span></label>)}</div></fieldset>
        <label>Transcript language<select value={values.language} onChange={event => update({ language: event.target.value })}><option value="both">Original + English</option><option value="original">Original language</option><option value="english">English translation</option></select></label>
        <div className="export-options">
          <label><input type="checkbox" checked={values.speakers} onChange={event => update({ speakers: event.target.checked })}/>Include speaker names</label>
          <label><input type="checkbox" checked={subtitles || values.timestamps} disabled={subtitles} onChange={event => update({ timestamps: event.target.checked })}/>Include timestamps</label>
          {subtitles && <p className="field-hint">Subtitle files always include timing cues.</p>}
          <label className="anonymize-option"><input type="checkbox" checked={values.anonymize} onChange={event => update({ anonymize: event.target.checked })}/><span><b>Anonymise this export</b><small>Use P1, P2… and redact your recording’s sensitive terms.</small></span></label>
        </div>
        {values.anonymize && !hasRedactions && <p className="field-hint">Speaker names will be anonymised. Add sensitive terms in Recording settings to redact them too.</p>}
        <fieldset><legend>{styled ? 'Document appearance' : 'Reading appearance'}</legend>
          {values.language === 'both' && <label>Language layout<select value={values.layout} onChange={event => update({ layout: event.target.value })}><option value="columns">Side by side</option><option value="stacked">Original, then English</option></select></label>}
          <label>Text size<select value={values.font_size} onChange={event => update({ font_size: Number(event.target.value) })}>{[[9, 'Compact'], [11, 'Standard'], [13, 'Large'], [16, 'Extra large']].map(([value, name]) => <option key={value} value={value}>{name} · {value} pt</option>)}</select></label>
          {!styled && <small className="field-hint">Appearance controls affect this reading preview. This file format stores text and data without document styling.</small>}
        </fieldset>
      </aside>
      <section className="export-preview" aria-label="Full transcript preview" aria-busy={loading}>
        <div className="export-preview-toolbar"><div><strong>{pdfPreview ? 'PDF preview' : 'Full transcript'}</strong><span>{count.toLocaleString()} segments · all pages included</span></div>{values.format === 'pdf' && <label><input type="checkbox" checked={exactPdf} onChange={event => setExactPdf(event.target.checked)}/>Show actual PDF</label>}</div>
        <p className="export-preview-note">{pdfPreview ? 'This is the same PDF that will be downloaded, including page breaks.' : 'Continuous reading view of every segment, independent of editor filters. Word and PDF downloads use your chosen layout and text size.'}</p>
        {loading && <Loading>Preparing the full transcript…</Loading>}
        {error && <ErrorNotice message={error} retry={() => setRevision(value => value + 1)}/>}
        {previewUrl && !loading && <iframe key={previewUrl} src={previewUrl} sandbox={pdfPreview ? undefined : ''} title={pdfPreview ? 'Complete PDF transcript' : 'Complete transcript reading preview'}/>}
      </section>
      <div className="modal-footer"><span>{count.toLocaleString()} segments · current saved text</span><div className="button-group"><Button type="button" onClick={close}>Close</Button><Button className="primary" icon="download" busy={busy} type="submit">Download {values.format.toUpperCase()}</Button></div></div>
    </form>
  </Modal>;
}

function SpeakersModal({ recording, close, action }) {
  const [names, setNames] = useState(() => Object.fromEntries(recording.speakers.map(speaker => [speaker.id, speaker.name]))), [newName, setNewName] = useState(''), [source, setSource] = useState(''), [target, setTarget] = useState(''), [busy, setBusy] = useState(null);
  return <Modal title="The voices in this room" subtitle="Names update everywhere in this recording." close={close}><div className="modal-body"><div className="speaker-edit-list">{recording.speakers.map((speaker, index) => <form key={speaker.id} className="speaker-edit-row" onSubmit={async event => { event.preventDefault(); setBusy(speaker.id); await action('speaker', `/speakers/${speaker.id}`, { name: names[speaker.id] }, 'PATCH', 'Speaker renamed.'); setBusy(null); }}><span className={`speaker-avatar speaker-${index % 5}`}>{index + 1}</span><input aria-label={`Name for speaker ${index + 1}`} value={names[speaker.id] ?? speaker.name} required onChange={event => setNames({ ...names, [speaker.id]: event.target.value })}/><Button type="submit" busy={busy === speaker.id} disabled={!names[speaker.id]?.trim() || names[speaker.id] === speaker.name}>Save</Button></form>)}</div><form className="add-speaker" onSubmit={async event => { event.preventDefault(); setBusy('add'); const result = await action('speaker', `/recordings/${recording.id}/speakers`, { name: newName }, 'POST', 'Speaker added.'); if (result) setNewName(''); setBusy(null); }}><label>Add a speaker<div className="input-action"><input placeholder="Speaker name" required value={newName} onChange={event => setNewName(event.target.value)}/><Button icon="plus" type="submit" busy={busy === 'add'} disabled={!newName.trim()}>Add</Button></div></label></form>{recording.speakers.length > 1 && <form className="merge-speakers" onSubmit={async event => { event.preventDefault(); setBusy('merge'); const result = await action('speaker', `/speakers/${source}/merge`, { target_id: target }, 'POST', 'Speakers merged.'); if (result) { setSource(''); setTarget(''); } setBusy(null); }}><h3>Merge two speakers</h3><p>Move all of one speaker’s segments into another speaker. The source speaker is removed.</p><div className="form-grid"><label>Merge this speaker<select required value={source} onChange={event => setSource(event.target.value)}><option value="">Choose source</option>{recording.speakers.map(speaker => <option key={speaker.id} value={speaker.id} disabled={String(speaker.id) === target}>{speaker.name}</option>)}</select></label><label>Into this speaker<select required value={target} onChange={event => setTarget(event.target.value)}><option value="">Choose destination</option>{recording.speakers.map(speaker => <option key={speaker.id} value={speaker.id} disabled={String(speaker.id) === source}>{speaker.name}</option>)}</select></label></div><Button icon="merge" type="submit" busy={busy === 'merge'} disabled={!source || !target || source === target}>Merge speakers</Button></form>}</div></Modal>;
}
function ReplaceModal({ id, close, action }) {
  const [values, setValues] = useState({ find: '', replace: '', field: 'both', case_sensitive: false }), [busy, setBusy] = useState(false), [result, setResult] = useState(null);
  return <Modal title="Find & replace" subtitle="Correct recurring names or terms across this recording." close={close}><form onSubmit={async event => { event.preventDefault(); setBusy(true); const data = await action('replace', `/recordings/${id}/replace`, values, 'POST'); if (data) setResult(data); setBusy(false); }}><div className="modal-body"><label>Find<input required autoFocus value={values.find} onChange={event => { setValues({ ...values, find: event.target.value }); setResult(null); }} placeholder="Word or phrase to replace"/></label><label>Replace with<input value={values.replace} onChange={event => setValues({ ...values, replace: event.target.value })} placeholder="Replacement (leave blank to remove)"/></label><label>Apply to<select value={values.field} onChange={event => setValues({ ...values, field: event.target.value })}><option value="both">Original text and English translation</option><option value="original">Original text only</option><option value="english">English translation only</option></select></label><label className="checkbox-line"><input type="checkbox" checked={values.case_sensitive} onChange={event => setValues({ ...values, case_sensitive: event.target.checked })}/>Match letter case</label>{result && <div className="success-notice"><Icon name="check"/>{result.changed ?? result.updated ?? result.count ?? result.segments_updated ?? 'Matching'} segments updated.</div>}<p className="field-hint">Replacing original text marks its English translation as outdated. The original machine drafts are preserved.</p></div><div className="modal-footer"><Button onClick={close} type="button">Close</Button><Button type="submit" className="primary" busy={busy} disabled={!values.find}>Replace all matches</Button></div></form></Modal>;
}
function SplitModal({ segment, position: initialPosition, time: splitTime, split, close }) {
  const text = segment.edited_text ?? segment.text ?? '';
  const [position, setPosition] = useState(Math.max(0, Math.min(text.length, initialPosition || 0))), [left, setLeft] = useState(segment.language || 'en'), [right, setRight] = useState(segment.language || 'en'), [when, setWhen] = useState(splitTime ?? ((segment.start + segment.end) / 2)), [busy, setBusy] = useState(false);
  return <Modal title="Split this segment" subtitle="Place the cursor between words. Each half can have its own language." close={close}><form onSubmit={async event => { event.preventDefault(); setBusy(true); await split({ position, time: Number(when), language_left: left, language_right: right }); setBusy(false); }}><div className="modal-body"><label>Click where the text should split<textarea rows="5" value={text} readOnly onSelect={event => setPosition(event.target.selectionStart)} ref={element => { if (element && document.activeElement !== element) element.setSelectionRange(position, position); }}/></label><div className="split-preview"><p>{text.slice(0, position) || <em>Empty first half</em>}</p><span><Icon name="split" size={18}/>Split at character {position}</span><p>{text.slice(position) || <em>Empty second half</em>}</p></div><label>Audio split point (seconds)<input type="number" min={segment.start + 0.01} max={segment.end - 0.01} step="0.01" required value={Number(when).toFixed(2)} onChange={event => setWhen(event.target.value)}/><small className="field-hint">Between {time(segment.start, true)} and {time(segment.end, true)}. Uses the playhead when it is inside the segment.</small></label><div className="form-grid"><label>First half language<select value={left} onChange={event => setLeft(event.target.value)}>{Object.entries(LANGUAGES).map(([code, name]) => <option key={code} value={code}>{name}</option>)}</select></label><label>Second half language<select value={right} onChange={event => setRight(event.target.value)}>{Object.entries(LANGUAGES).map(([code, name]) => <option key={code} value={code}>{name}</option>)}</select></label></div></div><div className="modal-footer"><Button type="button" onClick={close}>Cancel</Button><Button className="primary" type="submit" icon="split" busy={busy}>Split segment</Button></div></form></Modal>;
}
function CompareModal({ segment, models, action, close }) {
  const [model, setModel] = useState(models[0]?.key || ''), [language, setLanguage] = useState(segment.language || 'en'), [busy, setBusy] = useState(null);
  const [details, setDetails] = useState(segment), [loading, setLoading] = useState(true), [loadError, setLoadError] = useState('');
  useEffect(() => { let cancelled = false; setLoading(true); api(`/segments/${segment.id}`).then(data => { if (!cancelled) { setDetails(data); setLoadError(''); } }).catch(error => { if (!cancelled) setLoadError(error.message); }).finally(() => { if (!cancelled) setLoading(false); }); return () => { cancelled = true; }; }, [segment.id, segment.revision]);
  return <Modal title="Compare the alternatives" subtitle={`${time(segment.start)} – ${time(segment.end)} · Choose the draft that best matches what you hear.`} close={close} wide><div className="modal-body"><div className="current-transcript"><span className="eyebrow">CURRENT REVIEW TEXT</span><p>{segment.edited_text || <em>No text yet</em>}</p><small>{LANGUAGES[segment.language] || segment.language} · {segment.model || 'Manual'} · {segment.audio_version || 'original'} audio</small></div><div className="alternate-list">{loading ? <Loading>Loading alternate drafts...</Loading> : loadError ? <ErrorNotice message={loadError}/> : details.alternates?.length ? details.alternates.map((alternate, index) => <div className="alternate" key={index}><div><span className="alternate-language">{LANGUAGES[alternate.language] || alternate.language}</span><span>{alternate.model || 'Model unspecified'} · {alternate.audio_version || 'original'} audio</span>{alternate.confidence != null && <b>{Math.round(alternate.confidence * 100)}%</b>}</div><p>{alternate.text || <em>No text returned</em>}</p><Button busy={busy === index} disabled={!alternate.text} onClick={async () => { setBusy(index); await action('/choose-alternate', { index }, 'Alternative selected. English translation is being refreshed.'); setBusy(null); }}>Use this version<Icon name="arrow" size={15}/></Button></div>) : <p className="field-hint">There are no alternate drafts for this segment yet. Run another model below to compare its output.</p>}</div><form className="rerun-model" onSubmit={async event => { event.preventDefault(); setBusy('rerun'); await action('/retranscribe', { language, model_key: model }, 'Re-transcription queued. The current text is kept as an alternate.'); setBusy(null); }}><h3>Give it another listen</h3><p>Re-transcribe this segment with a model from your model registry.</p><div className="rerun-fields"><label>Model<select required value={model} onChange={event => setModel(event.target.value)}>{models.map(item => <option key={item.key} value={item.key}>{item.label || item.key}</option>)}</select></label><label>Language<select value={language} onChange={event => setLanguage(event.target.value)}>{Object.entries(LANGUAGES).map(([code, name]) => <option key={code} value={code}>{name}</option>)}</select></label><Button icon="refresh" className="primary" type="submit" busy={busy === 'rerun'} disabled={!model}>Re-transcribe</Button></div></form></div></Modal>;
}
function RecordingPreferences({ recording, save, remove, close }) {
  const [values, setValues] = useState({ title: recording.title, workshop: recording.workshop || '', date: recording.date || '', notes: recording.notes || '', settings: { ...recording.settings } }), [busy, setBusy] = useState(false);
  return <Modal title="Recording settings" subtitle="Keep the context and processing preferences with the recording." close={close} wide><form onSubmit={async event => { event.preventDefault(); if (!values.settings.allowed_languages?.length) return; setBusy(true); await save(values); setBusy(false); }}><div className="modal-body new-recording-grid"><section><label>Recording title<input required value={values.title} onChange={event => setValues({ ...values, title: event.target.value })}/></label><label>Workshop<input value={values.workshop} onChange={event => setValues({ ...values, workshop: event.target.value })}/></label><label>Recording date<input type="date" value={values.date.slice(0, 10)} onChange={event => setValues({ ...values, date: event.target.value })}/></label><label>Notes<textarea rows="6" value={values.notes} onChange={event => setValues({ ...values, notes: event.target.value })}/></label><div className="delete-recording"><h3>Remove recording</h3><p>Delete the audio, transcript and processing history from this workspace.</p><Button type="button" className="danger-text" icon="trash" onClick={remove}>Delete recording</Button></div></section><section className="processing-settings"><RecordingSettings value={values.settings} onChange={settings => setValues({ ...values, settings })}/><p className="field-hint">Processing preferences apply to the next full job. Existing transcript edits stay as they are.</p></section></div><div className="modal-footer"><span>{!values.settings.allowed_languages?.length && 'Select at least one language.'}</span><Button className="primary" type="submit" busy={busy} disabled={!values.settings.allowed_languages?.length}>Save preferences</Button></div></form></Modal>;
}

function SearchPage({ navigate, notify }) {
  const [query, setQuery] = useState(''), [results, setResults] = useState([]), [busy, setBusy] = useState(false), [searched, setSearched] = useState(''), [error, setError] = useState('');
  const search = async event => { event.preventDefault(); if (!query.trim()) return; setBusy(true); setError(''); try { const data = await api(`/search?q=${encodeURIComponent(query.trim())}`); setResults(asArray(data, 'results')); setSearched(query.trim()); } catch (e) { setError(e.message); } finally { setBusy(false); } };
  return <div className="page search-page"><PageHeader eyebrow="ACROSS YOUR WORKSPACE" title="Find the moment." subtitle="Search every conversation, in the original language and in English."/><form className="global-search" onSubmit={search}><Icon name="search" size={22}/><input autoFocus aria-label="Search all transcripts" placeholder="A name, an idea, a phrase…" value={query} onChange={event => setQuery(event.target.value)}/><Button className="primary" type="submit" busy={busy} disabled={!query.trim()}>Search transcripts</Button></form>{error && <ErrorNotice message={error}/>}<div className="search-results-heading">{searched && <><strong>{results.length} result{results.length === 1 ? '' : 's'}</strong><span>for “{searched}”</span></>}</div>{results.length ? <div className="search-results">{results.map((result, index) => <button key={result.segment_id || result.id || index} className="search-result" onClick={() => navigate(`/recordings/${result.recording_id}?t=${result.start || 0}&segment=${result.segment_id || result.id || ''}`)}><div><span className="result-recording"><Icon name="recordings" size={16}/>{result.recording_title || result.title || result.recording?.title}</span><span className="result-time">{time(result.start)}<Icon name="arrow" size={16}/></span></div><p>{result.edited_text || result.text || result.snippet || ''}</p>{(result.edited_english || result.english) && <p className="result-english">{result.edited_english || result.english}</p>}<small>{result.speaker_name || result.speaker || 'Unassigned speaker'}<i>·</i>{LANGUAGES[result.language] || result.language || 'Transcript'}</small></button>)}</div> : searched && !busy ? <Empty icon="search" title="That phrase hasn’t surfaced yet">Try fewer words, a different spelling or its English translation.</Empty> : !busy ? <Empty icon="search" title="There’s a conversation behind every word">Find a moment across your reviewed and unreviewed transcripts, then jump straight to the audio.</Empty> : <Loading>Searching your conversations…</Loading>}</div>;
}
function AccuracyPage() {
  const [data, setData] = useState(null), [error, setError] = useState('');
  const load = useCallback(() => api('/accuracy').then(result => { setData(result); setError(''); }).catch(e => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);
  const groups = data?.groups || [], total = data?.reviewed_segments || 0;
  const percent = value => `${(Number(value || 0) * 100).toFixed(1)}%`;
  return <div className="page accuracy-page"><PageHeader eyebrow="LEARN FROM YOUR CORRECTIONS" title="A clearer picture of accuracy." subtitle="See how each model performs against the transcripts you’ve reviewed." action={<Button icon="refresh" onClick={load}>Refresh</Button>}/>{error && <ErrorNotice message={error} retry={load}/>}{!data && !error ? <Loading>Comparing drafts with your reviewed text…</Loading> : <><div className="accuracy-intro"><div><span className="eyebrow">THE HUMAN BENCHMARK</span><strong>{total}</strong><p>reviewed segment{total === 1 ? '' : 's'} in the comparison</p></div><div><h3>Lower error rates mean closer drafts.</h3><p>Word error rate (WER) counts word substitutions, insertions and deletions. Character error rate (CER) makes smaller spelling differences visible. Only segments marked reviewed are included.</p></div></div>{groups.length ? <div className="accuracy-table"><div className="accuracy-table-head"><span>MODEL / LANGUAGE</span><span>REVIEWED</span><span>WORD ERROR RATE</span><span>CHARACTER ERROR RATE</span></div>{groups.map((group, index) => <div className="accuracy-row" key={`${group.model}-${group.language}-${index}`}><div><strong>{group.model || 'Unspecified model'}</strong><small>{LANGUAGES[group.language] || group.language}</small></div><span>{group.segments ?? group.count}</span><div><strong>{percent(group.wer)}</strong><div className="metric-bar"><i style={{ width: `${Math.min(100, Number(group.wer || 0) * 100)}%` }}/></div></div><div><strong>{percent(group.cer)}</strong><div className="metric-bar cer"><i style={{ width: `${Math.min(100, Number(group.cer || 0) * 100)}%` }}/></div></div></div>)}</div> : <Empty icon="chart" title="Your reviews will tell the story" action={<a className="button primary" href="#/recordings">Start reviewing<Icon name="arrow"/></a>}>Correct a few segments and mark them reviewed. Their machine drafts will be compared with your final text here.</Empty>}<div className="accuracy-footnote"><Icon name="help" size={19}/><p>These figures describe your reviewed sample, not every recording. A model’s score can change as you review more speakers, languages and recording conditions. Error rates can exceed 100% when the draft contains many extra words.</p></div></>}</div>;
}
function SettingsPage({ settings, onSave, notify }) {
  const [values, setValues] = useState(null), [busy, setBusy] = useState(false);
  useEffect(() => { if (settings && !values) setValues({ mode: settings.mode || 'kaggle', weekly_gpu_limit: settings.weekly_gpu_limit ?? 30 }); }, [settings, values]);
  if (!settings || !values) return <div className="page"><Loading>Loading settings…</Loading></div>;
  const credentials = settings.credentials || {};
  return <div className="page settings-page"><PageHeader eyebrow="MAKE IT YOUR WORKSPACE" title="Settings" subtitle="Choose where recordings are processed and keep an eye on your resources."/><form onSubmit={async event => { event.preventDefault(); setBusy(true); try { await api('/settings', { method: 'PATCH', body: values }); await onSave(); notify('Workspace settings saved.'); } catch (e) { notify(e.message, true); } finally { setBusy(false); } }}><section className="settings-section"><div><h2>Processing location</h2><p>New jobs use this setting. Both modes run the same audio and transcription pipeline.</p></div><fieldset className="mode-options"><legend className="sr-only">Processing mode</legend><label className={values.mode === 'kaggle' ? 'selected' : ''}><input type="radio" name="mode" checked={values.mode === 'kaggle'} onChange={() => setValues({ ...values, mode: 'kaggle' })}/><span><b>Kaggle GPU</b><small>Process in a private Kaggle notebook using your account’s GPU allowance. Audio and the worker credentials are sent to a private dataset for the run.</small></span></label><label className={values.mode === 'local' ? 'selected' : ''}><input type="radio" name="mode" checked={values.mode === 'local'} onChange={() => setValues({ ...values, mode: 'local' })}/><span><b>Local worker</b><small>Process on this host with the separate worker environment. Uses a compatible GPU when available.</small></span></label></fieldset></section><section className="settings-section"><div><h2>Weekly GPU allowance</h2><p>Track estimated GPU time from jobs in the last seven days.</p></div><div><label>Weekly limit (hours)<input type="number" min="0.1" max="1000" step="0.1" required value={values.weekly_gpu_limit} onChange={event => setValues({ ...values, weekly_gpu_limit: Number(event.target.value) })}/></label><div className="gpu-usage"><span><strong>{Number(settings.gpu_hours || 0).toFixed(2)}h</strong><span>of {values.weekly_gpu_limit}h used</span></span><div className="progress"><i style={{ width: `${Math.min(100, (settings.gpu_hours || 0) / (values.weekly_gpu_limit || 1) * 100)}%` }}/></div><small>Usage is an estimate from stored job records.</small></div></div></section><section className="settings-section"><div><h2>Connected credentials</h2><p>Credentials are managed through the server’s environment variables.</p></div><div className="credentials">{[['Hugging Face', 'HF_TOKEN', credentials.hf_token], ['Kaggle username', 'KAGGLE_USERNAME', credentials.kaggle_username], ['Kaggle API key', 'KAGGLE_KEY', credentials.kaggle_key]].map(([label, env, configured]) => <div key={env}><span><b>{label}</b><small>{env}</small></span><Badge status={configured ? 'ready' : 'missing'}>{configured ? 'Configured' : 'Not configured'}</Badge></div>)}<p className="field-hint">Speaker separation also requires accepting the pyannote model’s access terms with the Hugging Face account used by your token.</p></div></section><div className="settings-save"><Button className="primary" type="submit" busy={busy}>Save settings</Button></div></form></div>;
}
