"""Reviewed transcript exports; redaction is applied to every exported text field."""
import io
import json
import re
from html import escape
from pathlib import Path
from functools import lru_cache

from docx import Document
from docx.shared import Inches, Pt


def timestamp(seconds, separator="."):
    milliseconds = max(0, round(float(seconds or 0) * 1000))
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{millis:03}"


def export_transcript(recording, segments, speakers, format="docx", language="both", include_speakers=True, include_timestamps=True, anonymize=False, layout="columns", font_size=11):
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
    title = redact(recording.title)
    metadata = " · ".join(filter(None, [redact(recording.workshop), recording.date]))
    if format in {"html", "pdf"}:
        data = document_preview(title, metadata, rows, language, heading, layout, font_size) if format == "html" else pdf_document(title, metadata, rows, language, heading, layout, font_size)
        return data, "text/html" if format == "html" else "application/pdf", safe_name + "." + format
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
    normal.font.size = Pt(font_size)
    document.add_heading(redact(recording.title), 0)
    metadata = " · ".join(filter(None, [redact(recording.workshop), recording.date]))
    if metadata:
        document.add_paragraph(metadata)
    if language == "both" and layout == "columns":
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


def document_preview(title, metadata, rows, language, heading, layout, font_size):
    """A lightweight, escaped reading view of all export rows, without editor controls."""
    blocks = []
    both = language == "both"
    for row in rows:
        label = f'<h2>{escape(heading(row))}</h2>' if heading(row) else ""
        fields = ("original", "english") if both else (language,)
        paragraphs = []
        for field in fields:
            name = "Original language" if field == "original" else "English"
            paragraphs.append(f'<div>{f"<h3>{name}</h3>" if both else ""}<p>{escape(row[field])}</p></div>')
        blocks.append(f'<article>{label}<div class="texts {layout if both else "single"}">{"".join(paragraphs)}</div></article>')
    html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#e8ece7;color:#172b24;font-family:Arial,sans-serif;font-size:{font_size}pt;line-height:1.65}}main{{max-width:1000px;margin:20px auto;padding:38px;background:white;border:1px solid #a9b7ad}}h1{{font-size:24pt;line-height:1.3;margin:0 0 10px;overflow-wrap:anywhere}}.metadata{{color:#485c50;margin-bottom:26px}}article{{padding:18px 0;border-top:1px solid #a9b7ad;content-visibility:auto;contain-intrinsic-size:auto 160px;break-inside:avoid}}h2{{font-size:10pt;color:#345346;margin:0 0 10px}}h3{{font-size:9pt;color:#485c50;margin:0 0 6px}}p{{white-space:pre-wrap;overflow-wrap:anywhere;margin:0}}.texts{{display:grid;gap:18px}}.columns{{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}.columns>div+div{{border-left:1px solid #b5c1b7;padding-left:18px}}@media(max-width:550px){{main{{padding:20px;margin:0}}.columns{{grid-template-columns:1fr}}.columns>div+div{{border-left:0;padding:12px 0 0;border-top:1px solid #b5c1b7}}}}@media print{{body{{background:white}}main{{border:0;margin:0;padding:0}}article{{content-visibility:visible}}}}</style></head>
<body><main><h1>{escape(title)}</h1><p class="metadata">{escape(metadata)}</p>{"".join(blocks)}</main></body></html>'''
    return html.encode("utf-8")


@lru_cache(maxsize=1)
def pdf_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for regular, bold in [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ]:
        if Path(regular).is_file() and Path(bold).is_file():
            pdfmetrics.registerFont(TTFont("Transcript", regular))
            pdfmetrics.registerFont(TTFont("TranscriptBold", bold))
            pdfmetrics.registerFontFamily("Transcript", normal="Transcript", bold="TranscriptBold")
            return
    raise RuntimeError("PDF export needs DejaVu Sans or Arial fonts. Rebuild the app image to install them.")


def pdf_document(title, metadata, rows, language, heading, layout, font_size):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle
    pdf_fonts()
    buffer = io.BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=40, leftMargin=40,
                                topMargin=42, bottomMargin=42, title=title, author="Workshop Transcriber")
    body = ParagraphStyle("Body", fontName="Transcript", fontSize=font_size, leading=font_size * 1.5,
                          textColor=colors.HexColor("#172b24"), spaceAfter=9, splitLongWords=True)
    label = ParagraphStyle("Label", parent=body, fontName="TranscriptBold", fontSize=9, leading=13,
                           textColor=colors.HexColor("#345346"), spaceBefore=9, spaceAfter=5, keepWithNext=True)
    heading_style = ParagraphStyle("Title", parent=body, fontName="TranscriptBold", fontSize=21, leading=28, spaceAfter=12)
    def paragraph(value, style=body):
        # Paragraph markup is escaped; arbitrary transcript text cannot become PDF tags.
        return Paragraph(escape(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or ""))).replace("\n", "<br/>"), style)
    story = [paragraph(title, heading_style)]
    if metadata:
        story += [paragraph(metadata), Spacer(1, 12)]
    if language == "both" and layout == "columns":
        data = [[paragraph("Original language", label), paragraph("English", label)]]
        for row in rows:
            cells = []
            for field in ("original", "english"):
                cells.append(([paragraph(heading(row), label)] if heading(row) else []) + [paragraph(row[field])])
            data.append(cells)
        table = LongTable(data, colWidths=[document.width / 2] * 2, repeatRows=1, splitByRow=1, splitInRow=1)
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eee7")),
            ("LINEBELOW", (0, 0), (-1, -1), .5, colors.HexColor("#a9b7ad")),
            ("LINEAFTER", (0, 0), (0, -1), .5, colors.HexColor("#a9b7ad")),
            ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(table)
    else:
        for row in rows:
            if heading(row):
                story.append(paragraph(heading(row), label))
            for field in (("original", "english") if language == "both" else (language,)):
                if language == "both":
                    story.append(paragraph("Original language" if field == "original" else "English", label))
                story.append(paragraph(row[field]))
            story.append(Spacer(1, 7))
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#a9b7ad"))
        canvas.line(40, 32, A4[0] - 40, 32)
        canvas.setFont("Transcript", 8)
        canvas.setFillColor(colors.HexColor("#485c50"))
        canvas.drawString(40, 20, "Workshop transcript")
        canvas.drawRightString(A4[0] - 40, 20, str(doc.page))
        canvas.restoreState()
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
