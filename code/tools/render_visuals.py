# Usage: python -I render_visuals.py <input_root> <out.json>
import sys, os, io, json, base64, zipfile, pymupdf
from PIL import Image
root, outp = sys.argv[1], sys.argv[2]
def b64(im, q=72):
    buf=io.BytesIO(); im.convert('RGB').save(buf,'JPEG',quality=q); return 'data:image/jpeg;base64,'+base64.b64encode(buf.getvalue()).decode()
vis=[]
for dp,_,fn in os.walk(root):
    for n in fn:
        p=os.path.join(dp,n); rel=os.path.relpath(p,root); low=n.lower()
        try:
            if low.endswith('.pdf') and os.path.getsize(p)>0:
                d=pymupdf.open(p); pg=d[0]; r=pg.rect
                full=pg.get_pixmap(matrix=pymupdf.Matrix(560/r.width,560/r.width))
                im=Image.open(io.BytesIO(full.tobytes('png')))
                clip=pymupdf.Rect(r.x0+r.width*0.45, r.y0+r.height*0.66, r.x1, r.y1)
                tb=pg.get_pixmap(matrix=pymupdf.Matrix(900/clip.width,900/clip.width), clip=clip)
                vis.append({'file':rel,'kind':'PDF page 1','img':b64(im),'crop':b64(Image.open(io.BytesIO(tb.tobytes('png'))),80)})
            elif low.endswith(('.jpg','.jpeg','.png','.tif')) and os.path.getsize(p)>0:
                im=Image.open(p); im.thumbnail((560,560)); vis.append({'file':rel,'kind':'Raster image','img':b64(im)})
            elif low.endswith(('.3mf','.fcstd','.odg','.f3d')):
                z=zipfile.ZipFile(p)
                for m in z.namelist():
                    if 'thumb' in m.lower() and m.lower().endswith('.png'):
                        im=Image.open(io.BytesIO(z.read(m))); im.thumbnail((360,360))
                        vis.append({'file':rel+' !/ '+m,'kind':'Embedded preview inside native file','img':b64(im)})
        except Exception as e: print('skip',rel,e)
json.dump(vis,open(outp,'w'))
print(len(vis),'visuals', sum(len(v['img'])+len(v.get('crop','')) for v in vis)//1000,'KB')
