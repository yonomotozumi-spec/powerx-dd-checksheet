# PowerX 案件チェックシート 生成アプリ（社内Web版）

緯度経度または住所をブラウザに入力すると、サーバー側で国土地理院ジオコーディング・
不動産情報ライブラリ(reinfolib)判定・農地の青地/白地(国土数値情報A12)判定を行い、
**3タブ構成のDDチェックシート(xlsx)** を生成してダウンロードできる社内Webアプリです。

生成されるxlsx：許認可事前確認DD(17項目) ／ 許認可チェックシート(39法令) ／ 輸送確認。

## 特徴

- **ブラウザだけで利用可能**（利用者はPython導入・ローカル起動・APIキー入力すべて不要）
- **reinfolib APIキーはサーバー側で保持**（環境変数。社内共有・利用者は入力不要）
- **入力データは保存しない**（リクエスト処理の間だけ使用）
- 座標から 用途地域・市街化区域/調整区域・浸水・土砂災害・液状化・自然公園・農地(青地/白地) を自動判定し、該当する許認可も自動で「要」にします
- **社内限定アクセス**：Basic認証（環境変数で任意設定）

## Renderへのデプロイ手順

1. https://render.com にGitHubでログイン
2. **New → Web Service** → このリポジトリを選択
3. 設定は `render.yaml` が自動認識（手動なら：Runtime=Python / Build=`pip install -r requirements.txt` / Start=`gunicorn app:app --timeout 300 --workers 1`）
4. **Environment** で以下の環境変数を設定：

   | 変数 | 必須 | 内容 |
   |---|---|---|
   | `REINFOLIB_API_KEY` | 推奨 | 社内共有のreinfolib APIキー。設定すると利用者はキー入力不要で自動判定が有効になる |
   | `APP_USER` | 任意 | Basic認証のユーザー名（社内限定にする場合） |
   | `APP_PASS` | 任意 | Basic認証のパスワード（`APP_USER`とセットで有効） |

5. **Create Web Service** → 数分でビルド完了 → `https://<名前>.onrender.com` で社内公開

> APIキーやパスワードは**コードに書かず、必ずRenderの環境変数（Environment）に設定**してください。
> `render.yaml` は農地(A12)キャッシュ用の永続ディスク（1GB, `/var/data`）を定義しています。
> 無料プランは一定時間アクセスがないとスリープし、次アクセス時に数十秒かけて起動します。

## ローカルでの起動（開発・確認用）

```bash
pip install -r requirements.txt
# 社内キーを使う場合（任意）
export REINFOLIB_API_KEY=あなたのキー
python app.py            # http://127.0.0.1:8765
```

## 構成

| ファイル | 役割 |
|---|---|
| `app.py` | Flaskアプリ本体（フォーム・Basic認証・ネット取得・生成のオーケストレーション） |
| `reinfolib_judge.py` | reinfolib GeoJSONのタイル計算・点内判定・値/許認可の組み立て |
| `nouchi_aochi.py` | 国土数値情報A12から農地の青地/白地を点内判定（都道府県shapefileをDL・キャッシュ） |
| `build_px_checksheet.py` | xlsx生成（`--classic`で元の3タブ。系統接続タブ等の拡張も内包） |
| `render.yaml` / `Procfile` / `requirements.txt` / `runtime.txt` | Renderデプロイ設定 |

## 注意・免責

- reinfolib・国土数値情報A12（2015年版）は**参考精度**です。農地の種別(1/2/3種)や各区域の最終確定は、
  市町村農政課・農業委員会・各行政窓口へ照会してください。
- 住所からの座標は代表点（数百m誤差の可能性）です。筆単位の判定は緯度経度の入力を推奨します。
- 本ツールの判定は事前スクリーニング用の1次情報であり、許認可の最終判断に代わるものではありません。
