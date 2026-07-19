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
import os, pickle, struct


class DataNotReady(Exception):
    """当該都道府県のデータが未キャッシュ（＝要バックグラウンド取得）を表す。
    リクエスト内で重いダウンロードを走らせないための合図。"""
    pass


def _iter_shp_bboxes(sp_path):
    """.shx/.shp のヘッダだけを読み、各レコードのbbox(minx,miny,maxx,maxy)を順に返す。
    ポリゴンの全頂点(数百万点)を読まないので、大容量県でも軽量・高速。
    面/線/多点型のみbboxを持つ。点/ヌル型・読み取り不能はNone。"""
    shx_path = sp_path[:-4] + ".shx"
    with open(shx_path, "rb") as fx:
        fx.seek(24)
        file_len = struct.unpack(">i", fx.read(4))[0] * 2  # バイト長
        fx.seek(100)
        offsets = []
        n = max(0, (file_len - 100) // 8)
        for _ in range(n):
            rec = fx.read(8)
            if len(rec) < 8:
                break
            off_w, _clen = struct.unpack(">ii", rec)
            offsets.append(off_w * 2)  # 16bitワード→バイト
    with open(sp_path, "rb") as fp:
        for off in offsets:
            fp.seek(off + 8)  # 8バイトのレコードヘッダを飛ばす
            stb = fp.read(4)
            if len(stb) < 4:
                yield None; continue
            st = struct.unpack("<i", stb)[0]
            # bboxを持つ型: PolyLine/Polygon/MultiPoint と そのZ/M変種
            if st in (3, 5, 8, 13, 15, 18, 23, 25, 28, 31):
                box = fp.read(32)
                if len(box) < 32:
                    yield None; continue
                yield struct.unpack("<4d", box)
            else:
                yield None


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

    # 省メモリ経路: .shp/.shx のヘッダからbboxだけ、属性は.dbfのみ読む（頂点を読まない）。
    rows = None
    try:
        bboxes = list(_iter_shp_bboxes(sp_path))
        recs = r.records()  # .dbf のみ（geometry非読込）
        if len(bboxes) == len(recs) and not (bboxes and all(b is None for b in bboxes)):
            rows = [(tuple(bboxes[i]) if bboxes[i] is not None else None,
                     tuple((recs[i][p] if p is not None else None) for p in pos))
                    for i in range(len(recs))]
    except Exception:
        rows = None
    if rows is None:
        # フォールバック: 従来どおり形状ごと読む（確実だが重い）
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


def shp_intact(sp_path):
    """.shp のヘッダ宣言長(バイト)と実ファイルサイズを比較し、切れていないか大まかに検証。
    ダウンロードが途中で切れた等の破損キャッシュを検出するために使う。"""
    try:
        with open(sp_path, "rb") as f:
            f.seek(24)
            declared = struct.unpack(">i", f.read(4))[0] * 2  # 16bitワード→バイト
        return declared >= 100 and os.path.getsize(sp_path) >= declared
    except Exception:
        return False


def point_hits(sp_path, idx, lon, lat):
    """点を含むレコードの属性値タプル一覧。bbox候補のみ.shpから個別読み出しして厳密判定。
    破損レコード(KSJの一部地域に既知の不良あり／切れたファイル)は個別にスキップし、
    判定全体は止めない（本判定は1次・参考のため、読める地物で継続する）。"""
    cand = [i for i, (bb, _v) in enumerate(idx["rows"])
            if bb and bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3]]
    if not cand:
        return []
    import shapefile
    r = shapefile.Reader(sp_path, encoding="cp932", encodingErrors="replace")
    out = []
    for i in cand:
        try:
            sh = r.shape(i)
        except Exception:
            continue  # このレコードだけ読めない → スキップして継続
        if shape_contains(lon, lat, sh):
            out.append(idx["rows"][i][1])
    return out
