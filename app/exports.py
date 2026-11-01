"""Reviewed transcript exports; redaction is applied to every exported text field."""
import io
import json
import re

from docx import Document
from docx.shared import Inches, Pt


def timestamp(seconds, separator="."):
    milliseconds = max(0, round(float(seconds or 0) * 1000))
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{millis:03}"


def export_transcript(recording, segments, speakers, format="docx", language="both", include_speakers=True, include_timestamps=True, anonymize=False):
    redactions = recording.settings.get("redaction_list", []) if anonymize else []
    if isinstance(redactions, str):
        redactions = redactions.splitlines()
    terms = sorted({str(term).strip() for term in redactions if str(term).strip()}, key=len, reverse=True)
    expression = re.compile("|".join(re.escape(term) for term in terms), re.IGNORECASE) if terms else None

    def redact(value):
        value = str(value or "")
        return expression.sub("[REDACTED]", value) if expression else value

    speaker_names = {speaker.id: f"P{index + 1}" if anonymize else redact(speaker.name) for index, speaker in enumerate(speakers)}
    rows = []
    for segment in segments:
        rows.append({"start": segment.start, "end": segment.end, "language": segment.language, "speaker": speaker_names.get(segment.speaker_id, "Unknown"), "original": redact(segment.edited_text), "english": redact(segment.edited_english), "review_status": segment.review_status, "english_outdated": segment.english_outdated, "english_machine": segment.english_machine})

    def chosen_text(row):
        if language == "original":
            return row["original"]
        if language == "english":
            return row["english"]
        return row["original"] + "\n" + row["english"]

    def heading(row):
        fields = []
        if include_timestamps:
            fields.append(f"[{timestamp(row['start'])} – {timestamp(row['end'])}]")
        if include_speakers:
            fields.append(row["speaker"])
        return " · ".join(fields)

    safe_name = re.sub(r"[^\w\- .]", "", redact(recording.title)).strip(" .")[:100] or "transcript"
    if format == "json":
        output = []
        for row in rows:
            item = {key: value for key, value in row.items() if key not in {"original", "english", "speaker", "start", "end"}}
            if include_timestamps:
                item.update(start=row["start"], end=row["end"])
            if include_speakers:
                item["speaker"] = row["speaker"]
            if language in {"original", "both"}:
                item["text"] = row["original"]
            if language in {"english", "both"}:
                item["english"] = row["english"]
            output.append(item)
        payload = {"title": redact(recording.title), "workshop": redact(recording.workshop), "date": recording.date, "segments": output}
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json", safe_name + ".json"
    if format in {"srt", "vtt"}:
        blocks = ["WEBVTT\n"] if format == "vtt" else []
        separator = "," if format == "srt" else "."
        for index, row in enumerate(rows, start=1):
            body = chosen_text(row)
            if include_speakers:
                body = f"{row['speaker']}: {body}"
            # Escape subtitle control markup while preserving the transcript content.
            body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            blocks.append(f"{index}\n{timestamp(row['start'], separator)} --> {timestamp(row['end'], separator)}\n{body}\n")
        return "\n".join(blocks).encode("utf-8"), "text/vtt" if format == "vtt" else "application/x-subrip", safe_name + "." + format
    if format == "txt":
        blocks = [redact(recording.title), " · ".join(filter(None, [redact(recording.workshop), recording.date]))]
        for row in rows:
            blocks.append((heading(row) + "\n" if heading(row) else "") + chosen_text(row))
        return "\n\n".join(blocks).encode("utf-8"), "text/plain", safe_name + ".txt"
    document = Document()
    document.sections[0].left_margin = Inches(0.7)
    document.sections[0].right_margin = Inches(0.7)
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)
    document.add_heading(redact(recording.title), 0)
    metadata = " · ".join(filter(None, [redact(recording.workshop), recording.date]))
    if metadata:
        document.add_paragraph(metadata)
    if language == "both":
        table = document.add_table(rows=1, cols=2)
        table.style = "Light Shading Accent 1"
        table.rows[0].cells[0].text = "Original language"
        table.rows[0].cells[1].text = "English"
        for row in rows:
            cells = table.add_row().cells
            for cell, field in zip(cells, ("original", "english")):
                paragraph = cell.paragraphs[0]
                if heading(row):
                    paragraph.add_run(heading(row) + "\n").bold = True
                paragraph.add_run(row[field])
    else:
        for row in rows:
            paragraph = document.add_paragraph()
            if heading(row):
                paragraph.add_run(heading(row) + "\n").bold = True
            paragraph.add_run(chosen_text(row))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", safe_name + ".docx"
