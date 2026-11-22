import sys, collections, os
rows=[]
for line in open(sys.argv[1], encoding='utf-8'):
    if line.startswith('#'): continue
    p,b,m,u=line.rstrip('\n').split('\t')
    rows.append((p,int(b),m,u))
print("files",len(rows),"bytes",sum(r[1] for r in rows))
top=collections.Counter(); topb=collections.Counter()
for p,b,_,_ in rows:
    parts=p.split('/'); k='/'.join(parts[:2]) if parts[0]=='sources' else parts[0]
    top[k]+=1; topb[k]+=b
print("\n== top-level groups")
for k,c in top.most_common(40): print(f"{c:8d} {topb[k]/1e9:8.2f}GB  {k}")
ext=collections.Counter(); extb=collections.Counter()
for p,b,_,_ in rows:
    e=os.path.splitext(p)[1].lower() or '(none)'
    ext[e]+=1; extb[e]+=b
print("\n== extensions (top 70)")
for k,c in ext.most_common(70): print(f"{c:8d} {extb[k]/1e9:8.2f}GB  {k}")
print("\nempty files:", sum(1 for r in rows if r[1]==0))
print("distinct ext:", len(ext))
md5=collections.Counter(r[2] for r in rows if r[2])
print("dup md5 groups:", sum(1 for v in md5.values() if v>1), "files in dup groups:", sum(v for v in md5.values() if v>1))
print("max depth:", max(p.count('/') for p,_,_,_ in rows))
