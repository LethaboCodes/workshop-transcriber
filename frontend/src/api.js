export const CHUNK_SIZE = 8 * 1024 * 1024;
export const LANGUAGES = { en: 'English', xh: 'isiXhosa', af: 'Afrikaans' };

export async function api(path, options = {}) {
  const { body, ...rest } = options;
  const response = await fetch(`/api${path}`, {
    credentials: 'same-origin',
    ...rest,
    headers: { ...(body !== undefined && !(body instanceof Blob) ? { 'Content-Type': 'application/json' } : {}), ...rest.headers },
    ...(body !== undefined ? { body: body instanceof Blob ? body : JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch { /* The HTTP status still explains a non-JSON response. */ }
    if (response.status === 401 && path !== '/login') window.dispatchEvent(new Event('session-expired'));
    const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(item => item.msg || String(item)).join('; ') : `Request failed (${response.status})`;
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

export function time(seconds = 0, decimals = false) {
  const value = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor(value % 3600 / 60);
  const secs = Math.floor(value % 60);
  const text = `${hours ? `${hours}:` : ''}${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  return decimals ? `${text}.${Math.floor((value % 1) * 10)}` : text;
}
export function duration(seconds) {
  if (!seconds) return 'Awaiting audio';
  return seconds >= 3600 ? `${Math.floor(seconds / 3600)}h ${Math.round(seconds % 3600 / 60)}m` : `${Math.max(1, Math.round(seconds / 60))} min`;
}
export function size(bytes = 0) { return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`; }
export function date(value) { return value ? new Date(value.length === 10 ? `${value}T12:00:00` : value).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : 'No date'; }
export function asArray(data, key) { return Array.isArray(data) ? data : data?.[key] || []; }
export function pendingUploads() { try { return JSON.parse(localStorage.getItem('workshop-uploads') || '{}'); } catch { return {}; } }
export function rememberUpload(id, file) {
  localStorage.setItem('workshop-uploads', JSON.stringify({ ...pendingUploads(), [id]: { name: file.name, size: file.size, lastModified: file.lastModified } }));
}
export function forgetUpload(id) {
  const pending = pendingUploads();
  delete pending[id];
  localStorage.setItem('workshop-uploads', JSON.stringify(pending));
  return fileStore('delete', id).catch(() => {});
}
export async function fileStore(operation, id, file) {
  const db = await new Promise((resolve, reject) => {
    const request = indexedDB.open('workshop-transcriber', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('uploads');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('uploads', operation === 'get' ? 'readonly' : 'readwrite');
    const store = transaction.objectStore('uploads');
    const request = operation === 'put' ? store.put(file, String(id)) : operation === 'delete' ? store.delete(String(id)) : store.get(String(id));
    let result;
    request.onsuccess = () => { result = request.result; };
    transaction.oncomplete = () => { db.close(); resolve(result); };
    transaction.onerror = () => { db.close(); reject(transaction.error); };
    transaction.onabort = () => { db.close(); reject(transaction.error || new Error('Local file storage interrupted')); };
  });
}
