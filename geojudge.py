#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同梱データ(点内判定)モジュール。
実行時に国土数値情報(nlftp)へは一切アクセスしない。リポジトリに同梱した
簡略化済み・gzip圧縮の県別GeoJSON（data/a12/NN.geojson.gz, data/a13/NN.geojson.gz）を
読み込み、点内判定するだけ。

Renderの無料プランは永続ディスク非対応(=毎回キャッシュ消失)のため、実行時DL方式は
コールドスタートのたびに重いDL+解析をやり直してワーカーが落ちる。データを同梱すれば
デプロイに常に含まれるため、初回から高速・低メモリ・障害要因ゼロになる。

同梱GeoJSONのプロパティ:
  A13(森林): {"k": "保安林"|"国有林"|"地域森林計画対象民有林"|"保安施設地区"}
             （ブートストラップの保安林のみ版は {"hoanrinKind":"保安林"} も可）
  A12(農地): {"k": "青地"|"農業地域", "m": "市町村名"}
"""
import gzip, json, os

BASEDIR = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASEDIR, "data")

# 直近の県データをプロセス内に少数だけ保持（512MB枠を圧迫しないよう上限を設ける）
_CACHE = {}
_ORDER = []
_CACHE_MAX = 3  # 512MB枠に合わせ控えめに（大県の同時保持を避ける）


def _norm_area(area):
    s = "".join(ch for ch in str(area or "") if ch.isdigit())
    if not s:
        return None
    return s.zfill(2)[:2]


def _pip_ring(x, y, ring):
    inside = False
    n = len(ring); j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _pip_poly(x, y, poly):
    if not poly or not _pip_ring(x, y, poly[0]):
        return False
    return not any(_pip_ring(x, y, h) for h in poly[1:])  # 穴（ドーナツ）の中は非該当


def _pip_geom(x, y, geom):
    t = geom.get("type"); c = geom.get("coordinates")
    if t == "Polygon":
        return _pip_poly(x, y, c)
    if t == "MultiPolygon":
        return any(_pip_poly(x, y, p) for p in c)
    return False


def _bbox(geom):
    minx = miny = float("inf"); maxx = maxy = float("-inf")
    stack = [geom.get("coordinates")]
    while stack:
        a = stack.pop()
        if a and isinstance(a[0], (int, float)):
            if a[0] < minx: minx = a[0]
            if a[0] > maxx: maxx = a[0]
            if a[1] < miny: miny = a[1]
            if a[1] > maxy: maxy = a[1]
        else:
            for b in a:
                stack.append(b)
    if minx == float("inf"):
        return None
    return (minx, miny, maxx, maxy)


def _load(kind, area):
    """(bbox, geom, props) のリストを返す。データ未整備(ファイル無し/壊れ)なら None。"""
    key = (kind, area)
    if key in _CACHE:
        return _CACHE[key]
    path = os.path.join(DATA, kind, area + ".geojson.gz")
    feats = None
    if os.path.exists(path):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                fc = json.load(f)
            feats = []
            for ft in fc.get("features", []):
                g = ft.get("geometry")
                if not g:
                    continue
                feats.append((_bbox(g), g, ft.get("properties") or {}))
        except Exception:
            feats = None
    _CACHE[key] = feats
    _ORDER.append(key)
    if len(_ORDER) > _CACHE_MAX:
        _CACHE.pop(_ORDER.pop(0), None)
    return feats


def _hits(kind, area, lat, lon):
    feats = _load(kind, area)
    if feats is None:
        return None  # 未整備
    out = []
    for bb, g, p in feats:
        if bb and not (bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3]):
            continue
        if _pip_geom(lon, lat, g):
            out.append(p)
    return out


# ───────────────────────── 森林 / 保安林（A13） ─────────────────────────
def judge_hoanrin(lat, lon, area):
    """戻り値: (value, comment, kinds)。データ未整備なら (None, comment, None)。"""
    base_c = ("国土数値情報A13(森林地域)より1次判定（収録: 保安林/保安施設地区/地域森林計画対象民有林。"
              "国有林は非収録）。保安林内は立木伐採・土地形質変更に許可、開発には解除が必要な場合あり。"
              "指定範囲・可否は都道府県森林部局へ照会（参考精度）")
    a = _norm_area(area)
    hits = _hits("a13", a, lat, lon) if a else None
    if hits is None:
        return (None, base_c, None)
    names = set()
    for p in hits:
        k = p.get("k") or p.get("hoanrinKind")
        if k:
            names.add(str(k))
    is_hoanrin = ("保安林" in names) or ("保安施設地区" in names)
    if is_hoanrin:
        others = "／".join(n for n in ("地域森林計画対象民有林", "国有林", "森林地域") if n in names)
        val = "⚠ 保安林に該当（開発制限あり：森林法の許可・解除の対象）" + (f"［{others}］" if others else "")
    elif names:
        order = ["地域森林計画対象民有林", "国有林", "森林地域"]
        labels = [n for n in order if n in names] + [n for n in sorted(names) if n not in order]
        val = "森林地域内・保安林ではない（" + "／".join(labels) + "）"
    else:
        val = "非該当（森林地域外）"
    return (val, base_c, names)


# ───────────────────────── 農地 青地/白地（A12） ─────────────────────────
def judge_aochi(lat, lon, area):
    """戻り値: (value, comment)。データ未整備なら (None, comment)。"""
    base_c = "国土数値情報A12(農業地域)より1次判定。種別(1/2/3種)・最終確定は市町村農政課/農業委員会へ照会"
    a = _norm_area(area)
    hits = _hits("a12", a, lat, lon) if a else None
    if hits is None:
        return (None, base_c)
    kinds = set(); muni = ""
    for p in hits:
        k = p.get("k") or p.get("layer_no")
        kinds.add(str(k))
        if not muni:
            muni = str(p.get("m") or p.get("ctv_name") or "")
    if len(muni) > 16:  # 一部事務組合等でCTV_NAMEが全市町村連結になる場合に短縮
        muni = muni[:16] + "…"
    suffix = (f"／{muni}" if muni else "")
    if ("青地" in kinds) or ("6" in kinds) or ("06" in kinds):
        return ("青地（農用地区域内）" + suffix, base_c)
    if ("農業地域" in kinds) or ("5" in kinds) or ("05" in kinds):
        return ("白地（農業地域内・農用地区域外）" + suffix, base_c)
    return ("農業地域外（A12非該当）", base_c)
