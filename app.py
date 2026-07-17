#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PowerX 案件チェックシート 社内Webアプリ（Flask）。
ブラウザのフォームに 緯度経度/住所・PX番号・担当者 を入力すると、
サーバー側で 国土地理院ジオコーディング・reinfolib判定・農地青地/白地(A12)判定 を行い、
3タブ構成の xlsx を生成してダウンロードさせる。

- reinfolib APIキーは**サーバー側の環境変数 REINFOLIB_API_KEY**を使う（社内共有・利用者は入力不要）。
  環境変数が無い場合はフォームのキー欄（任意）を使う。どちらも無ければ自動判定はスキップ。
- 社内限定アクセス：環境変数 APP_USER / APP_PASS を設定すると Basic認証を要求する（未設定なら認証なし）。
- Render 等の Python 対応ホストで `gunicorn app:app` として起動する想定。
"""
import os, io, json, math, time, tempfile, datetime, urllib.parse, urllib.request, subprocess
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, send_file, Response, abort, redirect

import reinfolib_judge

app = Flask(__name__)

BASEDIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(tempfile.gettempdir(), "pxapp_data"))
OUT_DIR = os.path.join(tempfile.gettempdir(), "pxapp_out")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

# 社内共有のreinfolib APIキー（環境変数。コードには書かない）
SERVER_KEY = os.environ.get("REINFOLIB_API_KEY", "").strip()
# 社内限定アクセス用のBasic認証（任意）
APP_USER = os.environ.get("APP_USER", "").strip()
APP_PASS = os.environ.get("APP_PASS", "").strip()


def require_auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if APP_USER and APP_PASS:
            au = request.authorization
            if not au or au.username != APP_USER or au.password != APP_PASS:
                # WWW-Authenticate の realm は ASCII のみ（HTTPヘッダは latin-1）
                return Response("認証が必要です（社内限定）", 401,
                                {"WWW-Authenticate": 'Basic realm="PowerX DD Internal"'})
        return fn(*a, **kw)
    return wrapper

REINFO = "https://www.reinfolib.mlit.go.jp/ex-api/external/{code}?response_format=geojson&z={z}&x={x}&y={y}"
GSI_SEARCH = "https://msearch.gsi.go.jp/address-search/AddressSearch?q={q}"
GSI_REVERSE = "https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress?lat={lat}&lon={lon}"
UA = {"User-Agent": "PowerX-DD-App/1.0"}
TIMEOUT = 30


# ───────────────────────── ネットワーク ─────────────────────────
def _get(url, headers=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def geocode_address(addr):
    """住所→(lat, lon)。GSIジオコーダ（coordinatesは[経度,緯度]）。失敗時 None。"""
    try:
        data = json.loads(_get(GSI_SEARCH.format(q=urllib.parse.quote(addr))))
        if data:
            lon, lat = data[0]["geometry"]["coordinates"][:2]
            return float(lat), float(lon)
    except Exception:
        pass
    return None


def pref_code(lat, lon):
    """緯度経度→都道府県コード(2桁)。GSI逆ジオコーダのmuniCd先頭2桁。失敗時 None。"""
    try:
        data = json.loads(_get(GSI_REVERSE.format(lat=lat, lon=lon)))
        muni = (data.get("results") or {}).get("muniCd")
        if muni:
            return str(muni).zfill(5)[:2]
    except Exception:
        pass
    return None


def fetch_reinfolib(lat, lon, key, dest):
    """reinfolibの各レイヤをヘッダ認証で並列取得し dest/<CODE>.geojson に保存。取得数を返す。"""
    os.makedirs(dest, exist_ok=True)

    def one(item):
        code, (z, _d) = item
        x, y = reinfolib_judge.deg2num(lat, lon, z)
        url = REINFO.format(code=code, z=z, x=x, y=y)
        try:
            body = _get(url, headers={"Ocp-Apim-Subscription-Key": key})
            with open(os.path.join(dest, code + ".geojson"), "w", encoding="utf-8") as f:
                f.write(body)
            return 1
        except Exception:
            return 0

    items = list(reinfolib_judge.LAYERS.items())
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as ex:
        return sum(ex.map(one, items))


# ───────────────────────── 生成 ─────────────────────────
def generate(form):
    lat = form.get("lat", "").strip()
    lon = form.get("lon", "").strip()
    addr = form.get("addr", "").strip()
    pxno = form.get("pxno", "").strip()
    tanto = form.get("tanto", "").strip()
    # サーバー側の社内共有キーを優先。無ければフォームのキー欄（任意）。
    key = SERVER_KEY or form.get("key", "").strip()

    notes = []
    coord_source = "入力座標"

    if lat and lon:
        lat, lon = float(lat), float(lon)
    elif addr:
        got = geocode_address(addr)
        if not got:
            raise ValueError("住所から座標を特定できませんでした。緯度経度を直接入力してください。")
        lat, lon = got
        coord_source = "住所からの代表点（数百m誤差の可能性）"
        notes.append("座標は住所からの代表点です。筆単位の判定（農地・用途地域等）は目視で再確認してください。")
    else:
        raise ValueError("緯度経度、または住所のいずれかを入力してください。")

    if not addr:
        addr = f"緯度{lat} 経度{lon}"

    work = tempfile.mkdtemp(prefix="pxjob_", dir=OUT_DIR)
    values_data = {"values": {}, "permits": {}}

    # reinfolib 自動判定
    if key:
        rdir = os.path.join(work, "reinfolib")
        got = fetch_reinfolib(lat, lon, key, rdir)
        if got:
            res = reinfolib_judge.judge(lat, lon, rdir)
            values_data["values"].update(res.get("values", {}))
            values_data["permits"].update(res.get("permits", {}))
            notes.append(f"reinfolibから{got}レイヤを取得し自動判定しました。")
        else:
            notes.append("reinfolibの取得に失敗しました（APIキー・ネットワークを確認）。自動判定はスキップします。")
    else:
        notes.append("APIキーが未設定のため、reinfolib自動判定はスキップ（各確認リンクは生成されます）。")

    # 都道府県コード（農地A12・森林A13の判定に使用）
    try:
        pc = pref_code(lat, lon)
    except Exception:
        pc = None

    # 農地 青地/白地（A12）
    try:
        if pc:
            from nouchi_aochi import judge_aochi
            val, cmt = judge_aochi(lat, lon, pc, DATA_DIR)
            values_data["values"]["11"] = {"value": val, "comment": cmt}
            notes.append(f"農地(青地/白地)をA12から1次判定：{val}")
    except Exception as e:
        notes.append(f"農地(青地/白地)の自動判定はスキップしました（{type(e).__name__}）。地図で目視確認してください。")

    # 森林地域／保安林（A13）
    try:
        if pc:
            from hoanrin import judge_hoanrin
            val, cmt, kinds = judge_hoanrin(lat, lon, pc, DATA_DIR)
            values_data["values"]["12"] = {"value": val, "comment": cmt}
            # 保安林・保安施設地区→森林法の保安林手続、地域森林計画対象民有林→伐採届出/林地開発許可
            if ("保安林" in kinds) or ("保安施設地区" in kinds):
                values_data["permits"]["34"] = {"req": "要",
                    "note": "保安林に該当 (国土数値情報A13)。立木伐採・土地形質変更には許可、開発には解除が必要な場合あり"}
            if "地域森林計画対象民有林" in kinds:
                values_data["permits"].setdefault("33", {"req": "要",
                    "note": "地域森林計画対象民有林に該当 (A13)。1ha超開発は林地開発許可、伐採は届出"})
            notes.append(f"森林地域/保安林をA13から1次判定：{val}")
    except Exception as e:
        notes.append(f"森林/保安林の自動判定はスキップしました（{type(e).__name__}）。都道府県の森林GISで目視確認してください。")

    # values.json 書き出し
    vpath = os.path.join(work, "values.json")
    with open(vpath, "w", encoding="utf-8") as f:
        json.dump(values_data, f, ensure_ascii=False, indent=2)

    # xlsx 生成
    safe = "".join(c for c in (addr[:20]) if c not in '[]:*?/\\')
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(work, f"PX案件チェックシート_{safe}_{stamp}.xlsx")
    cmd = ["python", os.path.join(BASEDIR, "build_px_checksheet.py"),
           "--address", addr, "--lat", str(lat), "--lon", str(lon),
           "--pxno", pxno, "--tanto", tanto, "--values", vpath, "--out", out,
           "--classic"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("xlsx生成に失敗しました：" + (r.stderr or r.stdout)[-500:])

    return out, coord_source, notes


# ───────────────────────── HTML ─────────────────────────
PAGE = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PowerX 案件チェックシート 生成</title>
<style>
:root{--bg:#f0f3f9;--surf:#fff;--bdr:#cdd5e8;--txt:#16213d;--txt2:#465070;--txt3:#8390ac;--acc:#1b55cc;--acc2:#1444aa;--ok:#0b9966;--okbg:#e8faf3;--warnbg:#fef8e0;--warn:#bf7800}
@media(prefers-color-scheme:dark){:root{--bg:#0b1221;--surf:#161f35;--bdr:#273450;--txt:#e3ebfc;--txt2:#7d91ba;--txt3:#4a5a7a;--acc:#4d82f7;--acc2:#6896f9;--ok:#18bb82;--okbg:rgba(24,187,130,.14);--warnbg:rgba(240,168,0,.14);--warn:#f0a800}}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,'Segoe UI','Hiragino Sans','Yu Gothic UI',Meiryo,sans-serif;font-size:14px;line-height:1.6}
.wrap{max-width:640px;margin:0 auto;padding:28px 16px 72px}
h1{font-size:18px;letter-spacing:-.02em;display:flex;align-items:center;gap:8px;margin-bottom:4px}
.badge{font-size:10px;font-weight:700;background:var(--acc);color:#fff;padding:2px 8px;border-radius:4px}
.sub{font-size:12px;color:var(--txt3);margin-bottom:20px}
.card{background:var(--surf);border:1px solid var(--bdr);border-radius:8px;padding:18px;margin-bottom:14px;box-shadow:0 1px 3px rgba(16,32,80,.06)}
label{display:block;font-size:13px;color:var(--txt2);margin-bottom:4px;margin-top:12px;font-weight:500}
label:first-child{margin-top:0}
.hint{font-size:11px;color:var(--txt3);font-weight:400}
input,select{width:100%;background:var(--bg);border:1px solid var(--bdr);border-radius:6px;color:var(--txt);font:inherit;font-size:13px;padding:8px 10px;outline:none}
input:focus,select:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(27,85,204,.12)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
button{width:100%;margin-top:18px;padding:14px;background:var(--acc);color:#fff;border:none;border-radius:8px;font:inherit;font-size:15px;font-weight:700;cursor:pointer}
button:hover{background:var(--acc2)}
button:disabled{opacity:.6;cursor:progress}
.note{font-size:12px;color:var(--txt2);background:var(--warnbg);border-radius:6px;padding:10px 12px;margin-top:6px}
.ok{background:var(--okbg);color:var(--ok);font-weight:600;border-radius:6px;padding:12px 14px;margin-bottom:12px}
.dl{display:inline-block;margin-top:8px;background:var(--ok);color:#fff;text-decoration:none;padding:10px 16px;border-radius:6px;font-weight:600}
ul{margin:6px 0 0 18px}li{font-size:12px;color:var(--txt2);margin:3px 0}
a{color:var(--acc)}
.foot{font-size:11px;color:var(--txt3);margin-top:20px;line-height:1.5}
.spin{display:none;text-align:center;padding:14px;color:var(--txt3);font-size:13px}
</style></head><body><div class="wrap">
<h1><span class="badge">PX</span> 案件チェックシート 生成 <span class="hint" style="font-weight:400">社内限定</span></h1>
<div class="sub">緯度経度または住所を入力すると、3タブ構成のDDチェックシート(xlsx)を生成します。</div>
__RESULT__
<form method="post" action="/generate" id="f" onsubmit="document.getElementById('go').disabled=true;document.getElementById('sp').style.display='block';">
<div class="card">
  <label>PX案件番号 <span class="hint">任意</span></label>
  <input name="pxno" value="__PXNO__" placeholder="例：PE-2025-001">
  <label>担当者 <span class="hint">任意</span></label>
  <input name="tanto" value="__TANTO__" placeholder="氏名">
  <div class="row">
    <div><label>緯度 <span class="hint">推奨</span></label><input name="lat" placeholder="42.9270"></div>
    <div><label>経度 <span class="hint">推奨</span></label><input name="lon" placeholder="141.2698"></div>
  </div>
  <label>住所 <span class="hint">緯度経度が無い場合のみ・代表点で誤差あり</span></label>
  <input name="addr" placeholder="例：北海道札幌市南区簾舞">
__KEYFIELD__
  <button type="submit" id="go">📋 チェックシートを生成する</button>
  <div class="spin" id="sp">生成中です… 座標取得・判定・xlsx作成に数十秒かかることがあります（農地判定の初回はA12ダウンロードで追加時間）。</div>
</div>
</form>
<div class="foot">
社内限定ツール。reinfolib APIキーはサーバー側で保持し、利用者の入力は不要です。入力データは処理中のみ使用し保存しません。<br>
判定は事前確認用の1次情報です（reinfolib・国土数値情報A12は参考精度）。最終確認は各行政窓口へ。
</div>
</div></body></html>"""


def render(result_html="", pxno="", tanto=""):
    # サーバー側にキーがあればキー欄は出さない。無ければ任意入力欄を出す。
    if SERVER_KEY:
        keyfield = ""
    else:
        keyfield = ('<label>reinfolib APIキー <span class="hint">任意・空でも動作</span></label>'
                    '<input name="key" type="password" placeholder="サーバー未設定時のみ入力" autocomplete="off">')
    html = (PAGE
            .replace("__RESULT__", result_html)
            .replace("__PXNO__", pxno).replace("__TANTO__", tanto)
            .replace("__KEYFIELD__", keyfield))
    return Response(html, mimetype="text/html")


@app.route("/")
@require_auth
def index():
    return render()


@app.route("/generate", methods=["GET", "POST"])
@require_auth
def gen():
    # /generate はフォーム送信(POST)専用。直接アクセス・リロード・戻る等のGETは
    # 405ではなく入力フォーム(トップ)へ誘導する。
    if request.method == "GET":
        return redirect("/")
    try:
        out, coord_source, notes = generate(request.form)
    except Exception as e:
        err = f'<div class="note">⚠️ {str(e)}</div>'
        return render(err, request.form.get("pxno", ""), request.form.get("tanto", ""))
    token = os.path.basename(os.path.dirname(out)) + "/" + os.path.basename(out)
    lis = "".join(f"<li>{n}</li>" for n in notes)
    result = (f'<div class="ok">✅ チェックシートを生成しました（座標の出所：{coord_source}）</div>'
              f'<div class="card"><a class="dl" href="/download/{urllib.parse.quote(token)}">⬇ xlsx をダウンロード</a>'
              f'<ul>{lis}</ul></div>')
    return render(result, request.form.get("pxno", ""), request.form.get("tanto", ""))


@app.route("/download/<path:token>")
@require_auth
def download(token):
    # token = <jobdir>/<filename>
    parts = token.split("/")
    if len(parts) != 2 or ".." in token:
        abort(404)
    path = os.path.join(OUT_DIR, parts[0], parts[1])
    if not os.path.abspath(path).startswith(os.path.abspath(OUT_DIR)) or not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=parts[1])


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    app.run(host="0.0.0.0", port=port)
