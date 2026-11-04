#!/bin/bash
# zhengyin v0.1.0 acceptance run — CLI level, isolated HOME.
set -u
ZY=/tmp/zy-venv/bin/zhengyin
WORK=/tmp/zy-accept
rm -rf "$WORK" /tmp/zyhome
mkdir -p "$WORK"
export HOME=/tmp/zyhome
cd "$WORK"

echo "===== CASE 5a: data fetch (real download from GitHub Releases) ====="
$ZY data fetch
echo "fetch exit=$?"
$ZY data status

# If fetch failed in this sandbox, fall back to the local dict build and say so.
if [ ! -f "$HOME/.zhengyin/data/zhengyin.db" ]; then
  echo "!! real fetch unavailable here; simulating with local zhengyin-dict dist db"
  mkdir -p "$HOME/.zhengyin/data"
  cp ~/workspace/zhengyin-dict/dist/zhengyin.db ~/workspace/zhengyin-dict/dist/manifest.json "$HOME/.zhengyin/data/"
  $ZY data status
fi

DB="$HOME/.zhengyin/data/zhengyin.db"

echo; echo "===== CASE 5b: lookup 說 ====="
$ZY lookup 說 --db "$DB"

echo; echo "===== CASE 1a: 论语 + 拼音 (說=yue4) -> tongjia 悦 ====="
printf '學而時習之，不亦說乎' > lunyu.txt
printf 'xue2 er2 shi2 xi2 zhi1 bu4 yi4 yue4 hu1' > lunyu.pinyin.txt
$ZY convert lunyu.txt -o lunyu.out.txt --pinyin lunyu.pinyin.txt --changes lunyu.changes.json --with-pinyin --db "$DB"
echo "exit=$?"
echo "--- output:"; cat lunyu.out.txt; echo
echo "--- pinyin file:"; cat lunyu.out.txt.pinyin.txt
echo "--- changes:"; cat lunyu.changes.json

echo; echo "===== CASE 1b: 论语 no pinyin -> questions with 悦（喜悦） ====="
$ZY convert lunyu.txt -o lunyu.out2.txt --questions q1b.json --db "$DB"
echo "exit=$? (expect 2)"
echo "--- questions:"; cat q1b.json
test -f lunyu.out2.txt && echo "!! output TXT should NOT exist" || echo "no output TXT (correct)"

echo; echo "===== CASE 2: 大学 在親民 + pinyin (親=xin1) -> substitute ====="
printf '大學之道，在親民，在止於至善' > daxue.txt
printf 'da4 xue2 zhi1 dao4 zai4 xin1 min2 zai4 zhi3 yu2 zhi4 shan4' > daxue.pinyin.txt
$ZY convert daxue.txt -o daxue.out.txt --pinyin daxue.pinyin.txt --changes daxue.changes.json --db "$DB"
echo "exit=$?"
echo "--- output:"; cat daxue.out.txt; echo
echo "--- changes:"; cat daxue.changes.json

echo; echo "===== CASE 3: answer loop (共 -> 供), learn, rerun ====="
printf '共' > gong.txt
$ZY convert gong.txt -o gong.out.txt --questions q3.json --db "$DB"
echo "run1 exit=$? (expect 2)"; cat q3.json
QID=$(python3 -c "import json;print(json.load(open('q3.json'))[0]['id'])")
python3 -c "import json;json.dump({'$QID':'供'}, open('a3.json','w'), ensure_ascii=False)"
$ZY convert gong.txt -o gong.out.txt --answers a3.json --db "$DB"
echo "run2 exit=$? (expect 0)"; echo "output: $(cat gong.out.txt)"
echo "--- learned.jsonl:"; cat "$HOME/.zhengyin/learned.jsonl"
$ZY convert gong.txt -o gong.out.txt --db "$DB"
echo "run3 exit=$? (expect 0, no questions)"; echo "output: $(cat gong.out.txt)"
echo "--- learn export:"; $ZY learn export

echo; echo "===== CASE 4: wrong answer rejected (target gong1, answer 球） ====="
# isolate HOME so case 3's learned entry does not short-circuit this case
export HOME=/tmp/zyhome4
mkdir -p "$HOME/.zhengyin/data"
cp /tmp/zyhome/.zhengyin/data/zhengyin.db /tmp/zyhome/.zhengyin/data/manifest.json "$HOME/.zhengyin/data/"
printf 'gong1' > gong.pinyin.txt
$ZY convert gong.txt -o gong.out.txt --pinyin gong.pinyin.txt --questions q4.json --db "$DB"
echo "run1 exit=$? (expect 2)"
QID=$(python3 -c "import json;print(json.load(open('q4.json'))[0]['id'])")
python3 -c "import json;json.dump({'$QID':'球'}, open('a4.json','w'), ensure_ascii=False)"
$ZY convert gong.txt -o gong.out.txt --pinyin gong.pinyin.txt --answers a4.json --questions q4b.json --db "$DB"
echo "run2 exit=$? (expect 2)"; cat q4b.json

echo; echo "===== pinyin count mismatch -> exit 1 ====="
printf 'xue2 er2' > bad.pinyin.txt
$ZY convert lunyu.txt -o x.txt --pinyin bad.pinyin.txt --db "$DB"
echo "exit=$? (expect 1)"
