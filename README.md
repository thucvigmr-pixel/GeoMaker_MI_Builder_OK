GEOMAKER MBTILES CREATE - VERSION 1.2.5
Base: MI_MBtiles_Create.py

Changes in MI_MBtiles_Create.py only:
- Speed selector: Nhanh / Can bang / Uu tien dung luong.
- Nhanh: GDAL cache 512 MB, 4 threads, temporary GeoTIFF without LZW compression, PNG ZLEVEL=1.
- Can bang (default): temporary GeoTIFF LZW, PNG ZLEVEL=6, cache 256 MB.
- Uu tien dung luong: PNG ZLEVEL=9, other operations unchanged.
- Timing logs for VRT, warp, MBTiles creation, overview, missing zoom completion and verification.
- Keep existing missing-zoom fallback, reprojection, crop/georeference, metadata and SQLite validation.

Installation:
1. Backup working folder outside C:\GeoMaker_MI_Builder.
2. Unzip to a separate trial folder first or replace only MI_MBtiles_Create.py in a copied installation.
3. Retain currently compiled MapBasic MBX; source MB not changed.
4. Try same map with Nhanh and Can bang, compare report tile_counts, bounds, quality and timings.
5. Free disk space matters: Nhanh creates large uncompressed temporary GeoTIFFs.

Validation: Python syntax check passed. No live GDAL/QGIS Windows benchmark performed.
