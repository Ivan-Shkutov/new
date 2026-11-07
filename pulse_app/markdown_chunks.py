"""Markdown-aware bounded chunks; no model tokenizer or GPU dependency.

Keep paragraphs/headings and normal fenced blocks together. Oversized blocks
are explicitly marked continued. Character bounds are deliberately conservative
for multilingual BGE-M3; the embedding API must still reject truncation.
"""
import re


def chunks(text, size=1800, overlap=180):
    blocks=[];current=[];fence=None;heading=''
    for line in text.replace('\r\n','\n').splitlines(keepends=True):
        marker=re.match(r'^\s*(`{3,}|~{3,})',line)
        title=re.match(r'^(#{1,6})\s+(.+)',line)
        if not fence and title:
            if current:blocks.append((heading,''.join(current)));current=[]
            heading=title[2].strip()
        current.append(line)
        if marker:
            if fence is None:fence=marker[1][0]
            elif marker[1][0]==fence:fence=None
        if not fence and not line.strip() and current:
            blocks.append((heading,''.join(current)));current=[]
    if current:blocks.append((heading,''.join(current)))
    result=[];buffer='';active=''
    def publish():
        nonlocal buffer
        if buffer.strip():result.append({'text':buffer.strip(),'heading':active})
        buffer=''
    for title,block in blocks:
        if title!=active or len(buffer)+len(block)>size:publish()
        active=title
        if len(block)>size:
            publish()
            # Keep the raw source, overlapping only within an oversized block.
            for offset in range(0,len(block),size-overlap):
                part=block[offset:offset+size]
                if part.strip():result.append({'text':part.strip(),'heading':title,'continued':offset>0})
        else:buffer+=block
    publish()
    return result
