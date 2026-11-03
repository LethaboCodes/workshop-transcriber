// Resolve overlaps deterministically and keep navigation useful in gaps and at EOF.
export function segmentAtTime(segments, seconds, preferredId) {
  if (!segments.length || !Number.isFinite(seconds)) return null;
  const contains = row => seconds >= row.start && seconds < row.end;
  const preferred = preferredId && segments.find(row => row.id === preferredId && seconds >= row.start && seconds <= row.end);
  if (preferred) return preferred;
  let match = null, closest = null, distance = Infinity;
  for (const row of segments) {
    if (contains(row) && (!match || row.start >= match.start)) match = row;
    const gap = Math.max(row.start - seconds, seconds - row.end, 0);
    if (gap < distance || (gap === distance && (!closest || row.start >= closest.start))) {
      closest = row; distance = gap;
    }
  }
  return match || closest;
}

export function parseTranscriptTime(value) {
  const parts = String(value).trim().split(':');
  if (parts.length < 2 || parts.length > 3 || parts.some(part => !/^\d+$/.test(part))) return null;
  const values = parts.map(Number);
  if (values.at(-1) >= 60 || (parts.length === 3 && values[1] >= 60)) return null;
  const seconds = values.reduce((total, part) => total * 60 + part, 0);
  return Number.isSafeInteger(seconds) ? seconds : null;
}

export function insertTranscriptMarker(text, start, end, marker) {
  start = Math.max(0, Math.min(start, text.length));
  end = Math.max(start, Math.min(end, text.length));
  const before = text.slice(0, start), after = text.slice(end);
  const left = before && !/\s$/.test(before) ? ' ' : '';
  const right = after && !/^[\s,.;:!?)]/.test(after) ? ' ' : '';
  return before + left + marker + right + after;
}

export function transcriptParts(text) {
  const expression = /\[(inaudible|crosstalk)\s+((?:\d+:)?\d+:\d{2})\]/gi;
  const parts = []; let previous = 0;
  for (const match of text.matchAll(expression)) {
    const seconds = parseTranscriptTime(match[2]);
    if (seconds === null) continue;
    if (match.index > previous) parts.push({ text: text.slice(previous, match.index) });
    parts.push({ text: match[0], seconds, kind: match[1].toLowerCase() });
    previous = match.index + match[0].length;
  }
  if (previous < text.length) parts.push({ text: text.slice(previous) });
  return parts;
}
