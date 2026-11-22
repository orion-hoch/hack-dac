import sys, os, random, collections
random.seed(42)
man, out = sys.argv[1], sys.argv[2]
rows=[]
for line in open(man, encoding='utf-8'):
    if line.startswith('#'): continue
    p,b,m,u=line.rstrip('\n').split('\t'); rows.append((p,int(b),m,u))
def ext(p):
    e=os.path.splitext(p)[1].lower()
    if e[1:].isdigit():  # creo versioned: foo.prt.1 -> .prt.N
        e=os.path.splitext(p[:-len(e)])[1].lower()+'.N'
    return e or '(none)'
def src(p):
    a=p.split('/'); return '/'.join(a[:3])
by=collections.defaultdict(list)
for r in rows: by[ext(r[0])].append(r)
quota={}
for e,lst in by.items():
    n=len(lst)
    quota[e]= 8 if n>5000 else 6 if n>1000 else 4 if n>200 else 3 if n>=20 else 1
LIM={'.zip':15e6,'.tar':40e6,'.7z':20e6,'.rar':20e6}
picked=[]; total=0
for e,lst in sorted(by.items(), key=lambda kv:-len(kv[1])):
    lim=LIM.get(e,3e6)
    cand=[r for r in lst if 0<r[1]<=lim] or sorted([r for r in lst if r[1]>0], key=lambda r:r[1])[:1]
    random.shuffle(cand)
    seen=set(); chosen=[]
    for r in cand:              # one per source first for diversity
        if src(r[0]) in seen: continue
        seen.add(src(r[0])); chosen.append(r)
        if len(chosen)>=quota[e]: break
    for r in cand:
        if len(chosen)>=quota[e]: break
        if r not in chosen: chosen.append(r)
    picked+=chosen
# a couple of empty files and one duplicate group
picked+=[r for r in rows if r[1]==0][:2]
md5=collections.defaultdict(list)
for r in rows:
    if r[2] and r[1]<500000: md5[r[2]].append(r)
for g in md5.values():
    if len(g)>=3 and len({src(r[0]) for r in g})>=2: picked+=g[:3]; break
seenp=set(); final=[]
for r in picked:
    if r[0] in seenp: continue
    seenp.add(r[0]); final.append(r)
with open(out,'w',encoding='utf-8') as f:
    for r in final: f.write('\t'.join(map(str,r))+'\n')
print(len(final),"files", round(sum(r[1] for r in final)/1e6,1),"MB", len({ext(r[0]) for r in final}),"types", len({src(r[0]) for r in final}),"sources")
