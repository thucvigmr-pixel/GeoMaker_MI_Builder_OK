# -*- coding: utf-8 -*-
"""Integration bridge from MapBasic 11 to the verified Trial04 raster engine.

A generated TAB is a *candidate* georeference derived from MapperInfo bounds;
it is not interchangeable with MapInfo's native geo-referenced Save Window TAB.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path
from PIL import Image

def parse_bounds(path):
    payload = {}
    for line in Path(path).read_text(encoding='cp1252',errors='replace').splitlines():
        if '=' in line:
            key,value=line.split('=',1)
            payload[key.strip().lower()]=value.strip()
    for key in ('png','minx','miny','maxx','maxy','coordsys'):
        if not payload.get(key):
            raise ValueError('Missing field '+key)
    for key in ('minx','miny','maxx','maxy'):
        payload[key]=float(payload[key])
    if payload['maxx']<=payload['minx'] or payload['maxy']<=payload['miny']:
        raise ValueError('Invalid map bounds')
    return payload

def make_registration(info, image, output):
    # Strictly use exact current exported PNG pixel size, not an assumed DPI-based width.
    with Image.open(image) as im:
        width,height=im.size
    if width<2 or height<2:
        raise ValueError('Invalid image size')
    cs=info['coordsys'].strip()
    if not cs.lower().startswith('coordsys '):
        raise ValueError('Not a MapInfo CoordSys declaration')
    # A TAB raster requires 3 non-collinear registration points.
    # This is provisional until the actual MapInfo output bounds are validated.
    lines=['!table','!version 300','!charset WindowsLatin1','','Definition Table',
           '  File "'+Path(image).name+'"','  Type "RASTER"',
           f'  ({info["minx"]:.12f},{info["maxy"]:.12f}) (0,0) Label "Pt 1",',
           f'  ({info["maxx"]:.12f},{info["maxy"]:.12f}) ({width},0) Label "Pt 2",',
           f'  ({info["minx"]:.12f},{info["miny"]:.12f}) (0,{height}) Label "Pt 3"',
           '  '+cs,'  Units "m"']
    Path(output).write_text('\n'.join(lines)+'\n',encoding='cp1252',errors='replace')
    return width,height

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--bounds',default=r'C:\Users\Public\Documents\GeoMaker_MI_Bridge.bounds.txt')
    ap.add_argument('--gui',default=str(Path(__file__).with_name('MI_MBtiles_Create.py')))
    ap.add_argument('--use-existing-tab',help='Preferred: raster TAB created natively by MI Save Window As')
    a=ap.parse_args()
    bounds=Path(a.bounds)
    if not bounds.is_file():
        raise FileNotFoundError('MapInfo bounds not found: '+str(bounds))
    info=parse_bounds(bounds)
    img=Path(info['png'])
    if not img.is_file():
        raise FileNotFoundError('MapInfo PNG not found: '+str(img))
    if a.use_existing_tab:
        tab=Path(a.use_existing_tab)
        if not tab.is_file(): raise FileNotFoundError(str(tab))
        native=True
    else:
        tab=img.with_suffix('.TAB')
        if tab.exists():
            # Do not overwrite a native MapInfo registration supplied by the user.
            tab=img.with_name(img.stem+'_PROVISIONAL.TAB')
        w,h=make_registration(info,img,tab)
        print('Generated provisional raster TAB:',tab,'size=',w,h)
        print('WARNING: MapperInfo envelope may differ from Save Window geo-referenced TAB.')
        native=False
    cmd=[sys.executable, a.gui,'--oneclick','--mi-image',str(img),'--mi-tab',str(tab),'--mi-dpi',info.get('dpi','500'), '--mi-minzoom', info.get('minzoom','10'), '--mi-maxzoom', info.get('maxzoom','17')]
    if not native:cmd.append('--provisional')
    return subprocess.call(cmd)

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print('GeoMaker bridge error:',exc,file=sys.stderr)
        input('Press Enter to exit...')
        raise SystemExit(1)
