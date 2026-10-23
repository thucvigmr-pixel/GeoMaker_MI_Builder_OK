# -*- coding: utf-8 -*-
"""Optional non-destructive crop; all coordinates are original PNG pixels."""
from PIL import Image, ImageChops

def validate_box(box, size):
    w, h = size
    l,t,r,b = [int(round(float(x))) for x in box]
    if not (0 <= l < r <= w and 0 <= t < b <= h):
        raise ValueError('Khung cắt không hợp lệ hoặc vượt khỏi kích thước ảnh.')
    if r-l < 32 or b-t < 32:
        raise ValueError('Khung cắt quá nhỏ (<32 pixel).')
    return (l,t,r,b)

def _content_extent(image, max_side=1800):
    """Ignore the 1px image-canvas outline that MI Save Window sometimes adds.
    Count interior ink per scanline; reject isolated specks, not real labels.
    """
    w,h=image.size
    sample=image.convert('RGB');sample.thumbnail((max_side,max_side),Image.Resampling.BILINEAR)
    sw,sh=sample.size
    px=sample.load()
    # Keep a safe inset: the outermost canvas frame is not map content.
    inset=max(3,round(min(sw,sh)*0.003))
    rows=[0]*sh;cols=[0]*sw
    for y in range(inset,sh-inset):
        for x in range(inset,sw-inset):
            r,g,b=px[x,y]
            if min(r,g,b)<220 or (max(r,g,b)-min(r,g,b)>25 and min(r,g,b)<245):
                rows[y]+=1;cols[x]+=1
    yt=max(3,round((sw-2*inset)*0.002))
    xt=max(3,round((sh-2*inset)*0.002))
    ys=[i for i in range(inset,sh-inset) if rows[i]>=yt]
    xs=[i for i in range(inset,sw-inset) if cols[i]>=xt]
    if not xs or not ys:raise ValueError('Không tìm thấy vùng nội dung; hãy chỉnh cắt thủ công.')
    return (int(xs[0]*w/sw),int(ys[0]*h/sh),min(w,(xs[-1]+1)*w//sw+1),min(h,(ys[-1]+1)*h//sh))

def balanced_margins(image, padding_ratio=0.012):
    """Conservative usable-content box, preserving title, legend and scale bar.
    Thin image border is ignored; margins are made equal where possible.
    """
    w,h=image.size
    l,t,r,b=_content_extent(image)
    pad=max(8,round(min(w,h)*padding_ratio))
    # Equal remaining visible margins, avoiding any content removal.
    box=validate_box((max(0,l-pad),max(0,t-pad),min(w,r+pad),min(h,b+pad)),(w,h))
    if box==(0,0,w,h):
        raise ValueError('Không tìm thấy rìa trống đủ lớn để cắt an toàn. Không thay đổi ảnh.')
    return box

def frame_box(image):
    """Detect cartographic outer neatline, never merely the longest grid line.

    Uses dark-line support and confirms four sides plus corner intersections.
    Returns original-image pixel coordinates; raises on ambiguous sheets.
    """
    from PIL import Image

    w, h = image.size
    sample = image.convert('L')
    sample.thumbnail((1200, 1200), Image.Resampling.BILINEAR)
    sw, sh = sample.size
    # Pillow-only implementation: no new NumPy dependency on the user's Python.
    pix = sample.load()
    inset = max(4, round(min(sw, sh)*0.006))
    rows=[]; cols=[]
    for y in range(inset,sh-inset):
        if sum(pix[x,y]<145 for x in range(inset,sw-inset)) >= .50*(sw-2*inset):
            rows.append(y)
    for x in range(inset,sw-inset):
        if sum(pix[x,y]<145 for y in range(inset,sh-inset)) >= .50*(sh-2*inset):
            cols.append(x)
    if len(rows) < 2 or len(cols) < 2:
        raise ValueError('Không tìm được các cạnh khung đủ dài; xin chọn thủ công L/T/R/B.')

    # Group neighboring scanlines, retaining the central location.
    def groups(numbers, gap=3):
        result=[]; current=[numbers[0]]
        for n in numbers[1:]:
            if n-current[-1] <= gap:
                current.append(n)
            else:
                result.append(round(sum(current)/len(current))); current=[n]
        result.append(round(sum(current)/len(current)))
        return result

    ys=groups(rows); xs=groups(cols)
    candidates=[]
    for top in ys:
        for bottom in ys:
            rh=bottom-top
            if rh < sh*.56 or rh > sh*.97: continue
            for left in xs:
                for right in xs:
                    rw=right-left
                    if rw < sw*.50 or rw > sw*.97: continue
                    # Four sides must be supported by genuine continuous lines,
                    # including the four corners. A random grid-line pair
                    # cannot pass all these tests reliably.
                    thick=2
                    # Sample 300 points on each side, allowing small gaps
                    # around map labels and ticks. The line is at most 5px thick.
                    def support_h(y):
                        samples=min(300,right-left+1)
                        hits=0
                        for k in range(samples):
                            x=left + (right-left)*k//max(1,samples-1)
                            if any(pix[x,yy]<145 for yy in range(max(0,y-thick),min(sh,y+thick+1))):
                                hits+=1
                        return hits/samples
                    def support_v(x):
                        samples=min(300,bottom-top+1)
                        hits=0
                        for k in range(samples):
                            y=top + (bottom-top)*k//max(1,samples-1)
                            if any(pix[xx,y]<145 for xx in range(max(0,x-thick),min(sw,x+thick+1))):
                                hits+=1
                        return hits/samples
                    supports=[support_h(top),support_h(bottom),support_v(left),support_v(right)]
                    if min(supports)<.83: continue
                    corners=((left,top),(right,top),(left,bottom),(right,bottom))
                    if any(sum(pix[xx,yy]<145 for xx in range(max(0,cx-4),min(sw,cx+5))
                               for yy in range(max(0,cy-4),min(sh,cy+5))) < 6
                           for cx,cy in corners):
                        continue
                    # Favour the largest complete neatline enclosing the map.
                    score=(rw*rh)/(sw*sh) + .1*sum(supports)/4
                    candidates.append((score,left,top,right,bottom,supports))
    if not candidates:
        raise ValueError('Khung chưa đủ độ tin cậy (bốn cạnh/góc); hãy nhập L/T/R/B thủ công.')
    _, l,t,r,b,_=max(candidates,key=lambda item:item[0])
    # Retain the border stroke itself, with ~3 source-pixel safety padding.
    padx=max(3,round(w/sw*2));pady=max(3,round(h/sh*2))
    bounds=validate_box((max(0,int(l*w/sw)-padx),
                         max(0,int(t*h/sh)-pady),
                         min(w,int((r+1)*w/sw)+padx),
                         min(h,int((b+1)*h/sh)+pady)),(w,h))
    if bounds==(0,0,w,h):
        raise ValueError('Khung được phát hiện bằng toàn ảnh; không cắt để tránh sai.')
    return bounds

def cropped_points(points, box):
    l,t,_,_=box
    # Geographic x/y remain unchanged; only pixel origin shifts.
    return [(px-l,py-t,x,y) for px,py,x,y in points]
