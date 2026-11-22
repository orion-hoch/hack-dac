# Usage: python -I extract.py <input_root> <out.jsonl>
import sys, os, re, json, zlib, gzip, zipfile, io, subprocess, struct
import olefile, pymupdf
from PIL import Image, ExifTags
root, outp = sys.argv[1], sys.argv[2]
out = open(outp, 'w', encoding='utf-8')
def emit(f, layer, field, value):
    v = value if isinstance(value, str) else str(value)
    v = v.strip().replace('\x00', '')
    if not v or len(v) > 400: v = v[:400]
    if v: out.write(json.dumps({'file': f, 'layer': layer, 'field': field, 'value': v}) + '\n')

INTEREST = re.compile(r'(?i)([A-Z]:\\(?:Users|Documents and Settings)\\[^\\\s"]+|/(?:home|Users)/[^/\s"]+|[\w.+-]+@[\w-]+\.[\w.]+|https?://[^\s"<>]+|www\.[\w.-]+\.\w+|copyright|\(c\)\s*\d{4}|\bauthor\b|\bcompany\b|\bdesigner\b|\bdrawn\s*by\b|\bchecked\s*by\b|\bengineer\b|\bapproved\b|lastModifiedBy|\bcreator\b|<cp:|<dc:)')
def strings(b, n=5):
    for m in re.finditer(rb'[\x20-\x7e]{%d,}' % n, b): yield m.group().decode('latin1')
    for m in re.finditer(rb'(?:[\x20-\x7e]\x00){%d,}' % n, b): yield m.group().decode('utf-16le', 'ignore')
def interesting_strings(f, b, layer, limit=25):
    seen=set(); k=0
    for s in strings(b):
        if INTEREST.search(s) and s not in seen:
            seen.add(s); emit(f, layer, 'string', s); k+=1
            if k>=limit: break

def sw_streams(b):
    # SolidWorks 2015+ container: brute-force raw-deflate payloads after section markers
    res=[]; pos=0
    while len(res)<200:
        i=b.find(b'\x14\x00\x06\x00', pos)
        if i<0: break
        pos=i+4
        for off in range(18, 40):
            try:
                nl=struct.unpack_from('<I', b, i+off)[0]
                if not 0<nl<300: continue
                st=i+off+4+nl
                d=zlib.decompressobj(-15); data=d.decompress(b[st:st+8_000_000])
                if len(data)>32: res.append(data); break
            except Exception: pass
    return res

def handle_bytes(f, b, ext, layer='content', depth=0):
    head=b[:16]
    if not b: emit(f,'defect','empty','0-byte file'); return
    if head.startswith(b'%PDF'): return pdf(f,b)
    if head.startswith(b'\xd0\xcf\x11\xe0'): return ole(f,b)
    if head.startswith(b'PK\x03\x04') or head.startswith(b'PK\x05\x06'): return zipf(f,b,depth)
    if head.startswith(b'\x1f\x8b') and depth<2:
        try: inner=gzip.decompress(b); emit(f,'archive','gzip-inner-size',len(inner)); return handle_bytes(f, inner, ext, 'gzip-inner', depth+1)
        except Exception as e: emit(f,'defect','gzip',repr(e)[:120]); return
    if head[:4] in (b'\x89PNG',) or head[:3]==b'\xff\xd8\xff' or head[:4] in (b'II*\x00', b'MM\x00*'): return image(f,b)
    txt = b[:200000].decode('latin1')
    if 'ISO-10303-21' in txt[:200]:
        h=txt.split('ENDSEC;')[0]
        for fld in ('FILE_DESCRIPTION','FILE_NAME','FILE_SCHEMA'):
            m=re.search(fld+r'\s*\((.*?)\);', h, re.S)
            if m: emit(f,'step-header',fld,' '.join(m.group(1).split()))
        for m in list(re.finditer(r"PRODUCT\s*\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'", txt))[:4]:
            emit(f,'step-entity','PRODUCT', f"id='{m.group(1)}' name='{m.group(2)}' desc='{m.group(3)}'")
        for m in list(re.finditer(r"(PERSON|ORGANIZATION|PERSON_AND_ORGANIZATION|APPROVAL_PERSON_ORGANIZATION)\s*\(([^;]{0,200})\)", txt))[:4]:
            emit(f,'step-entity',m.group(1),m.group(2))
        for m in list(re.finditer(r"(SI_UNIT|CONVERSION_BASED_UNIT)\s*\(([^;]{0,80})\)", txt))[:2]:
            emit(f,'step-entity','UNIT',m.group(2))
        return
    lines=txt.splitlines()
    if len(lines)>2 and len(lines[0])>=72 and lines[0][72:73]=='S':
        s=''.join(l[:72] for l in lines if l[72:73]=='S'); g=''.join(l[:72] for l in lines if l[72:73]=='G')
        emit(f,'iges-start','S',' '.join(s.split())); emit(f,'iges-global','G',' '.join(g.split())); return
    if txt.startswith('**') and 'PART' in txt[:80] or txt.startswith('PS\x00'):
        hdr=txt.split('**END_OF_HEADER')[0][:2000]
        for l in hdr.splitlines()[:30]:
            if '=' in l: emit(f,'parasolid-header',l.split('=')[0].strip('*; '),l.split('=',1)[1])
        return
    if txt.startswith('#UGC') or '#UGC:' in txt[:40]:
        for l in lines[:12]: emit(f,'creo-header','line',l[:300])
        interesting_strings(f,b,'creo-strings'); return
    if ext in ('.sat',) or re.match(r'^\d+ \d+ \d+ \d+', txt[:40]):
        for l in lines[:3]: emit(f,'acis-header','line',l[:300])
        return
    if b.lstrip()[:5] in (b'<?xml',b'<svg ',b'<svg\n') or ext=='.svg':
        for pat in (r'sodipodi:docname="([^"]*)"', r'inkscape:export-filename="([^"]*)"', r'<dc:creator>.*?<dc:title>([^<]*)', r'<dc:title>([^<]*)', r'<dc:rights>.*?<dc:title>([^<]*)', r'<dc:publisher>.*?<dc:title>([^<]*)', r'<!--\s*(Generator[^>]*)-->', r'<!-- (Created with[^>]*)-->', r'<title>([^<]*)</title>', r'<desc>([^<]*)</desc>', r'xlink:href="(file:[^"]*|/[^"]*|[A-Z]:\\[^"]*)"', r'<metadata[^>]*>(.{0,300})'):
            for m in list(re.finditer(pat, txt, re.S))[:3]: emit(f,'svg-xml',pat.split('"')[0].split('(')[0][:30] or 'meta',' '.join(m.group(1).split()))
        tx=[ ' '.join(re.sub(r'<[^>]+>','',m.group(1)).split()) for m in re.finditer(r'<text[^>]*>(.*?)</text>', txt, re.S)]
        tx=[t for t in tx if t]
        if tx: emit(f,'svg-visible-text','text', ' | '.join(tx[:25]))
        return
    if lines and lines[0].strip()=='0' and len(lines)>1 and lines[1].strip()=='SECTION':
        for var in ('$ACADVER','$INSUNITS','$MEASUREMENT','$LASTSAVEDBY','$PROJECTNAME','$DWGCODEPAGE','$HYPERLINKBASE'):
            if var in txt:
                i=lines.index(var) if var in lines else -1
                if i>=0: emit(f,'dxf-header',var,lines[i+2].strip())
        full=b.decode('latin1')
        tx=re.findall(r'\n\s*1\s*\n([^\n]{2,200})', full)
        tx=[t.strip() for t in tx if re.search(r'[A-Za-z]{2}', t)]
        if tx: emit(f,'dxf-visible-text','TEXT/MTEXT',' | '.join(dict.fromkeys(tx).keys().__iter__().__class__ and list(dict.fromkeys(tx))[:30]))
        m=re.search(r'DWGPROPS COOKIE(.{0,800})', full, re.S)
        if m: emit(f,'dxf-dwgprops','DWGPROPS',' '.join(m.group(1).split())[:300])
        blocks=list(dict.fromkeys(re.findall(r'\nBLOCK\n.*?\n\s*2\n([^\n]+)', full, re.S)))[:10]
        layers=list(dict.fromkeys(re.findall(r'\nLAYER\n(?:.*?\n){0,12}?\s*2\n([^\n]+)', full)))[:12]
        if layers: emit(f,'dxf-structure','layer names',', '.join(l.strip() for l in layers))
        return
    if txt.lstrip().lower().startswith('solid') and ext=='.stl':
        emit(f,'stl-header','solid name',lines[0][:200]); return
    if ext=='.stl' and len(b)>84:
        emit(f,'stl-header','80-byte header', b[:80].decode('latin1').rstrip('\x00 ')); return
    if head.startswith(b'%!PS') or head.startswith(b'\xc5\xd0\xd3\xc6'):
        for l in [l for l in lines[:60] if l.startswith('%%')][:12]: emit(f,'postscript-dsc',l.split(':')[0],l)
        return
    if ext in ('.obj','.wrl','.unv','.mtl','.ply','.off') or head.startswith(b'#VRML'):
        cm=[l for l in lines[:80] if l.strip().startswith(('#','-1','DEF','o ','g ','mtllib','usemtl'))][:12]
        for l in cm: emit(f,ext.strip('.')+'-text','line',l[:250])
        if ext=='.unv':
            for l in lines[:40]: emit(f,'unv-header','line',l[:120])
        return
    # generic binary / unknown
    if ext in ('.sldprt','.sldasm','.slddrw','.slddrt','.sldlfp','.prtdot','.drwdot'):
        st=sw_streams(b); emit(f,'sw-container','decompressed sections',len(st))
        for d in st:
            interesting_strings(f,d,'sw-stream',limit=15)
            for m in re.finditer(rb'<(cp:lastModifiedBy|dc:creator|dc:title|cp:keywords|dc:subject)>([^<]*)<', d): emit(f,'sw-docProps',m.group(1).decode(),m.group(2).decode('utf-8','ignore'))
            for m in re.finditer(rb'<property[^>]*name="([^"]+)"[^>]*>\s*<vt:\w+>([^<]*)<', d): emit(f,'sw-custom-prop',m.group(1).decode('utf-8','ignore'),m.group(2).decode('utf-8','ignore'))
        interesting_strings(f,b,'raw-strings',limit=10); return
    interesting_strings(f,b,'raw-strings',limit=20)

def pdf(f,b):
    try: doc=pymupdf.open(stream=b, filetype='pdf')
    except Exception as e: emit(f,'defect','pdf-open',repr(e)[:150]); return
    for k,v in (doc.metadata or {}).items():
        if v and k not in ('format','encryption','trapped'): emit(f,'pdf-info',k,v)
    x=doc.get_xml_metadata()
    for m in re.finditer(r'<(dc:creator|xmp:CreatorTool|pdf:Producer|xmpMM:DocumentID|dc:title|pdf:Author|photoshop:AuthorsPosition)[^>]*>(.*?)</\1>', x or '', re.S):
        emit(f,'pdf-xmp',m.group(1),' '.join(re.sub(r'<[^>]+>',' ',m.group(2)).split()))
    for n in doc.embfile_names(): emit(f,'pdf-attachment','name',n)
    try:
        p=doc[0]; t=p.get_text(); emit(f,'pdf-visible-text','page1',' | '.join(l.strip() for l in t.splitlines() if l.strip())[:400])
        emit(f,'pdf-images','page1 image count',len(p.get_images()))
        for a in p.annots() or []: emit(f,'pdf-annot',a.type[1],a.info.get('title','')+' '+a.info.get('content',''))
        for l in p.get_links()[:3]:
            if l.get('uri'): emit(f,'pdf-link','uri',l['uri'])
    except Exception as e: emit(f,'defect','pdf-page',repr(e)[:150])

def ole(f,b):
    try: o=olefile.OleFileIO(io.BytesIO(b))
    except Exception as e: emit(f,'defect','ole',repr(e)[:120]); return
    md=o.get_metadata()
    for a in ('author','last_saved_by','company','title','subject','keywords','comments','manager','category','creating_application','template'):
        v=getattr(md,a,None)
        if v: emit(f,'ole-summaryinfo',a, v.decode('latin1','ignore') if isinstance(v,bytes) else v)
    names=['/'.join(s) for s in o.listdir()]
    emit(f,'ole-structure','streams',', '.join(names[:25]))
    for s in o.listdir():
        if s[-1].startswith('\x05'):
            try:
                props=o.getproperties(s, convert_time=True)
                for k,v in props.items():
                    if isinstance(v,(bytes,str)) and v and len(str(v))<200:
                        vv=v.decode('latin1','ignore') if isinstance(v,bytes) else v
                        if vv.strip('\x00 '): emit(f,'ole-propset:'+s[-1].strip('\x05')[:40],str(k),vv)
            except Exception: pass
    raw=b''.join(o.openstream(s).read()[:400000] for s in o.listdir() if not s[-1].startswith('\x05'))
    interesting_strings(f,raw,'ole-stream-strings',limit=12)

def zipf(f,b,depth):
    try: z=zipfile.ZipFile(io.BytesIO(b))
    except Exception as e: emit(f,'defect','zip',repr(e)[:120]); return
    nm=z.namelist(); emit(f,'archive-members','count',len(nm)); emit(f,'archive-members','names',' | '.join(nm[:30]))
    if z.comment: emit(f,'archive-comment','comment',z.comment.decode('latin1'))
    for n in nm:
        low=n.lower()
        if low.endswith(('document.xml','3dmodel.model','.config','core.xml','app.xml','meta.xml','manifest.xml','.txt','.ini','.json')) and z.getinfo(n).file_size<3_000_000:
            t=z.read(n).decode('utf-8','ignore')
            for m in list(re.finditer(r'<Property name="(CreatedBy|LastModifiedBy|Company|Label|Comment|License|LicenseURL|Id|Uid|FileName|CreationDate)"[^>]*>\s*<\w+ value="([^"]*)"', t))[:12]:
                emit(f,'zip:'+n[-40:],m.group(1),m.group(2))
            for m in list(re.finditer(r'<metadata name="([^"]+)"[^>]*>([^<]*)<', t))[:12]: emit(f,'zip:'+n[-40:],m.group(1),m.group(2))
            for m in list(re.finditer(r'<(dc:creator|cp:lastModifiedBy|meta:initial-creator|dc:title|meta:generator)>([^<]*)<', t))[:6]: emit(f,'zip:'+n[-40:],m.group(1),m.group(2))
            interesting_strings(f,t.encode(),'zip-text:'+n[-40:],limit=6)
        if low.endswith(('.png','.jpg','.jpeg')) and 'thumb' in low: emit(f,'embedded-preview','thumbnail',n)
    if depth<1:
        for n in nm[:6]:
            ext=os.path.splitext(n)[1].lower()
            if ext in ('.step','.stp','.sldprt','.ipt','.pdf','.dxf','.svg','.iges','.igs') and z.getinfo(n).file_size<5_000_000:
                handle_bytes(f+'!/'+n, z.read(n), ext, 'archive-member', depth+1)

def image(f,b):
    try: im=Image.open(io.BytesIO(b))
    except Exception as e: emit(f,'defect','image',repr(e)[:120]); return
    emit(f,'image-info','format/size',f'{im.format} {im.size[0]}x{im.size[1]}')
    for k,v in (im.info or {}).items():
        if isinstance(v,(str,bytes)) and k not in ('icc_profile','exif','transparency') and len(str(v))<400: emit(f,'image-chunk',k,v if isinstance(v,str) else v[:200].decode('latin1'))
    try:
        ex=im.getexif()
        for k,v in ex.items():
            name=ExifTags.TAGS.get(k,str(k))
            if name in ('Artist','Copyright','Software','ImageDescription','Make','Model','XPAuthor','HostComputer','DocumentName'): emit(f,'image-exif',name,v if isinstance(v,str) else str(v)[:120])
    except Exception: pass
    try:
        im2=im.convert('L')
        if max(im2.size)<200: im2=im2.resize((im2.size[0]*3,im2.size[1]*3))
        tmp=os.path.join(os.path.dirname(outp),'_ocr.png'); im2.save(tmp)
        r=subprocess.run(['tesseract',tmp,'-','--psm','11'],capture_output=True,text=True,timeout=40)
        words=[w for w in r.stdout.split() if re.search(r'[A-Za-z]{3}',w)]
        if words: emit(f,'image-ocr','visible text',' '.join(words)[:300])
    except Exception: pass

for dp,dn,fn in os.walk(root):
    for n in fn:
        p=os.path.join(dp,n); rel=os.path.relpath(p,root)
        ext=os.path.splitext(n)[1].lower()
        if ext[1:].isdigit(): ext=os.path.splitext(n[:-len(ext)])[1].lower()
        emit(rel,'path','path',rel)
        try:
            b=open(p,'rb').read()
            handle_bytes(rel,b,ext)
        except Exception as e: emit(rel,'defect','error',repr(e)[:200])
out.close()
