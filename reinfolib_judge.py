#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
不動産情報ライブラリ(reinfolib)API の GeoJSON から、座標の「該当/非該当」と詳細を判定する。
ネットワークはしない。GeoJSON取得は呼び出し側(app.py)が行い、本スクリプトは
(1)取得URL生成 と (2)点内判定→値・許認可の組み立て を担う。

使い方:
  python reinfolib_judge.py urls  --lat L --lon N --key <APIKEY>
  python reinfolib_judge.py judge --lat L --lon N --dir <reinfolibdir> --out values.json
出力 values.json:
  {"values": {DD項目No: {"value","comment"}}, "permits": {法令No: {"req","note"}}, "raw": {...}}
"""
import argparse, json, math, os

BASE = "https://www.reinfolib.mlit.go.jp/ex-api/external/{code}?response_format=geojson&z={z}&x={x}&y={y}"

# code -> (zoom, 説明)
LAYERS = {
    "XKT001": (14, "都市計画区域/区域区分"),
    "XKT002": (14, "用途地域"),
    "XKT025": (14, "液状化の発生傾向"),
    "XKT026": (15, "洪水浸水想定区域(想定最大)"),
    "XKT027": (15, "高潮浸水想定区域"),
    "XKT028": (15, "津波浸水想定"),
    "XKT029": (14, "土砂災害警戒区域"),
    "XKT019": (14, "自然公園地域"),
    "XKT021": (14, "地すべり防止地区"),
    "XKT022": (14, "急傾斜地崩壊危険区域"),
    "XKT016": (14, "災害危険区域"),
    "XKT020": (14, "大規模盛土造成地"),
}

FLOOD_DEPTH = {1: "0.5m未満", 2: "0.5〜3m", 3: "3〜5m", 4: "5〜10m", 5: "10〜20m", 6: "20m〜"}
SEDIMENT_PHENOM = {1: "急傾斜地の崩壊", 2: "土石流", 3: "地すべり"}
SEDIMENT_KUBUN = {1: "土砂災害警戒区域", 2: "土砂災害特別警戒区域"}


def deg2num(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def pip_ring(lon, lat, ring):
    inside = False; n = len(ring); j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]; xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def pip_polygon(lon, lat, poly):
    if not poly or not pip_ring(lon, lat, poly[0]):
        return False
    return not any(pip_ring(lon, lat, h) for h in poly[1:])


def feature_contains(lon, lat, geom):
    if not geom:
        return False
    t = geom.get("type"); c = geom.get("coordinates")
    if t == "Polygon":
        return pip_polygon(lon, lat, c)
    if t == "MultiPolygon":
        return any(pip_polygon(lon, lat, p) for p in c)
    return False


def matched(lon, lat, fc):
    return [f.get("properties") or {} for f in (fc.get("features") or [])
            if feature_contains(lon, lat, f.get("geometry"))]


def best_text(props_list):
    if not props_list:
        return ""
    p = props_list[0]; skip = ("code", "id", "date", "number", "year", "size", "leng", "area", "no", "div")
    picks = [v.strip() for k, v in p.items()
             if isinstance(v, str) and v.strip() and not any(s in k.lower() for s in skip)]
    return " / ".join(dict.fromkeys(picks))[:60]


def build_urls(lat, lon, key):
    out = []
    for code, (z, _d) in LAYERS.items():
        x, y = deg2num(lat, lon, z)
        url = BASE.format(code=code, z=z, x=x, y=y)
        if key:
            url += "&subscription-key=" + key
        out.append(f"{code}\t{url}")
    return out


def judge(lat, lon, directory):
    values, permits, raw = {}, {}, {}

    def load(code):
        p = os.path.join(directory, code + ".geojson")
        if not os.path.exists(p):
            return None
        try:
            t = open(p, encoding="utf-8").read().strip()
            return json.loads(t) if t else {"type": "FeatureCollection", "features": []}
        except Exception as e:
            return {"_error": str(e), "features": []}

    fcs = {c: load(c) for c in LAYERS}

    # 用途地域(10)
    if fcs["XKT002"] is not None:
        m = matched(lon, lat, fcs["XKT002"]); raw["XKT002"] = m
        if m:
            p = m[0]; name = p.get("use_area_ja") or "用途地域"
            bits = []
            if p.get("u_building_coverage_ratio_ja"): bits.append("建蔽率" + str(p["u_building_coverage_ratio_ja"]))
            if p.get("u_floor_area_ratio_ja"): bits.append("容積率" + str(p["u_floor_area_ratio_ja"]))
            val = name + (("（" + " ".join(bits) + "）") if bits else "")
        else:
            val = "指定なし"
        values["10"] = {"value": val, "comment": "reinfolib XKT002より自動判定"}

    # 区域区分(8,9)
    if fcs["XKT001"] is not None:
        m = matched(lon, lat, fcs["XKT001"]); raw["XKT001"] = m
        vals = [str(p.get("area_classification_ja") or "") for p in m]
        joined = " / ".join([v for v in vals if v])
        kasenka = any("市街化区域" in v for v in vals)
        chosei = any("市街化調整区域" in v for v in vals)
        cmt = "reinfolib XKT001より自動判定" + (f"（{joined}）" if joined else "（都市計画区域外の可能性）")
        values["8"] = {"value": "指定あり" if kasenka else "指定なし", "comment": cmt}
        values["9"] = {"value": "指定あり" if chosei else "指定なし", "comment": cmt}

    # 浸水(4): 洪水/高潮/津波
    flood = []
    if fcs["XKT026"] is not None:
        m = matched(lon, lat, fcs["XKT026"]); raw["XKT026"] = m
        if m:
            ranks = [p.get("A31a_205") for p in m if isinstance(p.get("A31a_205"), int)]
            rivers = sorted({str(p.get("A31a_202")) for p in m if p.get("A31a_202")})
            depth = FLOOD_DEPTH.get(max(ranks), "") if ranks else ""
            d = []
            if rivers: d.append("・".join(rivers))
            if depth: d.append("浸水深" + depth)
            flood.append("洪水浸水想定区域" + (("（" + "／".join(d) + "）") if d else ""))
    for code, lab in (("XKT027", "高潮浸水想定区域"), ("XKT028", "津波浸水想定")):
        if fcs[code] is not None:
            m = matched(lon, lat, fcs[code]); raw[code] = m
            if m:
                flood.append(lab)
    if any(fcs[c] is not None for c in ("XKT026", "XKT027", "XKT028")):
        values["4"] = {"value": " / ".join(flood) if flood else "該当なし",
                       "comment": "reinfolib 洪水/高潮/津波 浸水想定区域より自動判定"}

    # 土砂(5)
    sed_extra = []
    if fcs["XKT029"] is not None:
        m = matched(lon, lat, fcs["XKT029"]); raw["XKT029"] = m
        if m:
            labels = []
            tokubetsu = False
            for p in m:
                k = SEDIMENT_KUBUN.get(p.get("A33_002"), "土砂災害（警戒区域）")
                if p.get("A33_002") == 2: tokubetsu = True
                ph = SEDIMENT_PHENOM.get(p.get("A33_001"), "")
                labels.append(k + (f"（{ph}）" if ph else ""))
            uniq = sorted(set(labels), key=lambda s: ("特別警戒" not in s, s))
            values["5"] = {"value": " / ".join(uniq), "comment": "reinfolib XKT029より自動判定"}
            permits["16"] = {"req": "要", "note": "土砂災害危険箇所/警戒区域に該当 (reinfolib XKT029)"}
            if tokubetsu:
                permits["15"] = {"req": "要", "note": "土砂災害特別警戒区域に該当 (reinfolib XKT029)"}
        else:
            values["5"] = {"value": "該当なし", "comment": "reinfolib XKT029より自動判定"}

    # 液状化(6)
    if fcs["XKT025"] is not None:
        m = matched(lon, lat, fcs["XKT025"]); raw["XKT025"] = m
        if m:
            p = m[0]; note = p.get("note") or ""; micro = p.get("topographic_classification_name_ja") or ""
            lv = p.get("liquefaction_tendency_level")
            extra = [x for x in [micro, (f"傾向{lv}/6" if isinstance(lv, int) else "")] if x]
            val = (note + ("（" + "・".join(extra) + "）" if extra else "")) or "該当（傾向図あり）"
        else:
            val = "該当なし（対象範囲外）"
        values["6"] = {"value": val, "comment": "reinfolib XKT025（地形区分に基づく液状化発生傾向）より自動判定"}

    # 公園(13) 自然公園
    if fcs["XKT019"] is not None:
        m = matched(lon, lat, fcs["XKT019"]); raw["XKT019"] = m
        names = sorted({str(p.get("OBJ_NAME_ja")) for p in m if p.get("OBJ_NAME_ja")})
        values["13"] = {"value": "・".join(names) if names else ("自然公園地域に該当" if m else "該当なし"),
                        "comment": "reinfolib XKT019（自然公園地域）より自動判定"}

    # 防災補足 → 許認可＋item5コメント
    if fcs["XKT021"] is not None:
        m = matched(lon, lat, fcs["XKT021"]); raw["XKT021"] = m
        if m:
            permits["13"] = {"req": "要", "note": "地すべり防止地区に該当 (reinfolib XKT021)"}
            sed_extra.append("地すべり防止地区に該当")
    if fcs["XKT022"] is not None:
        m = matched(lon, lat, fcs["XKT022"]); raw["XKT022"] = m
        if m:
            permits["14"] = {"req": "要", "note": "急傾斜地崩壊危険区域に該当 (reinfolib XKT022)"}
            sed_extra.append("急傾斜地崩壊危険区域に該当")
    if fcs["XKT016"] is not None:
        m = matched(lon, lat, fcs["XKT016"]); raw["XKT016"] = m
        if m:
            sed_extra.append("災害危険区域に該当（建築制限の可能性）")
    if fcs["XKT020"] is not None:
        m = matched(lon, lat, fcs["XKT020"]); raw["XKT020"] = m
        if m:
            permits["7"] = {"req": "要", "note": "大規模盛土造成地に該当 (reinfolib XKT020)"}
            sed_extra.append("大規模盛土造成地に該当")
    if sed_extra and "5" in values:
        values["5"]["comment"] += " ／ " + "、".join(sed_extra)

    return {"values": values, "permits": permits, "raw": raw,
            "missing": [c for c in LAYERS if fcs[c] is None]}


def main():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    pu = sub.add_parser("urls"); pu.add_argument("--lat", type=float, required=True); pu.add_argument("--lon", type=float, required=True); pu.add_argument("--key", default="")
    pj = sub.add_parser("judge"); pj.add_argument("--lat", type=float, required=True); pj.add_argument("--lon", type=float, required=True); pj.add_argument("--dir", required=True); pj.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.cmd == "urls":
        for ln in build_urls(a.lat, a.lon, a.key): print(ln)
    else:
        res = judge(a.lat, a.lon, a.dir)
        json.dump(res, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("OK:", a.out)
        for k in sorted(res["values"], key=lambda s: int(s)):
            print(f"  DD項目{k}: {res['values'][k]['value']}")
        for k in sorted(res["permits"], key=lambda s: int(s)):
            print(f"  許認可No.{k}: {res['permits'][k]['req']} - {res['permits'][k]['note']}")
        if res["missing"]: print("  未取得:", ", ".join(res["missing"]))


if __name__ == "__main__":
    main()
