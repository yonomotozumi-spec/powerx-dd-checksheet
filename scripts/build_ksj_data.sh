#!/usr/bin/env bash
# 国土数値情報 A12(農業地域)・A13(森林地域) から、アプリ同梱用の県別 gzip GeoJSON を生成する。
# GitHub Actions (ubuntu-latest) での実行を想定。依存: curl, unzip, gzip, mapshaper(グローバルinstall済み)
#
#   data/a12/NN.geojson.gz : props {k:"青地"|"農業地域", m:"市町村名"}
#   data/a13/NN.geojson.gz : props {k:"保安林"|"保安施設地区"|"地域森林計画対象民有林"|"国有林"|"森林地域"}
#
# 設計上の教訓（run#1のタイムアウト解析より）:
#  - mapshaper の -clean は「重なり」を削除するため使用禁止（A13は多層重なり構造で全滅する）。
#  - 「全レイヤ結合→一括simplify」は巨大県(北海道A13)で位相計算が爆発し数時間ハングする。
#    必ず「レイヤ単位で個別にsimplify→無simplifyで結合」の順にする。
#  - 1呼び出しごとに timeout を掛け、1ファイルのハングで全体が死なないようにする。
set -u

MS_TIMEOUT=600   # mapshaper 1呼び出しの上限秒（超過はそのレイヤのみ諦めて継続）
ms() { timeout -k 15 "$MS_TIMEOUT" mapshaper "$@"; }

WORK=$(mktemp -d)
mkdir -p data/a12 data/a13
OK12=0; OK13=0; NG=""

fetch() { # $1=url $2=dest
  curl -fsSL --retry 3 --retry-delay 5 -A "powerx-dd-data-builder/1.0" -o "$2" "$1"
}

merge_parts() { # $1=出力geojson, 残り=部品geojson群。部品1個ならコピー、複数なら無simplify結合。
  local out="$1"; shift
  if [ "$#" -eq 1 ]; then cp "$1" "$out"; return 0; fi
  ms -i "$@" combine-files -merge-layers force -o format=geojson "$out"
}

for i in $(seq -w 1 47); do
  t0=$(date +%s)

  # ---------- A12 (農業地域: 05=農業地域, 06=農用地区域=青地) ----------
  rm -rf "$WORK/x"; mkdir -p "$WORK/x"
  if fetch "https://nlftp.mlit.go.jp/ksj/gml/data/A12/A12-15/A12-15_${i}_GML.zip" "$WORK/a.zip" \
     && unzip -oq "$WORK/a.zip" -d "$WORK/x"; then
    parts=()
    shp05=$(find "$WORK/x" -name "*_05.shp" | head -1)
    shp06=$(find "$WORK/x" -name "*_06.shp" | head -1)
    if [ -n "$shp05" ] && ms -i "$shp05" encoding=cp932 \
         -each 'k="農業地域", m=this.properties.CTV_NAME || this.properties.ctv_name || ""' \
         -filter-fields k,m -simplify 15% keep-shapes \
         -o format=geojson precision=0.00001 "$WORK/p05.geojson"; then
      parts+=("$WORK/p05.geojson")
    fi
    if [ -n "$shp06" ] && ms -i "$shp06" encoding=cp932 \
         -each 'k="青地", m=this.properties.CTV_NAME || this.properties.ctv_name || ""' \
         -filter-fields k,m -simplify 15% keep-shapes \
         -o format=geojson precision=0.00001 "$WORK/p06.geojson"; then
      parts+=("$WORK/p06.geojson")
    fi
    if [ ${#parts[@]} -gt 0 ] && merge_parts "$WORK/a12.geojson" "${parts[@]}"; then
      gzip -9n -c "$WORK/a12.geojson" > "data/a12/${i}.geojson.gz"
      OK12=$((OK12+1))
    else
      NG="$NG a12:${i}"
    fi
  else
    NG="$NG a12:${i}(dl)"
  fi

  # ---------- A13 (森林地域: layer_no/A13_001 を区分名に変換。レイヤ毎に個別simplify) ----------
  rm -rf "$WORK/y"; mkdir -p "$WORK/y"
  got=""
  for yr in 15 11 05; do
    if fetch "https://nlftp.mlit.go.jp/ksj/gml/data/A13/A13-${yr}/A13-${yr}_${i}_GML.zip" "$WORK/b.zip" \
       && unzip -oq "$WORK/b.zip" -d "$WORK/y"; then got=$yr; break; fi
  done
  if [ -n "$got" ]; then
    fparts=(); n=0
    while IFS= read -r shp; do
      n=$((n+1))
      if ms -i "$shp" encoding=cp932 \
           -each 'var c=+(this.properties.layer_no||this.properties.LAYER_NO||this.properties.A13_001||0); k = c==3||c==10 ? "保安林" : c==4 ? "保安施設地区" : c==2||c==9 ? "地域森林計画対象民有林" : c==1||c==8 ? "国有林" : c==7 ? "森林地域" : "区分"+c' \
           -filter-fields k -simplify 15% keep-shapes \
           -o format=geojson precision=0.00001 "$WORK/f${n}.geojson"; then
        fparts+=("$WORK/f${n}.geojson")
      else
        NG="$NG a13:${i}:part${n}"
      fi
    done < <(find "$WORK/y" -name "*.shp")
    if [ ${#fparts[@]} -gt 0 ] && merge_parts "$WORK/a13.geojson" "${fparts[@]}"; then
      gzip -9n -c "$WORK/a13.geojson" > "data/a13/${i}.geojson.gz"
      OK13=$((OK13+1))
    else
      NG="$NG a13:${i}"
    fi
  else
    NG="$NG a13:${i}(dl)"
  fi
  echo "pref ${i} done in $(( $(date +%s) - t0 ))s (A12:${OK12} A13:${OK13})"
done

echo "===== RESULT ====="
echo "A12 OK: ${OK12}/47  A13 OK: ${OK13}/47"
echo "NG:${NG:-none}"
du -sh data/a12 data/a13 || true
# 半分以上成功していれば成果をコミット対象にする（部分失敗は次回再実行で埋める）
[ "$OK12" -ge 24 ] || [ "$OK13" -ge 24 ]
