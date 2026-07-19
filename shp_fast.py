#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shapefile点内判定の高速化ヘルパ（A12農地・A13森林で共用）。

従来は生成のたびに県内の全ポリゴン（数千件×大量頂点）を解析していた。
本モジュールは「各レコードのbbox＋必要属性」だけの軽量インデックスを初回に作って
.bidx としてpickleキャッシュし、2回目以降は
  1) インデックス読込（軽量）
  2) 点をbboxに含む候補レコードの絞り込み（通常は数件）
  3) 候補だけ .shp から個別に読み出して厳密判定
とすることで、毎回のフル走査を排除する。
"""
import os, pickle


def _pip_ring(lon, lat, ring):
    inside = False; n = len(ring); j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]; xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def shape_contains(lon, lat, shape):
    bb = getattr(shape, "bbox", None)
    if bb and len(bb) == 4 and (lon < bb[0] or lon > bb[2] or lat < bb[1] or lat > bb[3]):
        return False
    pts = shape.points
    if not pts:
        return False
    parts = list(shape.parts) + [len(pts)]
    inside = False
    for i in range(len(shape.parts)):
        ring = pts[parts[i]:parts[i + 1]]
        if len(ring) >= 3 and _pip_ring(lon, lat, ring):
            inside = not inside
    return inside


def load_index(sp_path, fields):
    """bbox＋指定属性（小文字名、無い属性はNone）のインデックスを返す。.bidxにキャッシュ。"""
    import shapefile
    pkl = sp_path + ".bidx"
    try:
        if os.path.getmtime(pkl) >= os.path.getmtime(sp_path):
            with open(pkl, "rb") as f:
                idx = pickle.load(f)
            if idx.get("fields") == list(fields):
                return idx
    except (OSError, pickle.PickleError, EOFError):
        pass
    r = shapefile.Reader(sp_path, encoding="cp932", encodingErrors="replace")
    flds = [f[0].lower() for f in r.fields[1:]]
    pos = [flds.index(f) if f in flds else None for f in fields]
    rows = []
    for sr in r.iterShapeRecords():
        bb = getattr(sr.shape, "bbox", None)
        bb = tuple(bb) if bb and len(bb) == 4 else None
        vals = tuple((sr.record[p] if p is not None else None) for p in pos)
        rows.append((bb, vals))
    idx = {"fields": list(fields), "rows": rows}
    try:
        tmp = pkl + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(idx, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, pkl)
    except OSError:
        pass  # キャッシュ不可でも判定は続行
    return idx


def point_hits(sp_path, idx, lon, lat):
    """点を含むレコードの属性値タプル一覧。bbox候補のみ.shpから個別読み出しして厳密判定。"""
    cand = [i for i, (bb, _v) in enumerate(idx["rows"])
            if bb and bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3]]
    if not cand:
        return []
    import shapefile
    r = shapefile.Reader(sp_path, encoding="cp932", encodingErrors="replace")
    out = []
    for i in cand:
        if shape_contains(lon, lat, r.shape(i)):
            out.append(idx["rows"][i][1])
    return out
