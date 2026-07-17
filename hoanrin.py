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

# A13 の森林地域区分コード → 名称。
# 配布形式で属性が2系統あるため、値レンジで一意に解釈できるよう1つの表にまとめる。
#   ・Shapefile/GML版: 属性 layer_no（土地利用基本計画のレイヤー番号）= 7〜10
#       07=森林地域 / 08=国有林 / 09=地域森林計画対象民有林 / 10=保安林
#   ・GeoJSON版: 属性 A13_001（森林地域種別コード）= 1〜4
#       1=国有林 / 2=地域森林計画対象民有林 / 3=保安林 / 4=保安施設地区
# 1〜4 と 7〜10 は重複しないため、どちらの属性を読んでも値だけで区分を確定できる。
CODE = {
    1: "国有林", 2: "地域森林計画対象民有林", 3: "保安林", 4: "保安施設地区",
    7: "森林地域", 8: "国有林", 9: "地域森林計画対象民有林", 10: "保安林",
}


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
    """戻り値: (value, comment, kinds)。kindsは点が含まれる森林地域区分名(str)のset。失敗時は例外。"""
    import shapefile  # pyshp
    paths = ensure_a13(pref_cd, cachedir)
    names = set()
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
                try: code = int(sr.record[idx])
                except Exception: continue
                names.add(CODE.get(code) or f"区分{code}")
    base_c = ("国土数値情報A13(森林地域)より1次判定。保安林内は立木伐採・土地形質変更に許可、"
              "開発には解除が必要な場合あり。指定範囲・可否は都道府県森林部局へ照会（A13は参考精度）")
    is_hoanrin = ("保安林" in names) or ("保安施設地区" in names)
    if is_hoanrin:
        # DDではマイナス材料（制限あり）と一目で分かる文言にする
        others = "／".join(n for n in ("地域森林計画対象民有林", "国有林", "森林地域") if n in names)
        val = "⚠ 保安林に該当（開発制限あり：森林法の許可・解除の対象）" + (f"［{others}］" if others else "")
    elif names:
        order = ["地域森林計画対象民有林", "国有林", "森林地域"]
        labels = [n for n in order if n in names] + [n for n in sorted(names) if n not in order]
        val = "森林地域内・保安林ではない（" + "／".join(labels) + "）"
    else:
        val = "非該当（森林地域外）"
    return (val, base_c, names)
