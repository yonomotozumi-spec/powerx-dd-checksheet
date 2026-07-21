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
import os, io, json, math, time, tempfile, datetime, urllib.parse, urllib.request, subprocess, traceback, types, shutil, threading, queue
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from flask import Flask, request, send_file, Response, abort, redirect

import reinfolib_judge
from shp_fast import DataNotReady

app = Flask(__name__)

BASEDIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(tempfile.gettempdir(), "pxapp_data"))
OUT_DIR = os.path.join(tempfile.gettempdir(), "pxapp_out")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

# reinfolibのタイル応答(code×z/x/y)を永続ディスクにキャッシュする置き場とTTL。
# A12/A13は県単位で温める一方、reinfolibは毎リクエストで12レイヤをネット取得していた。
# タイル単位で保存すると、近隣案件（同一タイル≒1〜2km）の再判定でネット取得を丸ごと省ける。
REINFO_CACHE_DIR = os.path.join(DATA_DIR, "reinfolib_tiles")


def _env_float(name, default):
    """環境変数を数値で読む。未設定・空文字・不正値なら default（ダッシュボードで空欄にしても落ちない）。"""
    try:
        v = os.environ.get(name, "").strip()
        return float(v) if v else float(default)
    except ValueError:
        return float(default)


REINFO_CACHE_TTL = _env_float("REINFO_CACHE_TTL_DAYS", 30) * 86400.0

# 社内共有のreinfolib APIキー（環境変数。コードには書かない）
SERVER_KEY = os.environ.get("REINFOLIB_API_KEY", "").strip()
# 社内限定アクセス用のBasic認証（任意）
APP_USER = os.environ.get("APP_USER", "").strip()
APP_PASS = os.environ.get("APP_PASS", "").strip()


# ─────────────── バックグラウンド・ウォーム（重い初回処理をリクエスト外＆直列で） ───────────────
# 重いDL＋索引構築を「リクエストの中」でやると、無料枠(CPU弱/RAM512MB/timeout)で
# ワーカーごと落ち、初回が必ずエラーになる。そこで:
#  (1) リクエストは cache_only で軽く返し、未キャッシュ県はここへ積むだけ、
#  (2) ウォームは専用ワーカー1本で「1データセットずつ直列」に実行し、
#      複数の大容量県を同時展開してメモリ超過する事故を防ぐ。
_warm_q = queue.Queue()
_warm_seen = set()
_warm_lock = threading.Lock()


def _enqueue_warm(kind, pc):
    key = (kind, str(pc).zfill(2))
    with _warm_lock:
        if key in _warm_seen:
            return
        _warm_seen.add(key)
    _warm_q.put(key)


def _warm_worker():
    while True:
        kind, pc = _warm_q.get()
        try:
            t0 = time.time()
            if kind == "a12":
                from nouchi_aochi import judge_aochi
                judge_aochi(36.0, 138.0, pc, DATA_DIR)   # download=True でキャッシュを作る
            else:
                from hoanrin import judge_hoanrin
                judge_hoanrin(36.0, 138.0, pc, DATA_DIR)
            print(f"[warm] {kind} {pc} ready in {time.time()-t0:.1f}s", flush=True)
        except Exception as e:
            print(f"[warm] {kind} {pc} failed: {type(e).__name__}: {e}", flush=True)
        finally:
            with _warm_lock:
                _warm_seen.discard((kind, pc))
            _warm_q.task_done()


threading.Thread(target=_warm_worker, name="warm", daemon=True).start()

# 起動時プリウォーム（PREWARM_PREFS）も同じ直列キューに積む
for _pc in [p.strip().zfill(2) for p in os.environ.get("PREWARM_PREFS", "").split(",") if p.strip()]:
    _enqueue_warm("a12", _pc)
    _enqueue_warm("a13", _pc)


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


def _reinfo_cache_path(code, z, x, y):
    return os.path.join(REINFO_CACHE_DIR, code, str(z), str(x), f"{y}.geojson")


def _reinfo_cache_read(path):
    """タイルキャッシュが存在しTTL内なら本文を返す。無ければ/期限切れなら None。"""
    try:
        if (time.time() - os.path.getmtime(path)) < REINFO_CACHE_TTL:
            with open(path, encoding="utf-8") as f:
                return f.read()
    except OSError:
        pass
    return None


def _reinfo_cache_write(path, body):
    """正常なGeoJSON本文だけを原子的に保存する。認証エラー/HTML等は保存しない
    （毒キャッシュ防止）。ディスク不足時などの失敗は無視して判定を続行する。"""
    try:
        obj = json.loads(body)
    except Exception:
        return
    if not (isinstance(obj, dict) and ("features" in obj or obj.get("type") == "FeatureCollection")):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 同一タイルを同時取得する別リクエストとtmpを共有しないよう書き手ごとに一意名にする
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)
    except OSError:
        pass


def fetch_reinfolib(lat, lon, key, dest):
    """reinfolibの各レイヤを取得し dest/<CODE>.geojson に保存。取得数を返す。
    タイル(code×z/x/y)単位で永続ディスクにキャッシュし、ヒット時はネット取得を省く。"""
    os.makedirs(dest, exist_ok=True)

    def one(item):
        code, (z, _d) = item
        x, y = reinfolib_judge.deg2num(lat, lon, z)
        cpath = _reinfo_cache_path(code, z, x, y)
        body = _reinfo_cache_read(cpath)          # キャッシュ・ヒットならネット不要
        if body is None:                          # ミス→ヘッダ認証で取得し、正常なら保存
            try:
                body = _get(REINFO.format(code=code, z=z, x=x, y=y),
                            headers={"Ocp-Apim-Subscription-Key": key})
            except Exception:
                return 0
            _reinfo_cache_write(cpath, body)
        try:
            with open(os.path.join(dest, code + ".geojson"), "w", encoding="utf-8") as f:
                f.write(body)
            return 1
        except OSError:
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

    # reinfolib / 農地A12 / 森林A13 の判定は互いに独立なので並列実行して総時間を短縮する。
    # 各タスクは {"values","permits","notes"} を返し、本体で決まった順にマージする。
    def task_reinfolib():
        out = {"values": {}, "permits": {}, "notes": []}
        if not key:
            out["notes"].append("APIキーが未設定のため、reinfolib自動判定はスキップ（各確認リンクは生成されます）。")
            return out
        try:
            rdir = os.path.join(work, "reinfolib")
            got = fetch_reinfolib(lat, lon, key, rdir)
            if got:
                res = reinfolib_judge.judge(lat, lon, rdir)
                out["values"].update(res.get("values", {}))
                out["permits"].update(res.get("permits", {}))
                out["notes"].append(f"reinfolibから{got}レイヤを取得し自動判定しました。")
            else:
                out["notes"].append("reinfolibの取得に失敗しました（APIキー・ネットワークを確認）。自動判定はスキップします。")
        except Exception as e:
            out["notes"].append(f"reinfolib自動判定はスキップしました（{type(e).__name__}）。")
        return out

    def task_nouchi(pcode):
        out = {"values": {}, "permits": {}, "notes": []}
        # cache_only: リクエスト内ではダウンロードしない。未キャッシュなら準備をキューに積む。
        try:
            from nouchi_aochi import judge_aochi
            val, cmt = judge_aochi(lat, lon, pcode, DATA_DIR, cache_only=True)
            out["values"]["11"] = {"value": val, "comment": cmt}
            out["notes"].append(f"農地(青地/白地)をA12から1次判定：{val}")
        except DataNotReady:
            _enqueue_warm("a12", pcode)
            out["notes"].append("農地(青地/白地)データを準備中です（この地域は初回のみ）。1〜数分後に同じ地点で再実行すると自動判定されます。")
        except Exception as e:
            print(f"[gen] A12 judge failed: {type(e).__name__}: {e}", flush=True)
            out["notes"].append(f"農地(青地/白地)の自動判定はスキップしました（{type(e).__name__}）。地図で目視確認してください。")
        return out

    def task_hoanrin(pcode):
        out = {"values": {}, "permits": {}, "notes": []}
        try:
            from hoanrin import judge_hoanrin
            val, cmt, kinds = judge_hoanrin(lat, lon, pcode, DATA_DIR, cache_only=True)
            out["values"]["12"] = {"value": val, "comment": cmt}
            if ("保安林" in kinds) or ("保安施設地区" in kinds):
                out["permits"]["34"] = {"req": "要",
                    "note": "保安林に該当 (国土数値情報A13)。立木伐採・土地形質変更には許可、開発には解除が必要な場合あり"}
            if "地域森林計画対象民有林" in kinds:
                out["permits"]["33"] = {"req": "要",
                    "note": "地域森林計画対象民有林に該当 (A13)。1ha超開発は林地開発許可、伐採は届出"}
            out["notes"].append(f"森林地域/保安林をA13から1次判定：{val}")
        except DataNotReady:
            _enqueue_warm("a13", pcode)
            out["notes"].append("森林/保安林データを準備中です（この地域は初回のみ）。1〜数分後に同じ地点で再実行すると自動判定されます。")
        except Exception as e:
            print(f"[gen] A13 judge failed: {type(e).__name__}: {e}", flush=True)
            out["notes"].append(f"森林/保安林の自動判定はスキップしました（{type(e).__name__}: {str(e)[:200]}）。都道府県の森林GISで目視確認してください。")
        return out

    # 各判定には時間予算を設ける。初回のA12/A13ダウンロード＋索引構築が重い県でも、
    # 予算超過時はその判定だけ諦めて（バックグラウンドで継続＝次回はキャッシュで高速）
    # リクエスト自体は必ず返す。これによりワーカーのタイムアウト(=502)を防ぐ。
    JUDGE_BUDGET = float(os.environ.get("JUDGE_BUDGET_SEC", "70"))
    _tstart = time.time()
    deadline = _tstart + JUDGE_BUDGET
    ex = ThreadPoolExecutor(max_workers=3)
    try:
        f_rein = ex.submit(task_reinfolib)
        # 都道府県コードはreinfolib取得と並行して取得（農地・森林の判定に必要）
        try:
            pc = pref_code(lat, lon)
        except Exception:
            pc = None
        pending = [("reinfolib", f_rein)]
        if pc:
            pending.append(("農地(A12)", ex.submit(task_nouchi, pc)))
            pending.append(("森林/保安林(A13)", ex.submit(task_hoanrin, pc)))
        else:
            notes.append("都道府県コードを特定できず、農地・森林の自動判定はスキップしました。地図で目視確認してください。")
        results = []
        for label, fut in pending:  # reinfolib→農地→森林 の順に回収
            remaining = max(1.0, deadline - time.time())
            try:
                results.append(fut.result(timeout=remaining))
            except FuturesTimeout:
                print(f"[gen] {label} timed out (> {JUDGE_BUDGET:.0f}s), continuing", flush=True)
                notes.append(f"{label}の自動判定は時間切れでスキップしました。初回のデータ取得に時間がかかっています——"
                             f"バックグラウンドで準備を継続するので、数分後に同じ地点で再実行すると高速に判定できます。")
    finally:
        ex.shutdown(wait=False)  # 未完了スレッドは裏で継続（キャッシュを温める）。応答は待たせない。
    for res in results:  # キー衝突なしなので順不同でも安全
        values_data["values"].update(res["values"])
        values_data["permits"].update(res["permits"])
        notes.extend(res["notes"])
    print(f"[gen] judges gathered in {time.time()-_tstart:.1f}s", flush=True)

    # values.json 書き出し
    vpath = os.path.join(work, "values.json")
    with open(vpath, "w", encoding="utf-8") as f:
        json.dump(values_data, f, ensure_ascii=False, indent=2)

    # xlsx 生成（プロセス内で実行。毎回のPython起動＋ライブラリ再importの開銷を排除）
    safe = "".join(c for c in (addr[:20]) if c not in '[]:*?/\\')
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(work, f"PX案件チェックシート_{safe}_{stamp}.xlsx")
    import build_px_checksheet
    bargs = types.SimpleNamespace(address=addr, lat=lat, lon=lon, muni="", pref="",
                                  pxno=pxno, tanto=tanto, values=vpath, out=out,
                                  tso="", grid="", classic=True)
    _t0 = time.time()
    build_px_checksheet.run(bargs)
    print(f"[gen] xlsx built in {time.time()-_t0:.1f}s", flush=True)
    if not os.path.exists(out):
        raise RuntimeError("xlsx生成に失敗しました")

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


# ───────────────────────── 診断（ディスク永続性） ─────────────────────────
# 起動ごとにブートマーカーを更新。永続ディスクなら再起動をまたいで boot_count が増え、
# first_boot と cached データが残る。エフェメラルなら毎回 boot_count=1・cachedは空。
def _boot_marker():
    m = {"first_boot": None, "boot_count": 0}
    path = os.path.join(DATA_DIR, ".boot_marker.json")
    try:
        if os.path.exists(path):
            m = json.load(open(path, encoding="utf-8"))
    except Exception:
        pass
    m["boot_count"] = int(m.get("boot_count", 0)) + 1
    now = datetime.datetime.now().isoformat(timespec="seconds")
    if not m.get("first_boot"):
        m["first_boot"] = now
    m["this_boot"] = now
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        json.dump(m, open(path, "w", encoding="utf-8"))
    except Exception:
        pass
    return m


_BOOT_MARKER = _boot_marker()


def _diag_report():
    rep = {"data_dir": DATA_DIR, "boot": _BOOT_MARKER}
    # DATA_DIR が / と別デバイス＝専用マウント（＝永続ディスクの可能性大）。再起動不要の即判定。
    try:
        base = DATA_DIR
        while base and not os.path.exists(base):
            base = os.path.dirname(base)
        rep["separate_mount"] = (os.stat(base or "/").st_dev != os.stat("/").st_dev)
    except Exception as e:
        rep["mount_error"] = str(e)
    try:
        du = shutil.disk_usage(DATA_DIR if os.path.isdir(DATA_DIR) else "/")
        rep["disk_total_gb"] = round(du.total / 1e9, 2)
        rep["disk_used_gb"] = round(du.used / 1e9, 2)
        rep["disk_free_gb"] = round(du.free / 1e9, 2)
    except Exception as e:
        rep["disk_error"] = str(e)
    try:
        def _prefs(sub):
            p = os.path.join(DATA_DIR, sub)
            return sorted(d for d in os.listdir(p)) if os.path.isdir(p) else []
        rep["cached_a12_prefs"] = _prefs("a12")
        rep["cached_a13_prefs"] = _prefs("a13")
        total = 0
        for root, _dirs, files in os.walk(DATA_DIR):
            for fn in files:
                try: total += os.path.getsize(os.path.join(root, fn))
                except OSError: pass
        rep["data_dir_size_mb"] = round(total / 1e6, 1)
    except Exception as e:
        rep["cache_error"] = str(e)
    # reinfolib タイルキャッシュ（近隣案件の再判定を高速化）の件数・容量
    try:
        n = sz = 0
        if os.path.isdir(REINFO_CACHE_DIR):
            for root, _dirs, files in os.walk(REINFO_CACHE_DIR):
                for fn in files:
                    if fn.endswith(".geojson"):
                        n += 1
                        try: sz += os.path.getsize(os.path.join(root, fn))
                        except OSError: pass
        rep["reinfo_tiles_cached"] = n
        rep["reinfo_tiles_mb"] = round(sz / 1e6, 1)
        rep["reinfo_cache_ttl_days"] = round(REINFO_CACHE_TTL / 86400.0, 1)
    except Exception as e:
        rep["reinfo_tiles_error"] = str(e)
    rep["judge"] = ("separate_mount=true かつ 再起動後も boot_count が増え first_boot が不変で "
                    "cached_* が残っていれば【永続】。boot_count が毎回1に戻り cached_* が空なら【非永続】。")
    return rep


@app.route("/diag")
@require_auth
def diag():
    return Response(json.dumps(_diag_report(), ensure_ascii=False, indent=2),
                    mimetype="application/json; charset=utf-8")


@app.errorhandler(Exception)
def on_error(e):
    # 想定外の例外でも素の「Internal Server Error」を出さない。
    # ここは render() 等に一切依存せず、自前でHTMLを組み立てる（エラー処理が二重に
    # 失敗して素の500になるのを防ぐ）。全文トレースバックはRenderのLogsへ。
    from werkzeug.exceptions import HTTPException
    from html import escape as _esc
    # 404等の4xxはそのまま通す。5xx/一般例外は原因を表示する。
    if isinstance(e, HTTPException) and (e.code or 500) < 500:
        return e
    try:
        print("[error] unhandled exception:\n" + traceback.format_exc(), flush=True)
    except Exception:
        pass
    detail = _esc(f"{type(e).__name__}: {str(e)[:400]}")
    page = (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>エラー</title></head>"
        "<body style='font-family:-apple-system,Segoe UI,Meiryo,sans-serif;"
        "max-width:720px;margin:48px auto;padding:0 16px;line-height:1.7;color:#16213d'>"
        "<h2>⚠️ サーバ内部でエラーが発生しました</h2>"
        "<p style='background:#fef8e0;border-radius:8px;padding:12px 14px'><code>" + detail + "</code></p>"
        "<p>お手数ですが、この画面の文言を管理者にお伝えください。"
        "<a href='/'>入力画面に戻る</a></p></body></html>"
    )
    return Response(page, status=500, mimetype="text/html; charset=utf-8")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    app.run(host="0.0.0.0", port=port)
