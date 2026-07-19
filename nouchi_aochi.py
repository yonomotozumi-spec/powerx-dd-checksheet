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
import os, glob, zipfile, shutil, urllib.request

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


def ensure_a12(pref_cd, cachedir, download=True):
    """A12を都道府県単位でDL・展開し、.shpパス一覧を返す。
    download=False のときはダウンロードせず、未キャッシュなら DataNotReady を送出する。"""
    from shp_fast import shp_intact, DataNotReady
    p = _p2(pref_cd)
    d = os.path.join(cachedir, "a12", p)
    os.makedirs(d, exist_ok=True)
    shps = glob.glob(os.path.join(d, "**", "*.shp"), recursive=True)
    if shps and all(shp_intact(s) for s in shps):
        return shps
    if shps:  # 切れた（破損）キャッシュ → 掃除して取り直す
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    if not download:
        raise DataNotReady("A12 未キャッシュ: " + p)
    zpath = os.path.join(d, f"A12-15_{p}.zip")
    urllib.request.urlretrieve(A12_URL.format(p=p), zpath)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(d)
    return glob.glob(os.path.join(d, "**", "*.shp"), recursive=True)


def judge_aochi(lat, lon, pref_cd, cachedir, cache_only=False):
    """戻り値: (value, comment)。cache_only=True のときはダウンロードせず、
    未キャッシュなら DataNotReady を送出する。"""
    from shp_fast import load_index, point_hits  # bboxインデックス＋候補のみ厳密判定（全件走査を回避）
    paths = ensure_a12(pref_cd, cachedir, download=not cache_only)
    hit = set(); muni = ""
    pref_shps = [p for p in paths if os.path.basename(p)[:-4][-2:] in ("05", "06")]
    if pref_shps:
        paths = pref_shps
    for sp in paths:
        idx = load_index(sp, ("layer_no", "ctv_name"))
        for (ln, cn) in point_hits(sp, idx, lon, lat):
            if ln is not None:
                try: hit.add(int(ln))
                except Exception: pass
            if cn and not muni:
                muni = str(cn)
    base_c = "国土数値情報A12(2015)より1次判定。種別(1/2/3種)・最終確定は市町村農政課/農業委員会へ照会"
    if 6 in hit:
        return ("青地（農用地区域内）" + (f"／{muni}" if muni else ""), base_c)
    if 5 in hit:
        return ("白地（農業地域内・農用地区域外）" + (f"／{muni}" if muni else ""), base_c)
    return ("農業地域外（A12非該当）", base_c)
