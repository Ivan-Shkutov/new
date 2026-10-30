import io
from pathlib import Path
from fastapi import HTTPException

MAX_BYTES = 10 * 1024 * 1024
MAX_CHARS = 2_000_000


def extract(name, data):
    if len(data) > MAX_BYTES:
        raise HTTPException(413, 'Файл превышает 10 MiB')
    ext = Path(name).suffix.lower()
    try:
        if ext in ('.md', '.txt', '.rst'):
            text = data.decode('utf-8-sig')
        elif ext == '.html':
            from .import_knowledge import Page
            page=Page();page.feed(data.decode('utf-8-sig'));text=page.body()
        elif ext == '.pdf':
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            if len(reader.pages) > 500:
                raise ValueError('too many pages')
            chunks = []
            total = 0
            for page in reader.pages:
                chunk = page.extract_text() or ''
                total += len(chunk)
                if total > MAX_CHARS:
                    raise ValueError('too much text')
                chunks.append(chunk)
            text = '\n'.join(chunks)
        elif ext == '.docx':
            import zipfile
            from docx import Document
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if sum(i.file_size for i in z.infolist()) > 40 * 1024 * 1024:
                    raise ValueError('expanded document too big')
            doc = Document(io.BytesIO(data))
            text = '\n'.join([p.text for p in doc.paragraphs] + [cell.text for t in doc.tables for r in t.rows for cell in r.cells])
        else:
            raise HTTPException(415, 'Поддерживаются UTF-8 MD/TXT/RST/HTML, PDF с текстом и DOCX')
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, 'Не удалось прочитать документ: проверьте формат, размер, шифрование и наличие текста')
    if not text.strip() or len(text) > MAX_CHARS:
        raise HTTPException(422, 'Документ пуст или превышает 2 млн символов. Для скана нужен OCR.')
    return text
