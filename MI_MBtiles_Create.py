# -*- coding: utf-8 -*-
"""GeoMaker MBTiles Create – Version 1.2.5: calibrate a complete MapInfo page PNG and export raster MBTiles."""
import json, math, os, queue, re, shutil, sqlite3, subprocess, sys, tempfile, threading
from contextlib import closing
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
from mi_crop import balanced_margins, frame_box, validate_box, cropped_points

APP = 'GeoMaker MBTiles Create – Version 1.2.5'

def next_output_name(directory):
    directory = Path(directory)
    for n in range(1, 100000):
        candidate = directory / f'GeoMaker_{n:02d}.mbtiles'
        if not any(p.exists() for p in (candidate, candidate.with_suffix('.report.json'), candidate.with_suffix('.build.log'))):
            return candidate
    raise RuntimeError('Không thể tạo tên MBTiles mới')

GDAL_BIN_NAMES = ('gdal_translate', 'gdalwarp', 'gdaladdo', 'gdalsrsinfo')

def find_tool(name, custom=''):
    exe = name + ('.exe' if os.name == 'nt' else '')
    opts = []
    if custom: opts.extend([Path(custom)/exe, Path(custom)/'bin'/exe])
    p = shutil.which(name)
    if p: opts.append(Path(p))
    for root in [Path('C:/Program Files'), Path('C:/OSGeo4W'), Path('C:/OSGeo4W64')]:
        if not root.exists(): continue
        if root.name == 'Program Files':
            opts.extend(sorted(root.glob('QGIS*/bin/'+exe), reverse=True))
            opts.extend(sorted(root.glob('QGIS*/apps/gdal/bin/'+exe), reverse=True))
        else: opts.append(root/'bin'/exe)
    for x in opts:
        if x.is_file(): return str(x)
    return None

def run_cmd(argv, log, env=None):
    log('$ ' + subprocess.list2cmdline([str(a) for a in argv]))
    proc = subprocess.Popen([str(a) for a in argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors='replace', env=env, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
    for line in proc.stdout: log(line.rstrip())
    code = proc.wait()
    if code: raise RuntimeError('Lệnh trả mã lỗi '+str(code)+': '+Path(str(argv[0])).name)

def check_mbtiles(p, minz, maxz):
    con = sqlite3.connect(p)
    try:
        chk = con.execute('PRAGMA integrity_check').fetchone()[0]
        metadata = dict(con.execute('SELECT name,value FROM metadata').fetchall())
        counts = dict(con.execute('SELECT zoom_level,count(*) FROM tiles GROUP BY zoom_level').fetchall())
        if chk != 'ok': raise RuntimeError('SQLite integrity_check: '+chk)
        if metadata.get('format','').lower() not in ('png','jpg','jpeg'):
            raise RuntimeError('Metadata format không phải raster PNG/JPEG')
        missing = [z for z in range(minz, maxz + 1) if counts.get(z, 0) < 1]
        unexpected = [z for z in counts if z < minz or z > maxz]
        if missing or unexpected:
            raise RuntimeError('Dải zoom không đúng. Thiếu: '+str(missing)+'; ngoài dải: '+str(unexpected)+'; đã có: '+str(counts))
        return {'metadata': metadata, 'tile_counts':counts, 'integrity':chk}
    finally: con.close()

def tile_counts(path):
    """Read tile counts without leaving a SQLite handle on Windows."""
    with closing(sqlite3.connect(str(path), timeout=30)) as db:
        return dict(db.execute('SELECT zoom_level, COUNT(*) FROM tiles GROUP BY zoom_level').fetchall())


def merge_zoom_tiles(main_mb, tile_mb, target_zoom):
    """Copy a single zoom safely, closing BOTH SQLite connections explicitly.

    Do not use ATTACH here: its DB handles and open cursors made temp-file
    cleanup unreliable on Windows in Fix02.
    """
    with closing(sqlite3.connect(str(tile_mb), timeout=30)) as source:
        with closing(sqlite3.connect(str(main_mb), timeout=30)) as target:
            count = 0
            try:
                cursor = source.execute(
                    'SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level=?',
                    (target_zoom,))
                while True:
                    batch = cursor.fetchmany(256)
                    if not batch:
                        break
                    target.executemany(
                        'INSERT OR REPLACE INTO tiles '
                        '(zoom_level, tile_column, tile_row, tile_data) '
                        'VALUES (?, ?, ?, ?)',
                        [(target_zoom, col, row, data) for col, row, data in batch])
                    count += len(batch)
                cursor.close()
                target.commit()
            except Exception:
                target.rollback()
                raise
            return count


def supplement_missing_zooms(tools, source_vrt, srs_file, main_mb, tempdir, lo, hi, fmt, log, env):
    """Build absent low zooms; explicitly close all SQLite files before cleanup."""
    counts = tile_counts(main_mb)
    extra = [z for z in counts if z < lo or z > hi]
    if extra:
        raise RuntimeError(f'GDAL xuất zoom ngoài dải yêu cầu: {extra}. Dừng để tránh kết quả sai.')
    missing = [z for z in range(lo, hi + 1) if counts.get(z, 0) == 0]
    if not missing:
        return
    log(f'GDAL overview không tạo đủ mức zoom; bổ sung riêng: {missing}')
    for z in missing:
        res = 2 * math.pi * 6378137.0 / (256 * (2 ** z))
        tif = tempdir / f'missing_z{z}.tif'
        mb = tempdir / f'missing_z{z}.mbtiles'
        run_cmd([tools['gdalwarp'], '-overwrite', '-of', 'GTiff',
                 '-s_srs', str(srs_file), '-t_srs', 'EPSG:3857',
                 '-order', '1', '-r', 'bilinear', '-dstalpha',
                 '-tr', f'{res:.12f}', f'{res:.12f}',
                 '-co', 'TILED=YES', '-co', 'COMPRESS=LZW',
                 str(source_vrt), str(tif)], log, env)
        run_cmd([tools['gdal_translate'], '-of', 'MBTILES',
                 '-co', 'TILE_FORMAT=' + fmt, str(tif), str(mb)], log, env)
        c = tile_counts(mb)
        if not c.get(z, 0):
            raise RuntimeError(f'GDAL không tạo được tile tại zoom {z}; mức thực tế: {c}')
        if any(k != z for k in c):
            raise RuntimeError(f'Tệp tạm zoom {z} chứa mức ngoài dự kiến: {c}')
        n = merge_zoom_tiles(main_mb, mb, z)
        if n != c[z]:
            raise RuntimeError(f'Ghép zoom {z} không đủ tile: {n}/{c[z]}')
        log(f'Đã ghép zoom {z}: {n} tile')
    with closing(sqlite3.connect(str(main_mb), timeout=30)) as db:
        try:
            for key, val in [('minzoom', str(lo)), ('maxzoom', str(hi))]:
                db.execute('DELETE FROM metadata WHERE name=?', (key,))
                db.execute('INSERT INTO metadata(name, value) VALUES (?,?)', (key, val))
            db.commit()
        except Exception:
            db.rollback()
            raise


# MapInfo Save Window As -> 'Create a Geographically Referenced Tab File'.
# These are image registration points, NOT a vector TAB.
REGISTRATION_RE = re.compile(
    r'^\s*\(\s*([+-]?[0-9.]+)\s*,\s*([+-]?[0-9.]+)\s*\)'
    r'\s*\(\s*([+-]?[0-9.]+)\s*,\s*([+-]?[0-9.]+)\s*\)'
    r'\s*Label\s+"', re.IGNORECASE | re.MULTILINE)

def read_raster_tab(tab_path, image_path):
    """Read MapInfo-generated raster TAB control points; reject mismatched images."""
    data = Path(tab_path).read_bytes()
    # TAB is typically Windows-1252; bytes used for coordinate records are ASCII.
    content = data.decode('cp1252', errors='replace')
    if not re.search(r'^\s*Type\s+"RASTER"', content, re.I | re.M):
        raise ValueError('TAB không phải raster TAB định vị ảnh (Type "RASTER").')
    m = re.search(r'^\s*File\s+"([^"]+)"', content, re.I | re.M)
    if not m:
        raise ValueError('TAB thiếu mục File ảnh.')
    img_ref = m.group(1).replace('\\', '/')
    referenced = Path(img_ref)
    if not referenced.is_absolute():
        referenced = Path(tab_path).parent / referenced
    # Require the exact source image to prevent silently applying wrong georeferencing.
    if referenced.resolve() != Path(image_path).resolve():
        raise ValueError('TAB tham chiếu ảnh khác:\n' + str(referenced) +
                         '\nẢnh đang chọn: ' + str(image_path))
    pts = [(float(px),float(py),float(x),float(y))
           for x,y,px,py in REGISTRATION_RE.findall(content)]
    if len(pts) < 3:
        raise ValueError('TAB raster thiếu 3 điểm khống chế hợp lệ.')
    a,b,c = pts[:3]
    cross = abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))
    if cross < 1:
        raise ValueError('Các điểm pixel đăng ký trong TAB bị thẳng hàng.')
    if not all(math.isfinite(v) for q in pts for v in q):
        raise ValueError('TAB chứa tọa độ không hợp lệ.')
    return pts

def make_crs_mif_from_raster_tab(tab_path, mif_path):
    """Build a temporary OGR-readable MIF carrying MapInfo CoordSys verbatim.
    Raster TAB is not an OGR vector dataset; gdalsrsinfo on it may fail.
    """
    content=Path(tab_path).read_bytes().decode('cp1252', errors='replace')
    m=re.search(r'^\s*CoordSys\s+(.+?)(?=^\s*(?:Units|RasterStyle|begin_metadata|end_metadata)\b|\Z)',
                content, flags=re.I|re.M|re.S)
    if not m:
        raise ValueError('TAB không có CoordSys hợp lệ.')
    coordsys=' '.join(('CoordSys '+m.group(1)).split())
    # MIF uses its own line for the CoordSys definition; retain every original datum parameter.
    mif=Path(mif_path)
    mif.write_text('Version 300\nCharset "WindowsLatin1"\nDelimiter ","\n'+coordsys+
                   '\nColumns 1\n  gm_id Integer\nData\nPoint 0 0\n',encoding='ascii')
    mif.with_suffix('.mid').write_text('1\n',encoding='ascii')
    return coordsys

def raster_pixel_size_metres(points):
    """Affine-scale approximation, valid for projected CRS in metres only."""
    a,b,c=points[:3]
    dp=(b[0]-a[0], b[1]-a[1]); dq=(c[0]-a[0], c[1]-a[1])
    dx=(b[2]-a[2], b[3]-a[3]); dy=(c[2]-a[2],c[3]-a[3])
    det=dp[0]*dq[1]-dp[1]*dq[0]
    if abs(det)<1: raise ValueError('Các GCP không độc lập.')
    vx=((dx[0]*dq[1]-dy[0]*dp[1])/det, (dx[1]*dq[1]-dy[1]*dp[1])/det)
    vy=((dy[0]*dp[0]-dx[0]*dq[0])/det, (dy[1]*dp[0]-dx[1]*dq[0])/det)
    return math.hypot(*vx),math.hypot(*vy)

class GUI:
    def __init__(self, root):
        self.root=root; root.title(APP); root.geometry('1120x790'); root.minsize(1080,670)
        self.src=tk.StringVar(value=r'C:\Users\Public\Documents\GeoMaker_MI_Create.png')
        self.tab=tk.StringVar(); self.out=tk.StringVar(value=str(Path.home()/'Documents'/'GeoMaker_MI_Create.mbtiles'))
        self.gdal=tk.StringVar(); self.fmt=tk.StringVar(value='PNG'); self.zmin=tk.IntVar(value=10);self.zmax=tk.IntVar(value=17)
        self.x=tk.StringVar(); self.y=tk.StringVar(); self.points=[]; self.pic=None;self.view_scale=1;self.base_scale=1;self.img=None
        self.events=queue.Queue();self.active=False;self.oneclick=False;self._log_file=None
        self.provisional=False
        self.crop_enabled=tk.BooleanVar(value=False); self.crop_mode=tk.StringVar(value='margin'); self.crop_fields=[tk.StringVar() for _ in range(4)]; self.crop_rect=None; self._crop_drag_start=None; self._crop_action=None; self._crop_origin=None; self._crop_initial=None
        self.geo_mode=tk.StringVar(value='auto'); self.dpi_src=tk.StringVar(value='500'); self.latitude=tk.StringVar(value='20.0')
        self._ui();self.root.after(100,self._drain)
    def _ui(self):
        top=ttk.Frame(self.root,padding=10);top.pack(fill='x')
        ttk.Label(top,text=APP,font=('Segoe UI',16,'bold')).pack(anchor='w')
        ttk.Label(top,text='Xuất MBTiles tự động từ MapInfo Pro 15: kiểm tra file MBTiles đầu ra rồi nhấn TẠO MBTILES. Có sẵn chế độ thủ công tạo MBTiles từ Raster.').pack(anchor='w')
        inputs=ttk.Frame(top);inputs.pack(fill='x',pady=6)
        self._file_row(inputs,0,'PNG trang bản đồ',self.src,lambda:self._browse_file(self.src,[('PNG','*.png')]))
        self._file_row(inputs,1,'TAB raster / TAB tham chiếu CRS',self.tab,lambda:self._browse_file(self.tab,[('MapInfo TAB','*.tab')]))
        self._file_row(inputs,2,'MBTiles đầu ra',self.out,self._save)
        self._file_row(inputs,3,'Thư mục GDAL (nếu cần)',self.gdal,lambda:self.gdal.set(filedialog.askdirectory() or self.gdal.get()))
        row=ttk.Frame(top);row.pack(fill='x',pady=2)
        ttk.Label(row,text='Định dạng').pack(side='left');ttk.Combobox(row,textvariable=self.fmt,values=['PNG','JPEG'],width=9,state='readonly').pack(side='left',padx=5)
        ttk.Label(row,text='Zoom từ').pack(side='left',padx=(16,4));ttk.Spinbox(row,from_=0,to=22,textvariable=self.zmin,width=5).pack(side='left')
        ttk.Label(row,text='đến').pack(side='left',padx=5);ttk.Spinbox(row,from_=0,to=22,textvariable=self.zmax,width=5).pack(side='left')
        ttk.Button(row,text='Kiểm tra công cụ',command=self._check).pack(side='left',padx=16)
        ttk.Button(row,text='Mở ảnh',command=self._open).pack(side='left')
        ttk.Button(row,text='Mở thư mục đầu ra',command=self._open_output_folder).pack(side='left',padx=8)
        options=ttk.Frame(top);options.pack(fill='x',pady=3)
        ttk.Label(options,text='Định vị:').pack(side='left')
        ttk.Radiobutton(options,text='Tự động từ TAB raster',variable=self.geo_mode,value='auto').pack(side='left',padx=8)
        ttk.Radiobutton(options,text='Thủ công (Fix04)',variable=self.geo_mode,value='manual').pack(side='left',padx=8)
        ttk.Button(options,text='Đọc TAB / Nạp điểm',command=self._read_auto).pack(side='left',padx=10)
        dpirow=ttk.Frame(top);dpirow.pack(fill='x',pady=3)
        ttk.Label(dpirow,text='DPI ảnh MI hiện tại:').pack(side='left')
        ttk.Entry(dpirow,textvariable=self.dpi_src,width=7).pack(side='left',padx=5)
        ttk.Label(dpirow,text='Vĩ độ gần đúng (°):').pack(side='left',padx=(15,4))
        ttk.Entry(dpirow,textvariable=self.latitude,width=8).pack(side='left')
        ttk.Button(dpirow,text='Tính DPI / kích thước ảnh đề xuất',command=self._estimate_dpi).pack(side='left',padx=12)
        cropbar=ttk.LabelFrame(top,text='Cắt ảnh trước khi xuất – tùy chọn');cropbar.pack(fill='x',pady=3)
        ttk.Checkbutton(cropbar,text='Bật cắt',variable=self.crop_enabled,command=self._crop_toggle).pack(side='left',padx=6)
        ttk.Radiobutton(cropbar,text='Theo khung bản đồ',variable=self.crop_mode,value='frame',command=self._crop_mode_changed).pack(side='left',padx=5)
        ttk.Radiobutton(cropbar,text='Cân đối rìa trống',variable=self.crop_mode,value='margin',command=self._crop_mode_changed).pack(side='left',padx=5)
        ttk.Radiobutton(cropbar,text='Tùy chỉnh',variable=self.crop_mode,value='custom',command=self._crop_mode_changed).pack(side='left',padx=5)
        ttk.Button(cropbar,text='Phát hiện',command=self._crop_detect).pack(side='left',padx=6)
        ttk.Button(cropbar,text='Xem vùng cắt',command=self._crop_draw).pack(side='left',padx=4)
        for lbl,v in zip(('L','T','R','B'),self.crop_fields):
            ttk.Label(cropbar,text=lbl).pack(side='left',padx=(5,0))
            ttk.Entry(cropbar,textvariable=v,width=6).pack(side='left',padx=1)
        box=ttk.Panedwindow(self.root,orient='horizontal');box.pack(fill='both',expand=True,padx=10,pady=4)
        left=ttk.Frame(box);right=ttk.Frame(box);box.add(left,weight=3);box.add(right,weight=2)
        viewbar=ttk.Frame(left);viewbar.pack(fill='x',pady=(0,4))
        ttk.Button(viewbar,text='− Thu nhỏ',command=lambda:self._zoom(1/1.5)).pack(side='left',padx=(0,4))
        ttk.Button(viewbar,text='+ Phóng to',command=lambda:self._zoom(1.5)).pack(side='left',padx=(0,4))
        ttk.Button(viewbar,text='Vừa khung',command=self._fit).pack(side='left',padx=(0,8))
        self.zoom_label=ttk.Label(viewbar,text='100% ảnh gốc');self.zoom_label.pack(side='left')
        viewer=ttk.Frame(left);viewer.pack(fill='both',expand=True)
        self.canvas=tk.Canvas(viewer,bg='#777',highlightthickness=1,highlightbackground='#aaa',cursor='crosshair')
        vs=ttk.Scrollbar(viewer,orient='vertical',command=self.canvas.yview)
        hs=ttk.Scrollbar(viewer,orient='horizontal',command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=hs.set,yscrollcommand=vs.set)
        self.canvas.grid(row=0,column=0,sticky='nsew');vs.grid(row=0,column=1,sticky='ns');hs.grid(row=1,column=0,sticky='ew')
        viewer.rowconfigure(0,weight=1);viewer.columnconfigure(0,weight=1)
        self.canvas.bind('<Button-1>',self._canvas_press)
        self.canvas.bind('<B1-Motion>',self._canvas_drag)
        self.canvas.bind('<ButtonRelease-1>',self._canvas_release)
        for var in self.crop_fields: var.trace_add('write', lambda *_:self._crop_draw())
        self.canvas.bind('<Control-MouseWheel>',self._wheel_zoom)
        self.canvas.bind('<MouseWheel>',lambda e:self.canvas.yview_scroll(-int(e.delta/120),'units'))
        self.canvas.bind('<Shift-MouseWheel>',lambda e:self.canvas.xview_scroll(-int(e.delta/120),'units'))
        ttk.Label(right,text='Điểm khống chế (CRS của TAB, đơn vị theo nguồn)',font=('Segoe UI',11,'bold')).pack(anchor='w',padx=8,pady=4)
        ttk.Label(right,text='1) Mở ảnh. 2) Bấm đúng giao điểm lưới. 3) Nhập X/Y in trên lưới, bấm Thêm. Lặp lại ≥3 điểm không thẳng hàng.',wraplength=340).pack(anchor='w',padx=8)
        row=ttk.Frame(right);row.pack(fill='x',padx=8,pady=6)
        ttk.Label(row,text='X').pack(side='left');ttk.Entry(row,textvariable=self.x,width=15).pack(side='left',padx=3)
        ttk.Label(row,text='Y').pack(side='left');ttk.Entry(row,textvariable=self.y,width=15).pack(side='left',padx=3)
        ttk.Button(right,text='Thêm điểm vừa bấm',command=self._add).pack(anchor='w',padx=8)
        self.tree=ttk.Treeview(right,columns=('px','py','x','y'),show='headings',height=8)
        for c in ('px','py','x','y'):self.tree.heading(c,text=c.upper());self.tree.column(c,width=75,anchor='center')
        self.tree.pack(fill='both',expand=True,padx=8,pady=6)
        actions=ttk.Frame(right);actions.pack(fill='x',padx=8)
        ttk.Button(actions,text='Xóa điểm chọn',command=self._remove).pack(side='left')
        ttk.Button(actions,text='Lưu điểm',command=self._save_points).pack(side='left',padx=4)
        ttk.Button(actions,text='Đọc điểm',command=self._load_points).pack(side='left')
        self.go=ttk.Button(right,text='TẠO RASTER MBTILES',command=self._start);self.go.pack(fill='x',padx=8,pady=(12,4))
        self.progress=ttk.Progressbar(right,mode='indeterminate');self.progress.pack(fill='x',padx=8,pady=(0,7))
        ttk.Label(self.root,text='Nhật ký xử lý').pack(anchor='w',padx=10)
        self.log=tk.Text(self.root,height=9,bg='#152432',fg='#d7e8ed',font=('Consolas',9));self.log.pack(fill='x',padx=10,pady=(0,10))
        self.root.after(300,self._open)
    def _crop_toggle(self):
        if self.crop_enabled.get():
            self._crop_detect()
        else:
            self._crop_draw()
    def _crop_mode_changed(self):
        if not self.crop_enabled.get():
            self.crop_enabled.set(True)
        if self.crop_mode.get() == 'custom':
            if self.img is not None:
                try: self._crop_box()
                except (ValueError,TypeError):
                    for v,n in zip(self.crop_fields,(0,0,*self.img.size)): v.set(str(n))
            self._msg('[CẮT TÙY CHỈNH] Kéo bên trong để di chuyển; kéo cạnh/góc để đổi kích thước; kéo ngoài khung để vẽ mới; hoặc nhập L/T/R/B.')
            self._crop_draw()
        else:
            self._crop_detect()

    def _crop_detect(self):
        if self.img is None:
            self._open()
        if self.img is None:return
        if self.crop_mode.get() == 'custom':
            self._msg('[CẮT] Chế độ tùy chỉnh không chạy phát hiện tự động. Kéo chuột trên ảnh hoặc nhập L/T/R/B.')
            return
        try:
            box = frame_box(self.img) if self.crop_mode.get()=='frame' else balanced_margins(self.img)
            for v,n in zip(self.crop_fields,box): v.set(str(n))
            self.crop_enabled.set(True)
            self._crop_draw()
            w,h=self.img.size; l,t,r,b=box
            saved=100*(1-((r-l)*(b-t))/(w*h))
            self._msg(f'[CẮT] Đề xuất {box}; giảm {saved:.1f}% diện tích pixel. Xem trước trước khi xuất.')
        except Exception as exc:
            messagebox.showwarning('Chưa xác định vùng cắt',str(exc))
    def _crop_box(self):
        if self.img is None:raise ValueError('Chưa mở ảnh để xác nhận khung cắt.')
        return validate_box(tuple(v.get() for v in self.crop_fields),self.img.size)
    def _crop_draw(self):
        if not hasattr(self,'canvas'):return
        self.canvas.delete('crop-preview')
        if not self.crop_enabled.get() or self.img is None:return
        try:
            l,t,r,b=self._crop_box();f=self.view_scale
            self.canvas.create_rectangle(l*f,t*f,r*f,b*f,outline='#00d5ee',width=2,dash=(8,4),tags='crop-preview')
            if self.crop_mode.get()=='custom':
                midx=(l+r)/2; midy=(t+b)/2
                handles={'nw':(l,t),'n':(midx,t),'ne':(r,t),'e':(r,midy),'se':(r,b),'s':(midx,b),'sw':(l,b),'w':(l,midy)}
                for _,(px,py) in handles.items():
                    u,v=px*f,py*f
                    self.canvas.create_rectangle(u-5,v-5,u+5,v+5,fill='#fff',outline='#007c95',width=2,tags='crop-preview')
        except (ValueError, TypeError):pass
    def _file_row(self,parent,row,label,var,cmd):
        ttk.Label(parent,text=label,width=32).grid(row=row,column=0,sticky='w',pady=2)
        ttk.Entry(parent,textvariable=var).grid(row=row,column=1,sticky='ew',padx=5)
        ttk.Button(parent,text='Chọn...',command=cmd).grid(row=row,column=2)
        parent.grid_columnconfigure(1,weight=1)
    def _browse_file(self,var,types):
        val=filedialog.askopenfilename(filetypes=types+[('Tất cả','*.*')]);
        if val:var.set(val)
    def _save(self):
        val=filedialog.asksaveasfilename(defaultextension='.mbtiles',filetypes=[('MBTiles','*.mbtiles')]);
        if val:self.out.set(val)
    def _msg(self,s):
        self.log.insert('end',s+'\n');self.log.see('end')
        try:
            log_path = Path(self.out.get()).with_suffix('.build.log')
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open('a', encoding='utf-8') as fh:
                fh.write(s+'\n')
        except OSError:
            pass
    def _confirm_oneclick(self):
        # Keep legacy entry point but never automatically start an export.
        # User reviews output path in the GUI and clicks TẠO RASTER MBTILES.
        if not self.oneclick:
            return
        if not Path(self.src.get()).is_file() or not Path(self.tab.get()).is_file():
            self._msg('[LỖI] Không tìm thấy ảnh hoặc TAB từ MapInfo. Chọn lại nguồn trong GUI.')
            return
        self.geo_mode.set('auto')
        self._msg('[MI] Đã nạp dữ liệu. Kiểm tra tên tệp đầu ra trước khi nhấn TẠO RASTER MBTILES.')

    def _open(self):
        if not Path(self.src.get()).is_file():return
        try:
            if self.img is not None:self.img.close()
            self.img=Image.open(self.src.get()).convert('RGB')
            self.loaded_image=self.src.get()
            w,h=self.img.size
            self.base_scale=min(680/w,510/h,1.0)
            self.view_scale=self.base_scale
            self._render_preview()
            self._msg('Ảnh: %d × %d pixel. Zoom chỉ ảnh hưởng việc chấm điểm, không đổi ảnh nguồn.'%(w,h))
        except Exception as e:messagebox.showerror('Lỗi ảnh',str(e))
    def _render_preview(self,center=None):
        if self.img is None:return
        w,h=self.img.size
        pw,ph=max(1,round(w*self.view_scale)),max(1,round(h*self.view_scale))
        if pw*ph>75_000_000:
            raise ValueError('Mức phóng quá lớn, yêu cầu nhiều bộ nhớ.')
        preview=self.img.resize((pw,ph),Image.Resampling.BILINEAR)
        self.pic=ImageTk.PhotoImage(preview)
        self.canvas.delete('all')
        self.canvas.configure(scrollregion=(0,0,pw,ph))
        self.canvas.create_image(0,0,image=self.pic,anchor='nw',tags='photo')
        self.zoom_label.configure(text=f'{self.view_scale*100:.1f}% ảnh gốc')
        self._dots()
        self._crop_draw()
        if center is not None:
            # Restore geographic pixel at visual center after zoom.
            self.canvas.update_idletasks()
            cw=max(1,self.canvas.winfo_width());ch=max(1,self.canvas.winfo_height())
            self.canvas.xview_moveto(max(0,(center[0]*self.view_scale-cw/2)/max(1,pw)))
            self.canvas.yview_moveto(max(0,(center[1]*self.view_scale-ch/2)/max(1,ph)))
    def _zoom(self,factor):
        if self.img is None:return
        center=((self.canvas.canvasx(self.canvas.winfo_width()/2))/self.view_scale,
                (self.canvas.canvasy(self.canvas.winfo_height()/2))/self.view_scale)
        new_scale=min(4.0,max(self.base_scale,self.view_scale*factor))
        if abs(new_scale-self.view_scale)<1e-9:return
        self.view_scale=new_scale
        try:self._render_preview(center)
        except Exception as e:messagebox.showerror('Zoom ảnh',str(e))
    def _fit(self):
        if self.img is None:return
        self.view_scale=self.base_scale
        self._render_preview()
        self.canvas.xview_moveto(0);self.canvas.yview_moveto(0)
    def _wheel_zoom(self,event):
        self._zoom(1.5 if event.delta>0 else 1/1.5)
        return 'break'
    def _canvas_pixel(self,event):
        if self.img is None: return None
        w,h=self.img.size
        x=round(self.canvas.canvasx(event.x)/self.view_scale)
        y=round(self.canvas.canvasy(event.y)/self.view_scale)
        return (min(w,max(0,x)),min(h,max(0,y)))

    def _crop_hit(self, point):
        """Return a corner/edge/move/new interaction based on image pixel coordinates."""
        try:l,t,r,b=self._crop_box()
        except (ValueError,TypeError):return 'new'
        x,y=point; tolerance=max(5.0, 10.0/max(self.view_scale,0.01))
        handles={
            'nw':(l,t),'n':((l+r)/2,t),'ne':(r,t),
            'e':(r,(t+b)/2),'se':(r,b),'s':((l+r)/2,b),
            'sw':(l,b),'w':(l,(t+b)/2)
        }
        nearest=min(handles,key=lambda k:(handles[k][0]-x)**2+(handles[k][1]-y)**2)
        hx,hy=handles[nearest]
        if abs(hx-x)<=tolerance and abs(hy-y)<=tolerance:return nearest
        on_l=abs(x-l)<=tolerance and t-tolerance<=y<=b+tolerance
        on_r=abs(x-r)<=tolerance and t-tolerance<=y<=b+tolerance
        on_t=abs(y-t)<=tolerance and l-tolerance<=x<=r+tolerance
        on_b=abs(y-b)<=tolerance and l-tolerance<=x<=r+tolerance
        if on_l:return 'w'
        if on_r:return 'e'
        if on_t:return 'n'
        if on_b:return 's'
        if l<x<r and t<y<b:return 'move'
        return 'new'

    def _crop_transform(self,action,initial,start,end):
        l,t,r,b=initial
        dx=end[0]-start[0];dy=end[1]-start[1]
        w,h=self.img.size
        minsize=10
        if action=='new':
            return (min(start[0],end[0]),min(start[1],end[1]),max(start[0],end[0]),max(start[1],end[1]))
        if action=='move':
            dx=max(-l,min(w-r,dx));dy=max(-t,min(h-b,dy))
            return (l+dx,t+dy,r+dx,b+dy)
        if 'w' in action:l=max(0,min(r-minsize,l+dx))
        if 'e' in action:r=min(w,max(l+minsize,r+dx))
        if 'n' in action:t=max(0,min(b-minsize,t+dy))
        if 's' in action:b=min(h,max(t+minsize,b+dy))
        return (l,t,r,b)

    def _canvas_press(self,event):
        if self.crop_enabled.get() and self.crop_mode.get()=='custom':
            point=self._canvas_pixel(event)
            if point is None:return 'break'
            self._crop_origin=point
            try:self._crop_initial=self._crop_box()
            except (ValueError,TypeError):self._crop_initial=(0,0,*self.img.size)
            self._crop_action=self._crop_hit(point)
            return 'break'
        self._click(event)

    def _canvas_drag(self,event):
        if self._crop_action is None:return
        end=self._canvas_pixel(event)
        if end is None:return 'break'
        box=self._crop_transform(self._crop_action,self._crop_initial,self._crop_origin,end)
        try:box=validate_box(box,self.img.size)
        except ValueError:return 'break'
        self._set_crop_box(box)
        return 'break'

    def _set_crop_box(self,box):
        for var,n in zip(self.crop_fields,box):var.set(str(int(n)))
        self._crop_draw()

    def _canvas_release(self,event):
        if self._crop_action is None:return
        action=self._crop_action
        self._crop_action=None
        end=self._canvas_pixel(event)
        if end is None:return 'break'
        box=self._crop_transform(action,self._crop_initial,self._crop_origin,end)
        try:box=validate_box(box,self.img.size)
        except ValueError as exc:
            self._msg(f'[CẮT] Vùng chọn không hợp lệ: {exc}')
            return 'break'
        self._set_crop_box(box)
        w,h=self.img.size;l,t,r,b=box
        saved=100*(1-(r-l)*(b-t)/(w*h))
        self._msg(f'[CẮT TÙY CHỈNH] {action}: L={l}, T={t}, R={r}, B={b}; giữ {r-l} × {b-t} px, giảm {saved:.1f}% diện tích.')
        return 'break'

    def _click(self,event):
        if self.img is None:return
        self.clicked=(self.canvas.canvasx(event.x)/self.view_scale,self.canvas.canvasy(event.y)/self.view_scale)
        self._msg('Vừa chọn pixel (%.2f, %.2f). Nhập tọa độ giao lưới X,Y.'%self.clicked)
        self._dots()
    def _dots(self):
        self.canvas.delete('dot')
        for i,p in enumerate(self.points):
            u,v=p[:2];u*=self.view_scale;v*=self.view_scale
            self.canvas.create_oval(u-4,v-4,u+4,v+4,fill='lime',outline='black',tags='dot')
            self.canvas.create_text(u+10,v-8,text=str(i+1),fill='red',anchor='w',tags='dot')
        if hasattr(self,'clicked'):
            u,v=self.clicked;u*=self.view_scale;v*=self.view_scale
            self.canvas.create_line(u-8,v,u+8,v,fill='red',width=2,tags='dot')
            self.canvas.create_line(u,v-8,u,v+8,fill='red',width=2,tags='dot')
    def _add(self):
        try:
            if not hasattr(self,'clicked'):raise ValueError('Chưa bấm chọn giao điểm trên ảnh')
            x=float(self.x.get().replace(',','.'));y=float(self.y.get().replace(',','.'))
            p=(*self.clicked,x,y)
            if not all(math.isfinite(v) for v in p):raise ValueError('Tọa độ không hợp lệ')
            self.points.append(p);self._update_points()
        except Exception as e:messagebox.showerror('Điểm khống chế',str(e))
    def _update_points(self):
        for item in self.tree.get_children():self.tree.delete(item)
        for i,p in enumerate(self.points):self.tree.insert('', 'end',iid=str(i),values=tuple(round(v,2) for v in p))
        self._dots()
    def _remove(self):
        for i in sorted((int(a) for a in self.tree.selection()),reverse=True):del self.points[i]
        self._update_points()
    def _save_points(self):
        p=filedialog.asksaveasfilename(defaultextension='.json',filetypes=[('JSON','*.json')]);
        if p:Path(p).write_text(json.dumps({'image':self.src.get(),'tab':self.tab.get(),'points':self.points},ensure_ascii=False,indent=2),encoding='utf-8')
    def _load_points(self):
        p=filedialog.askopenfilename(filetypes=[('JSON','*.json')]);
        if not p:return
        obj=json.loads(Path(p).read_text(encoding='utf-8'))
        if 'image' in obj:self.src.set(obj['image'])
        if 'tab' in obj:self.tab.set(obj['tab'])
        self.points=[tuple(map(float,v)) for v in obj['points']];self._open();self._update_points()
    def _read_auto(self):
        try:
            if not Path(self.src.get()).is_file(): raise ValueError('Chưa chọn ảnh nguồn.')
            if not Path(self.tab.get()).is_file(): raise ValueError('Chưa chọn TAB raster.')
            pts=read_raster_tab(self.tab.get(),self.src.get())
            if self.img is None or Path(getattr(self,'loaded_image','')) != Path(self.src.get()): self._open()
            if self.img:
                w,h=self.img.size
                if any(px < -1 or px > w+1 or py < -1 or py > h+1 for px,py,_,_ in pts):
                    raise ValueError('GCP trong TAB nằm ngoài ảnh; kiểm tra TAB/PNG phù hợp.')
            self.points=pts; self._update_points()
            self.geo_mode.set('auto')
            self._msg(f'[AUTO TAB] Đọc {len(pts)} điểm đăng ký trực tiếp từ MapInfo; không chấm tay.')
        except Exception as e: messagebox.showerror('Đọc TAB tự động',str(e))
    def _estimate_dpi(self):
        try:
            if not Path(self.src.get()).is_file(): raise ValueError('Chọn ảnh trước.')
            if self.img is None: self._open()
            z=int(self.zmax.get()); lat=float(self.latitude.get().replace(',','.'))
            dpi=float(self.dpi_src.get().replace(',','.'))
            if not (0<=z<=22 and -85<=lat<=85 and 1<=dpi<=10000):
                raise ValueError('Zoom, vĩ độ hoặc DPI không hợp lệ.')
            if self.geo_mode.get()=='auto':
                pts=read_raster_tab(self.tab.get(),self.src.get())
            else:pts=self.points
            if len(pts)<3:raise ValueError('Chưa có đủ ba điểm để tính độ phân giải.')
            rx,ry=raster_pixel_size_metres(pts)
            target=156543.03392804097*math.cos(math.radians(lat))/(2**z)
            factor=max(rx,ry)/target
            req_dpi=math.ceil(dpi*factor)
            w,h=self.img.size
            rec_w=math.ceil(w*max(1,factor));rec_h=math.ceil(h*max(1,factor))
            self._msg(f'[CHẤT LƯỢNG] Ảnh {w}x{h}; {rx:.4f} x {ry:.4f} m/px (ước lượng), zoom {z}: {target:.4f} m/px tại {lat}°.')
            self._msg(f'[CHẤT LƯỢNG] DPI gợi ý >= {req_dpi} (từ DPI {dpi:g}); cỡ ảnh >= {rec_w}x{rec_h} px.')
            messagebox.showinfo('Khuyến nghị ảnh từ MapInfo',
                f'Ảnh hiện tại: {w} × {h} px, khoảng {rx:.3f} × {ry:.3f} m/px.\n'
                f'Zoom {z} tại vĩ độ {lat}°: {target:.3f} m/px.\n'
                f'Đề xuất: >= {req_dpi} DPI, kích thước >= {rec_w} × {rec_h} px.\n\n'
                'ƯỚC LƯỢNG: giả định CRS nguồn sử dụng mét và biến dạng phép chiếu nhỏ; '
                'đối chiếu CRS và chất lượng font/ký hiệu thực tế. '
                'Nếu ảnh đã đủ chi tiết thì không phải render lại.')
        except Exception as e:messagebox.showerror('Tính DPI',str(e))
    def _tools(self):return {n:find_tool(n,self.gdal.get()) for n in GDAL_BIN_NAMES}
    def _open_output_folder(self):
        try:
            path=Path(self.out.get()).expanduser().parent
            if not path.exists():
                messagebox.showwarning('Thư mục đầu ra',f'Thư mục chưa tồn tại: {path}')
                return
            if os.name == 'nt':
                os.startfile(str(path))
            elif sys.platform == 'darwin':
                subprocess.Popen(['open',str(path)])
            else:
                subprocess.Popen(['xdg-open',str(path)])
        except Exception as e:
            messagebox.showerror('Không mở được thư mục',str(e))
    def _check(self):
        for n,p in self._tools().items():self._msg(('[OK] ' if p else '[THIẾU] ')+n+(' → '+p if p else ''))
        if any(x is None for x in self._tools().values()):self._msg('GDAL: https://download.osgeo.org/osgeo4w/v2/osgeo4w-setup.exe')
    def _start(self):
        if self.provisional and not self.oneclick:
            if not messagebox.askyesno('TAB tự dựng - cần xác nhận', 'TAB định vị này được tính từ MapperInfo, chưa phải TAB do MapInfo tự đăng ký. Anh chỉ nên dùng thử sau khi kiểm tra chồng khớp trong QGIS. Tiếp tục tạo tệp thử nghiệm?'):
                return

        if self.active:return
        try:
            if not Path(self.src.get()).is_file():raise ValueError('Thiếu PNG nguồn')
            if not Path(self.tab.get()).is_file():raise ValueError('Chưa chọn TAB gốc để lấy đúng hệ tọa độ')
            if self.geo_mode.get()=='auto':
                self.points=read_raster_tab(self.tab.get(),self.src.get())
                self._update_points()
            if self.geo_mode.get()=='auto':
                from PIL import Image
                with Image.open(self.src.get()) as actual_img:
                    iw,ih=actual_img.size
                if any(px < -0.5 or px > iw+0.5 or py < -0.5 or py > ih+0.5 for px,py,_,_ in self.points):
                    raise ValueError(f'Ảnh hiện tại {iw}x{ih} pixel nhưng TAB chứa điểm đăng ký nằm ngoài ảnh. '
                                     'Hãy chọn đúng cặp PNG + TAB từ cùng một lần Save Window As.')
            if len(self.points)<3:raise ValueError('Cần ít nhất 3 giao điểm lưới không thẳng hàng')
            a,b,c=self.points[:3];area=abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))
            if area<1:raise ValueError('3 điểm đầu gần thẳng hàng; hãy chọn các góc lưới cách xa nhau')
            lo=int(self.zmin.get());hi=int(self.zmax.get())
            if not (0<=lo<=hi<=22):raise ValueError('Zoom không hợp lệ')
            fmt=self.fmt.get();out=Path(self.out.get());out.parent.mkdir(parents=True,exist_ok=True)
            if out.resolve()==Path(self.src.get()).resolve():raise ValueError('Đầu ra trùng đầu vào')
            found=self._tools();missing=[n for n,v in found.items() if not v]
            if missing:raise ValueError('Thiếu GDAL: '+', '.join(missing))
            if out.exists() and not messagebox.askyesno('Ghi đè','MBTiles đã tồn tại. Có ghi đè?'):return
            self.active=True;self.go.config(state='disabled');self.progress.start(14);self._msg('[BẮT ĐẦU] Đang tạo MBTiles. Nhật ký GDAL xuất hiện bên dưới.')
            crop_box = self._crop_box() if self.crop_enabled.get() else None
            if crop_box and crop_box == (0,0,*self.img.size):
                raise ValueError('Vùng cắt đang bằng toàn ảnh: chưa cắt gì cả. Hãy bấm Phát hiện hoặc nhập L/T/R/B.')
            if crop_box:
                # No silent changes: user explicitly enabled this ROI.
                self._msg(f'[CẮT] Đã chọn vùng {crop_box} trong ảnh gốc.')
            args=(found,Path(self.src.get()),Path(self.tab.get()),out,list(self.points),lo,hi,fmt,crop_box)
            threading.Thread(target=self._worker,args=(args,),daemon=True).start()
        except Exception as e:messagebox.showerror('Không thể xuất',str(e))
    def _worker(self,args):
        try:self._build(*args);self.events.put(('done','Xuất MBTiles và kiểm tra SQLite thành công'))
        except Exception as e:self.events.put(('error',str(e)))
    def _build(self,tools,src,tab,out,pts,lo,hi,fmt,crop_box=None):
        log=lambda s:self.events.put(('log',s))
        env=os.environ.copy();binpath=str(Path(tools['gdal_translate']).parent)
        env['PATH']=binpath+os.pathsep+env.get('PATH','')
        # Avoid inheriting QGIS-specific PROJ_LIB pointing at a different installation.
        with tempfile.TemporaryDirectory(prefix='mi_mbtiles_') as td:
            td=Path(td);wkt=td/'input_crs.wkt';gcps=td/'page_gcps.vrt';warp=td/'page_3857.tif';tempmb=td/'out.mbtiles'
            original_src=src
            if crop_box is not None:
                with Image.open(src) as im:
                    crop_box=validate_box(crop_box,im.size)
                    cropped=im.crop(crop_box)
                    src=td/'cropped_page.png'
                    cropped.save(src,format='PNG')
                pts=cropped_points(pts,crop_box)
                log(f'[CẮT] Đã cắt trên bản sao: {crop_box}, kích thước {cropped.size}. GCP pixel đã dịch gốc.')
            # Convert raster TAB CoordSys to a tiny valid MapInfo MIF dataset, then ask OGR for WKT.
            crs_mif=td/'source_crs.mif'
            mapinfo_srs=make_crs_mif_from_raster_tab(tab, crs_mif)
            log('MapInfo CoordSys: '+mapinfo_srs)
            cmd=[tools['gdalsrsinfo'],'-o','wkt',str(crs_mif)]
            log('$ '+subprocess.list2cmdline(cmd))
            res=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,errors='replace',env=env)
            if res.returncode or not res.stdout.strip() or ('ERROR' in res.stderr.upper()):
                raise RuntimeError('Không chuyển được MapInfo CoordSys thành WKT bằng GDAL. '+res.stderr[:500])
            srs=res.stdout.strip();wkt.write_text(srs,encoding='utf-8');log('CRS từ MapInfo TAB: '+srs[:220].replace('\n',' '))
            cmd=[tools['gdal_translate'],'-of','VRT','-a_srs',str(wkt)]
            for px,py,x,y in pts:cmd+=['-gcp',str(px),str(py),str(x),str(y)]
            cmd += [str(src),str(gcps)];run_cmd(cmd,log,env)
            # GDAL MBTiles driver does NOT support ZOOM_LEVEL as a creation option.
            # Make the EPSG:3857 raster resolution equal to the desired MAX zoom.
            # Web Mercator resolution (m/pixel) at equator for 256px tiles.
            pixel_size = 2 * math.pi * 6378137.0 / (256 * (2 ** hi))
            log(f'Zoom cao nhất: {hi} | độ phân giải EPSG:3857: {pixel_size:.10f} m/pixel')
            run_cmd([tools['gdalwarp'],'-overwrite','-of','GTiff','-s_srs',str(wkt),'-t_srs','EPSG:3857',
                     '-order','1','-r','bilinear','-dstalpha','-multi','-wo','NUM_THREADS=ALL_CPUS',
                     '-tr',f'{pixel_size:.12f}',f'{pixel_size:.12f}',
                     '-co','TILED=YES','-co','COMPRESS=LZW',
                     str(gcps),str(warp)],log,env)
            # Let GDAL MBTiles infer its native zoom from that pixel resolution.
            # Overviews 2,4,8,... yield hi-1,hi-2,...,lo.
            run_cmd([tools['gdal_translate'],'-of','MBTILES','-co','TILE_FORMAT='+fmt,
                     str(warp),str(tempmb)],log,env)
            powers=[2**i for i in range(1,hi-lo+1)]
            if powers:
                run_cmd([tools['gdaladdo'],'-r','average',str(tempmb),*[str(v) for v in powers]],log,env)
            supplement_missing_zooms(tools,gcps,wkt,tempmb,td,lo,hi,fmt,log,env)
            detail=check_mbtiles(tempmb,lo,hi)
            report=out.with_suffix('.report.json')
            report_data={'image':str(original_src),'crop_box':list(crop_box) if crop_box is not None else None,'reference_tab':str(tab),'control_points':[list(p) for p in pts],
                         'requested_minzoom':lo,'requested_maxzoom':hi,'format':fmt,'source_dpi_user':self.dpi_src.get(), 'georeference_mode':self.geo_mode.get(),**detail}
            if out.exists():out.unlink()
            shutil.move(str(tempmb),str(out))
            report.write_text(json.dumps(report_data,indent=2,ensure_ascii=False),encoding='utf-8')
            log('Số tile: '+str(detail['tile_counts']));log('Tệp: '+str(out))
    def _cleanup_mi_intermediates(self):
        """Only remove known automatic bridge intermediates after verified success.
        Never remove user-supplied source images or TAB files.
        """
        base = Path(r'C:\Users\Public\Documents')
        allowed = {'GeoMaker_MI_Bridge.png', 'GeoMaker_MI_Bridge.bounds.txt',
                   'GeoMaker_MI_Bridge_PROVISIONAL.TAB'}
        try:
            files = [Path(self.src.get()), Path(self.tab.get()), base / 'GeoMaker_MI_Bridge.bounds.txt']
            if not all(f.parent == base and f.name in allowed for f in files):
                return
            for f in files:
                try:
                    f.unlink(missing_ok=True)
                except OSError as exc:
                    self._msg('[DỌN TỆP TẠM] '+str(exc))
        except Exception as exc:
            self._msg('[DỌN TỆP TẠM] '+str(exc))

    def _drain(self):
        try:
            while True:
                kind,msg=self.events.get_nowait()
                if kind=='log':self._msg(msg)
                else:
                    self.active=False;self.go.config(state='normal');self.progress.stop();self._msg(msg)
                    if kind=='done' and self.oneclick:
                        self._cleanup_mi_intermediates()
                    (messagebox.showinfo if kind=='done' else messagebox.showerror)('GeoMaker MBTiles',msg)
        except queue.Empty:pass
        self.root.after(100,self._drain)
if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--mi-image')
    parser.add_argument('--mi-tab')
    parser.add_argument('--mi-dpi')
    parser.add_argument('--mi-minzoom')
    parser.add_argument('--mi-maxzoom')
    parser.add_argument('--provisional',action='store_true')
    parser.add_argument('--oneclick',action='store_true')
    args=parser.parse_args()
    root=tk.Tk()
    app=GUI(root)
    if args.mi_image:
        app.src.set(args.mi_image)
        app.out.set(str(next_output_name(Path.home()/'Documents')))
    if args.mi_tab: app.tab.set(args.mi_tab)
    if args.mi_dpi: app.dpi_src.set(args.mi_dpi)
    if args.mi_minzoom: app.zmin.set(int(args.mi_minzoom))
    if args.mi_maxzoom: app.zmax.set(int(args.mi_maxzoom))
    app.provisional=args.provisional
    app.oneclick=args.oneclick
    if args.provisional:
        app._msg('[CẢNH BÁO] TAB đang dùng là bản tự dựng từ MapperInfo; phải kiểm tra sai số trong QGIS.')
    if args.mi_image: root.after(400,app._open)
    if args.mi_tab: root.after(550,app._read_auto)
    if args.oneclick: root.after(900,app._confirm_oneclick)
    root.mainloop()
