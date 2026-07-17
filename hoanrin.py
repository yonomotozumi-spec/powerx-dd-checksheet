#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
森林地域／保安林を 国土数値情報A13「森林地域データ」で1次判定する。
- 都道府県単位のA13(2015)をダウンロード＆キャッシュ → 点内判定
- A13_001（森林地域種別コード）: 1=国有林 / 2=地域森林計画対象民有林 / 3=保安林 / 4=保安施設地区
判定: 点が含まれる森林地域区分をすべて拾い、保安林(3)・保安施設地区(4)を優先表示する。
※A13は2015年版・参考表示で精度保証なし。保安林の指定範囲・解除可否は都道府県森林部局へ照会。
依存: pyshp (import shapefile)
"""
import os, glob, zipfile, urllib.request

A13_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/A13/A13-15/A13-15_{p}_GML.zip"

# 森林地域種別コード → 名称（A13_001 の区分）
KIND = {1: "国有林", 2: "地域森林計画対象民有林", 3: "保安林", 4: "保安施設地区"}


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


def ensure_a13(pref_cd, cachedir):
    """A13を都道府県単位でDL・展開し、.shpパス一覧を返す。"""
    p = _p2(pref_cd)
    d = os.path.join(cachedir, "a13", p)
    os.makedirs(d, exist_ok=True)
    shps = glob.glob(os.path.join(d, "**", "*.shp"), recursive=True)
    if not shps:
        zpath = os.path.join(d, f"A13-15_{p}.zip")
        urllib.request.urlretrieve(A13_URL.format(p=p), zpath)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(d)
        shps = glob.glob(os.path.join(d, "**", "*.shp"), recursive=True)
    return shps


def judge_hoanrin(lat, lon, pref_cd, cachedir):
    """戻り値: (value, comment, kinds)。kindsは点が含まれる森林地域種別コードのset。失敗時は例外。"""
    import shapefile  # pyshp
    paths = ensure_a13(pref_cd, cachedir)
    hit = set()
    for sp in paths:
        r = shapefile.Reader(sp, encoding="cp932", encodingErrors="replace")
        flds = [f[0].lower() for f in r.fields[1:]]
        idx = None
        for cand in ("a13_001", "layer_no"):
            if cand in flds:
                idx = flds.index(cand); break
        if idx is None:
            continue
        for sr in r.iterShapeRecords():
            if _shape_contains(lon, lat, sr.shape):
                try: hit.add(int(sr.record[idx]))
                except Exception: pass
    base_c = ("国土数値情報A13(森林地域)より1次判定。保安林の指定範囲・解除可否・"
              "作業許可は都道府県森林部局へ照会（A13は2015年版・参考精度）")
    if not hit:
        return ("該当なし（森林地域外・A13）", base_c, hit)
    # 保安林(3)・保安施設地区(4)を優先し、次に民有林(2)・国有林(1)を並べる
    labels = [KIND[c] for c in (3, 4, 2, 1) if c in hit]
    labels += [KIND.get(c, f"区分{c}") for c in sorted(hit) if c not in (1, 2, 3, 4)]
    return ("／".join(labels), base_c, hit)
