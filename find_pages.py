#!/usr/bin/env python3
"""Extract page numbers for each heading by checking page breaks in the docx XML."""
import sys, re, os
sys.path.insert(0, r'C:\Users\juzhiyuan\AppData\Local\Programs\Python\Python312\Lib\site-packages')
from docx import Document
from lxml import etree

DOC = r'C:\Users\juzhiyuan\.hermes\desktop-attachments\盲派十干直断口诀_五种实战用法_出版小册_修正版.docx'
doc = Document(DOC)

nsmap = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
}

body = doc.element.body

# Find all page breaks and headings with their estimated page numbers
page = 1
# First check for explicit page breaks before each heading
print('=== 逐个标题页码 ===')
print()

for child in body:
    tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
    
    # Check for page break before this element
    # Page breaks can be in run properties
    if tag == 'p':
        # Check for page break in paragraph
        pPr = child.find('.//w:lastRenderedPageBreak', nsmap)
        if pPr is not None:
            page += 1
            
        # Also check explicit page breaks
        brs = child.findall('.//w:br', nsmap)
        for br in brs:
            if br.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type') == 'page':
                page += 1
        
        # Check for section break (new page)
        sectPr = child.find('.//w:sectPr', nsmap)
        if sectPr is not None:
            page += 1
    
    # Check if this paragraph has a heading style
    if tag == 'p':
        pPr = child.find('w:pPr', nsmap)
        if pPr is not None:
            pStyle = pPr.find('w:pStyle', nsmap)
            if pStyle is not None:
                style_val = pStyle.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val', '')
                if style_val.startswith('Heading'):
                    # Get text
                    texts = []
                    for t in child.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'):
                        if t.text:
                            texts.append(t.text)
                    text = ''.join(texts).strip()
                    
                    indent = '  ' if '2' in style_val else '    ' if '3' in style_val else ''
                    print(f'  p{page:>3}  {indent}{text}')

print()
print(f'Estimated total pages: ~{page}')
