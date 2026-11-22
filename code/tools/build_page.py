# Usage: python -I build_page.py <tools_dir> <findings.jsonl> <visuals.json> <out.html>
# Note: tools/curated.py (hand-labelled examples with real identifiers) is local-only and gitignored.
import sys, json, html, re, collections
sys.path.insert(0, sys.argv[1]); from curated import EX
findings=[json.loads(l) for l in open(sys.argv[2], encoding='utf-8')]
visuals=sorted(json.load(open(sys.argv[3])), key=lambda v: 0 if v["kind"].startswith("PDF") else 1 if "Embedded" in v["kind"] else 2)
E=html.escape

def mark(raw, ids, keeps):
    spans=[]
    for s,t,_ in ids:
        for m in re.finditer(re.escape(s), raw): spans.append((m.start(),m.end(),'id',t))
    for s in keeps:
        for m in re.finditer(re.escape(s), raw): spans.append((m.start(),m.end(),'keep',''))
    spans.sort(key=lambda x:(x[0], 0 if x[2]=='id' else 1, -(x[1]-x[0])))
    out=[]; pos=0
    for a,b,k,t in spans:
        if a<pos: continue
        out.append(E(raw[pos:a])); out.append(f'<mark class="{k}" title="{E(t or "keep: engineering / format data")}">{E(raw[a:b])}</mark>'); pos=b
    out.append(E(raw[pos:])); return ''.join(out)

cards=[]; cats=[]; counts=collections.Counter()
for cat,fmt,f,field,raw,ids,keeps,verdict,note in EX:
    real=[i for i in ids if not i[2].startswith('(gray')]
    v=verdict or ('ID' if real else 'KEEP')
    counts[v]+=1
    if cat not in cats: cats.append(cat)
    reps=''.join(f'<li><span class="o">{E(s)}</span><span class="arrow">→</span><span class="p">{E(p)}</span><span class="t">{E(t)}</span></li>' for s,t,p in ids)
    cards.append(f'''<article class="card" data-cat="{E(cat)}" data-v="{v}">
<header><span class="v v-{v}">{ {"ID":"Identifier → replace","KEEP":"Keep as is","GRAY":"Gray zone"}[v] }</span><span class="fmt">{E(fmt)}</span><span class="field">{E(field)}</span></header>
<pre class="raw">{mark(raw,ids,keeps)}</pre>
{f'<ul class="reps">{reps}</ul>' if ids else ''}
{f'<p class="note">{E(note)}</p>' if note else ''}
<p class="file">{E(f)}</p></article>''')

# ---- raw auto-labelled table
USERPATH=re.compile(r'(?i)([A-Z]:\\(?:Users|Documents and Settings)\\(?!All Users|Public|Default)[^\\\s"]+|/(?:home|Users)/[^/\s"]+|\\\\[\w.-]+\\users\\[^\\]+|U:\\\w+)')
CONTACT=re.compile(r'[\w.+-]+@[\w-]+\.[a-z]{2,}|QQ|微信|\+?\d[\d\s-]{9,}\d')
PERSONF=re.compile(r'(?i)^(author|last_saved_by|company|manager|dc:creator|cp:lastModifiedBy|SW-Author|SW-Last Saved By|SW-Dernier.*|Author|Designer|CreatedBy|LastModifiedBy|WDC_DRN|WDC_APPR\d|%%Creator|%%For|creator)$')
GENERIC={'administrator','operator','user','admin','unknown','author','','temp','owner'}
KEEPF=re.compile(r'(?i)(unit|schema|acadver|codepage|measurement|massprop|weight|material|tol|scale|sheet|configuration|format|version|producer|creatortool|boundingbox|pages|languagelevel|image count|format/size|date|count$)')
SOFT=re.compile(r'(?i)solidworks|inventor|solid ?edge|adobe|acrobat|ezdxf|cairo|inkscape|freecad|open cascade|netfabb|autodesk|quartz|acis|asm \d|ptc|creo|microsoft|ghostscript|LEAD Technologies|greenshot')
def label(r):
    v=r['value']; f=r['field']
    if r['layer']=='defect': return 'GRAY','source defect'
    if USERPATH.search(v): return 'ID','username in path'
    if CONTACT.search(v) and not v.startswith(('<','http')): return 'ID','contact detail'
    if PERSONF.match(f) or (r['layer'].startswith('ole-propset') and f in ('4','16','41','15','10')):
        clean=v.split('\x1f')[0].strip().lower()
        if clean in GENERIC: return 'GRAY','generic account'
        if SOFT.search(v): return 'KEEP','software name'
        return 'ID','person / company field'
    if KEEPF.search(f) or SOFT.search(v): return 'KEEP','engineering / format / software'
    return 'REVIEW',''
SKIP_LAYERS={'path','unv-header','unv-text','wrl-text','ole-structure','sw-container','archive','image-info','pdf-images'}
seen=set(); rows=[]
for r in findings:
    lay=r['layer'].split(':')[0]
    if lay in SKIP_LAYERS: continue
    if lay=='creo-header' and r['value'].strip(' \\').startswith('#-') and len(r['value'].strip(' \\'))<12: continue
    if r['field'].startswith('SW_Units') or r['field'].isdigit() and r['value'].count('\x1f')>1: continue
    k=(lay,r['field'],r['value'][:70])
    if k in seen: continue
    seen.add(k); lab,why=label(r); rows.append((lab,why,r))
order={'ID':0,'GRAY':1,'REVIEW':2,'KEEP':3}
rows.sort(key=lambda x:order[x[0]])
rc=collections.Counter(x[0] for x in rows)
trs=''.join(f'<tr data-v="{lab}"><td><span class="v v-{lab}">{lab}</span></td><td>{E(why)}</td><td class="mono">{E(r["layer"])}</td><td class="mono">{E(r["field"][:40])}</td><td class="mono val">{E(r["value"].replace(chr(0x1f)," ¦ ")[:220])}</td><td class="mono file">{E(("…"+r["file"][-70:]) if len(r["file"])>72 else r["file"])}</td></tr>' for lab,why,r in rows)

gal=''.join(f'''<figure><img loading="lazy" src="{v['img']}" alt="">{f'<img loading="lazy" class="crop" src="{v["crop"]}" alt="title block crop">' if v.get('crop') else ''}<figcaption><b>{E(v['kind'])}</b><br>{E(('…'+v['file'][-90:]) if len(v['file'])>92 else v['file'])}</figcaption></figure>''' for v in visuals)

chips=''.join(f'<button class="chip" data-cat="{E(c)}">{E(c)} <span>{sum(1 for e in EX if e[0]==c)}</span></button>' for c in cats)

TESTS='''
<li><b>Identifier dictionary + leak scan (recall).</b> Run the extractor from this page on the <i>input</i> to build a dictionary of every identity string (path owners and hosts, metadata people and companies, user paths, emails, phone/QQ numbers, title-block names). Add variants: case-folded, accent-stripped, UTF-16LE, URL-encoded, initials, and username forms. Then scan the <i>output</i> at every layer: names, decompressed SW sections, OLE property sets, PDF Info/XMP/text/attachments, recursively unpacked archives, and OCR of images and previews. Your local score is the share of dictionary hits that are gone.</li>
<li><b>Canary injection, per format and per layer.</b> Copy about 50 files and plant fake identities everywhere: PDF Author, XMP, SW custom prop, Inventor Designer, STEP FILE_NAME, IGES global, FCStd CreatedBy, a zip member name, a PNG tEXt chunk, rendered text in a drawing. Run the pipeline and grep for the canaries. This gives you a recall number for each layer.</li>
<li><b>Consistency check.</b> Log every (original, pseudonym, file, layer) triple privately. Assert that one original always maps to one pseudonym and that no two originals share one. Linked forms must map in step (full name ↔ username). Pseudonyms in identity fields must be non-empty and look like the right type (a name looks like a name).</li>
<li><b>Duplicate invariance.</b> The manifest has 35,264 groups of identical files. After processing, each group should still be byte-identical (or at least identical apart from its path). It's a free, corpus-wide consistency test.</li>
<li><b>Open-ability check for every file.</b> PDF: pymupdf or <code>qpdf --check</code>. ZIP/3MF/FCStd: <code>testzip</code> plus a parse of the XML members. OLE (Inventor, old SW, Solid Edge): olefile opens it, with the same stream list and sizes apart from the property streams. SW 2015+: every section still inflates and the section count matches. DXF: <code>ezdxf.audit</code>. Images: decode, same dimensions. The baseline of 292 says preservation is a large share of the checks, so a file that won't open costs you twice.</li>
<li><b>Geometry diff.</b> STEP/IGES/BREP: load with OCP or cadquery and compare volume, bounding box, and face/edge counts before vs after. STL/OBJ/3MF: trimesh vertex hash and volume. Text formats: diff input vs output and assert that every changed span overlaps a known identifier. Anything else changed is collateral damage.</li>
<li><b>Reference graph.</b> Before processing, extract the links: SW model paths (Config-Model-View-Sheet-List, SW-Material@…), Inventor RSeDb refs, Creo CMNM, OBJ mtllib, DXF xrefs, PDF attachments, IGES file names. Push them through your rename table and assert that every link resolves to a file that exists in the output.</li>
<li><b>Counts and structure.</b> The output should have about the same number of files as the input (188,722) minus anything documented, stay under 400k objects with no file over 2 GB, and have no raw original in any archive. Every omission or defect must be listed in README.txt.</li>
<li><b>Calibrate against the real scorer once.</b> Run your local suite on an unchanged copy (the real score is 292) and on your first submission. Line up your categories with the pass counts per category the scorer returns. After that, local deltas predict real deltas, and you save your 20 attempts.</li>'''

IDEAS='''
<li><b>Prefer length-preserving replacement in binary formats</b> (IGES Hollerith counts, STL's 80-byte header, OLE property sizes, Creo binary blocks). Pad or truncate the pseudonym to the original length unless you fully re-serialise the format.</li>
<li><b>Resolve identities before replacing anything.</b> Cluster per repo: owner handle, repo name, the People and Company fields, path usernames and initials. One identity gets one pseudonym family (full name, username, initials, email, domain).</li>
<li><b>Use context-scoped matching, not global substring replacement.</b> 'jo' or 'Atom' as a username must not change 'joint' or 'Atomic'. Replace inside a known slot (path segment, property value, header field) or on word boundaries.</li>
<li><b>Keep an allowlist</b> for software vendors (SolidWorks, PTC, Siemens, Spatial, netfabb), standards (ASME, ISO, DIN, GB, ГОСТ), materials and units. Most false positives come from these.</li>
<li><b>Handle images and logos explicitly:</b> OCR, then inpaint or overlay neutral text in title blocks. Logo files (svg, eps, png named *logo*) get a neutral replacement graphic. Previews inside native files need regenerating or a neutral image.</li>
<li><b>Write rename and reference updates as one transaction:</b> a rename table first, then rewrite every reference string from that table in a single pass.</li>'''

page=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>De-ID Sample Explorer</title>
<style>
:root{{--bg:#f7f6f3;--panel:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#e3e1dc;--id:#c2410c;--idbg:#ffedd5;--keep:#15803d;--keepbg:#dcfce7;--gray:#7c6f00;--graybg:#fef9c3;--rev:#475569;--revbg:#e2e8f0;--accent:#1d4ed8}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#141416;--panel:#1d1d20;--ink:#ececef;--mute:#9a9aa2;--line:#2e2e33;--id:#fb923c;--idbg:#431407;--keep:#4ade80;--keepbg:#052e16;--gray:#facc15;--graybg:#3a3000;--rev:#cbd5e1;--revbg:#1e293b;--accent:#93c5fd}}}}
:root[data-theme="dark"]{{--bg:#141416;--panel:#1d1d20;--ink:#ececef;--mute:#9a9aa2;--line:#2e2e33;--id:#fb923c;--idbg:#431407;--keep:#4ade80;--keepbg:#052e16;--gray:#facc15;--graybg:#3a3000;--rev:#cbd5e1;--revbg:#1e293b;--accent:#93c5fd}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1240px;margin:0 auto;padding:28px 16px 80px}}
h1{{font-size:28px;margin:0 0 4px;letter-spacing:-.01em}} h2{{font-size:20px;margin:44px 0 6px}} .sub{{color:var(--mute);margin:0 0 18px;max-width:860px}}
.stats{{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}} .stat{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:120px}} .stat b{{display:block;font-size:22px;font-variant-numeric:tabular-nums}} .stat span{{color:var(--mute);font-size:13px}}
.legend{{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--mute);margin:8px 0 14px}}
mark{{border-radius:3px;padding:0 2px;color:inherit}} mark.id{{background:var(--idbg);color:var(--id);font-weight:600;box-shadow:inset 0 -2px 0 var(--id)}} mark.keep{{background:var(--keepbg);color:var(--keep)}}
.bar{{background:var(--bg);padding:10px 0;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:6px;align-items:center}}
.chip{{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:999px;padding:5px 11px;font-size:13px;cursor:pointer}} .chip span{{color:var(--mute);font-variant-numeric:tabular-nums}} .chip.on{{border-color:var(--accent);color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}}
input[type=search]{{flex:1;min-width:180px;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:8px;padding:6px 10px;font-size:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px;margin-top:14px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;display:flex;flex-direction:column;gap:8px;min-width:0}}
.card header{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px}}
.v{{font-size:11px;font-weight:700;letter-spacing:.02em;border-radius:5px;padding:2px 7px;white-space:nowrap}} .v-ID{{background:var(--idbg);color:var(--id)}} .v-KEEP{{background:var(--keepbg);color:var(--keep)}} .v-GRAY{{background:var(--graybg);color:var(--gray)}} .v-REVIEW{{background:var(--revbg);color:var(--rev)}}
.fmt{{font-weight:600}} .field{{color:var(--mute);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
pre.raw{{margin:0;white-space:pre-wrap;word-break:break-word;font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bg);border-radius:8px;padding:8px 10px}}
ul.reps{{margin:0;padding:0;list-style:none;font-size:12.5px;display:flex;flex-direction:column;gap:2px}} .reps li{{display:flex;gap:6px;flex-wrap:wrap;align-items:baseline}} .reps .o{{color:var(--id);font-family:ui-monospace,Menlo,monospace}} .reps .p{{font-family:ui-monospace,Menlo,monospace}} .reps .arrow,.reps .t{{color:var(--mute)}} .reps .t{{font-size:11px}}
.note{{margin:0;font-size:13px}} .file{{margin:0;font-size:11px;color:var(--mute);font-family:ui-monospace,Menlo,monospace;word-break:break-all}}
.gallery{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:12px}} figure{{margin:0;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:8px;display:flex;flex-direction:column;gap:6px}} figure img{{width:100%;border-radius:6px;background:#fff;outline:1px solid rgba(0,0,0,.08);outline-offset:-1px}} figure img.crop{{outline:2px solid var(--id)}} figcaption{{font-size:11.5px;color:var(--mute);word-break:break-all}}
.tablewrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel);margin-top:10px;max-height:640px}} table{{border-collapse:collapse;width:100%;font-size:12px}} th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}} th{{position:sticky;top:0;background:var(--panel)}} .mono{{font-family:ui-monospace,Menlo,monospace}} td.val{{max-width:460px;word-break:break-word}} td.file{{color:var(--mute);max-width:280px;word-break:break-all}}
ol.tests li,ul.ideas li{{margin:0 0 10px;max-width:920px}} code{{font-size:12.5px;background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:0 4px}}
.callout{{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:10px 14px;max-width:920px;font-size:14px}}
@media (max-width:640px){{.grid{{grid-template-columns:1fr}} h1{{font-size:23px}}}}
</style></head><body><div class="wrap">
<h1>Machina CAD De-ID: what to replace and what to keep</h1>
<p class="sub">Real examples from a stratified sample of the challenge input: 275 files covering 69 file types from 129 different repos and sites, picked from the 188,722-file manifest. Orange spans are identities to pseudonymise. Green spans are engineering or format data that must survive byte-for-byte. Pseudonyms shown are illustrative. This page contains raw input, so keep it private.</p>
<div class="stats">
<div class="stat"><b>{len(EX)}</b><span>labelled examples</span></div>
<div class="stat"><b>{counts['ID']}</b><span>contain identifiers</span></div>
<div class="stat"><b>{counts['KEEP']}</b><span>keep as is</span></div>
<div class="stat"><b>{counts['GRAY']}</b><span>gray zone / policy</span></div>
<div class="stat"><b>{len(rows)}</b><span>raw extracted fields</span></div>
<div class="stat"><b>{len(visuals)}</b><span>rendered visuals</span></div>
</div>
<div class="callout">Where the corpus comes from (by file count): GitHub repos 94.6%, prior search collections 2.4%, organisation catalogues 1.9%, web hosts 0.8%, GitLab 0.4%. Top types: .step 57.7k, .sat 26.4k, .sldprt 18.8k, Creo .prt/.asm/.drw.N 14.3k+, .stl 12.8k, .pdf 10.3k, .svg 7.9k, .ipt 5.6k, .fcstd 4.7k.</div>

<h2>Labelled examples</h2>
<div class="bar"><button class="chip on" data-cat="*">All <span>{len(EX)}</span></button>{chips}
<button class="chip vf" data-v="ID">Only IDs</button><button class="chip vf" data-v="KEEP">Only keep</button><button class="chip vf" data-v="GRAY">Only gray</button>
<input type="search" id="q" placeholder="Filter text…"></div>
<div class="legend"><span><mark class="id">identifier</mark> replace with a consistent pseudonym</span><span><mark class="keep">engineering / format</mark> must not change</span></div>
<div class="grid" id="cards">{''.join(cards)}</div>

<h2>What the drawings look like</h2>
<p class="sub">Page 1 of every sampled PDF, with the bottom-right title block (outlined) cropped at higher resolution, plus raster drawings, logos and previews embedded inside native files. Names in vector title blocks have to be edited in the content stream. Names and logos in raster images need OCR plus inpainting.</p>
<div class="gallery">{gal}</div>

<h2>Every extracted field, auto-labelled</h2>
<p class="sub">{len(rows)} unique fields the extractor pulled from the 275 files (deduplicated, with noise layers removed). Labels come from simple rules: {rc['ID']} ID, {rc['GRAY']} gray, {rc['KEEP']} keep, {rc['REVIEW']} need review. Treat them as a first pass, not ground truth.</p>
<div class="bar"><button class="chip tf on" data-v="*">All</button><button class="chip tf" data-v="ID">ID {rc['ID']}</button><button class="chip tf" data-v="GRAY">Gray {rc['GRAY']}</button><button class="chip tf" data-v="REVIEW">Review {rc['REVIEW']}</button><button class="chip tf" data-v="KEEP">Keep {rc['KEEP']}</button><input type="search" id="tq" placeholder="Filter rows…"></div>
<div class="tablewrap"><table><thead><tr><th>Label</th><th>Rule</th><th>Layer</th><th>Field</th><th>Value</th><th>File</th></tr></thead><tbody id="tb">{trs}</tbody></table></div>

<h2>Testing locally without spending a submission</h2>
<p class="sub">The scorer is hidden, but its rules are in the brief: remove identities at every layer, keep pseudonyms consistent, keep files openable, and keep geometry and links intact. Each of those can be checked locally.</p>
<ol class="tests">{TESTS}</ol>

<h2>Pipeline ideas from what the sample shows</h2>
<ul class="ideas">{IDEAS}</ul>
</div>
<script>
const cards=[...document.querySelectorAll('.card')];let cat='*',vf=null,q='';
function apply(){{cards.forEach(c=>{{const ok=(cat==='*'||c.dataset.cat===cat)&&(!vf||c.dataset.v===vf)&&(!q||c.textContent.toLowerCase().includes(q));c.style.display=ok?'':'none'}})}}
document.querySelectorAll('.chip[data-cat]').forEach(b=>b.onclick=()=>{{document.querySelectorAll('.chip[data-cat]').forEach(x=>x.classList.remove('on'));b.classList.add('on');cat=b.dataset.cat;apply()}});
document.querySelectorAll('.chip.vf').forEach(b=>b.onclick=()=>{{const on=b.classList.contains('on');document.querySelectorAll('.chip.vf').forEach(x=>x.classList.remove('on'));vf=on?null:b.dataset.v;if(!on)b.classList.add('on');apply()}});
document.getElementById('q').oninput=e=>{{q=e.target.value.toLowerCase();apply()}};
const trs=[...document.querySelectorAll('#tb tr')];let tv='*',tq='';
function tapply(){{trs.forEach(r=>{{r.style.display=((tv==='*'||r.dataset.v===tv)&&(!tq||r.textContent.toLowerCase().includes(tq)))?'':'none'}})}}
document.querySelectorAll('.chip.tf').forEach(b=>b.onclick=()=>{{document.querySelectorAll('.chip.tf').forEach(x=>x.classList.remove('on'));b.classList.add('on');tv=b.dataset.v;tapply()}});
document.getElementById('tq').oninput=e=>{{tq=e.target.value.toLowerCase();tapply()}};
</script></body></html>'''
open(sys.argv[4],'w',encoding='utf-8').write(page)
print('examples',len(EX),dict(counts),'rows',len(rows),dict(rc),'bytes',len(page))
