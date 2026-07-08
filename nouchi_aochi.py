#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
農地の青地/白地（農振法 農用地区域の内外）を 国土数値情報A12「農業地域データ」で1次判定する。
- 都道府県単位のA12(2015)をダウンロード＆キャッシュ → 点内判定
- layer_no: 05=農業地域, 06=農用地区域（青地）
判定: 点が06内→青地 / 05内のみ→白地 / どれにも入らない→農業地域外
※A12は2015年版・参考表示で精度保証なし。種別(1/2/3種)・確定は市町村農政課/農業委員会照会。
依存: pyshp (import shapefile)
"""
import os, glob, zipfile, urllib.request

A12_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/A12/A12-15/A12-15_{p}_GML.zip"


def _p2(pref_cd):
    return str(pref_cd).zfill(2)


def _pip_ring(lon, lat, ring):
    inside = False; n = len(ring); j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]; xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _shape_contains(lon, lat, shape):
    # バウンディングボックスで事前判定（点がbbox外のポリゴンは重い内外判定を省略）
    bb = getattr(shape, "bbox", None)
    if bb and len(bb) == 4 and (lon < bb[0] or lon > bb[2] or lat < bb[1] or lat > bb[3]):
        return False
    pts = shape.points
    if not pts: return False
    parts = list(shape.parts) + [len(pts)]
    inside = False
    for i in range(len(shape.parts)):
        ring = pts[parts[i]:parts[i+1]]
        if len(ring) >= 3 and _pip_ring(lon, lat, ring):
            inside = not inside
    return inside


def _pip_poly(lon, lat, poly):
    if not poly or not _pip_ring(lon, lat, poly[0]):
        return False
    return not any(_pip_ring(lon, lat, h) for h in poly[1:])


def _contains(lon, lat, geo):
    t = geo.get("type"); c = geo.get("coordinates")
    if t == "Polygon":
        return _pip_poly(lon, lat, c)
    if t == "MultiPolygon":
        return any(_pip_poly(lon, lat, p) for p in c)
    return False


def ensure_a12(pref_cd, cachedir):
    """A12を都道府県単位でDL・展開し、.shpパス一覧を返す。"""
    p = _p2(pref_cd)
    d = os.path.join(cachedir, "a12", p)
    os.makedirs(d, exist_ok=True)
    shps = glob.glob(os.path.join(d, "**", "*.shp"), recursive=True)
    if not shps:
        zpath = os.path.join(d, f"A12-15_{p}.zip")
        urllib.request.urlretrieve(A12_URL.format(p=p), zpath)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(d)
        shps = glob.glob(os.path.join(d, "**", "*.shp"), recursive=True)
    return shps


def judge_aochi(lat, lon, pref_cd, cachedir):
    """戻り値: (value, comment)。失敗時は例外。"""
    import shapefile  # pyshp
    paths = ensure_a12(pref_cd, cachedir)
    hit = set(); muni = ""
    pref_shps = [p for p in paths if os.path.basename(p)[:-4][-2:] in ("05", "06")]
    if pref_shps:
        paths = pref_shps
    for sp in paths:
        r = shapefile.Reader(sp, encoding="cp932", encodingErrors="replace")
        flds = [f[0].lower() for f in r.fields[1:]]
        li = flds.index("layer_no") if "layer_no" in flds else None
        ci = flds.index("ctv_name") if "ctv_name" in flds else None
        for sr in r.iterShapeRecords():
            if _shape_contains(lon, lat, sr.shape):
                if li is not None:
                    try: hit.add(int(sr.record[li]))
                    except Exception: pass
                if ci is not None and not muni:
                    muni = str(sr.record[ci] or "")
    base_c = "国土数値情報A12(2015)より1次判定。種別(1/2/3種)・最終確定は市町村農政課/農業委員会へ照会"
    if 6 in hit:
        return ("青地（農用地区域内）" + (f"／{muni}" if muni else ""), base_c)
    if 5 in hit:
        return ("白地（農業地域内・農用地区域外）" + (f"／{muni}" if muni else ""), base_c)
    return ("農業地域外（A12非該当）", base_c)
