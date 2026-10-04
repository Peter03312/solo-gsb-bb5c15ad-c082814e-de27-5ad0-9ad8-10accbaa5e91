#!/usr/bin/env bash
# 本轮用法端到端示例: 会务方实例迁移 (一致业务数据快照导出 -> 空实例恢复)
#   源实例录入/发布/确认/评语/快照 -> 导出快照 (含格式版本与内容校验和) ->
#   本地复算校验和 -> 空目标实例恢复 -> 修订号/发布序号/凭据/访问码按原规则工作
#
# 需要两个实例:
#   源实例 (已有业务数据):   BASE  默认 http://localhost:8000
#   目标实例 (必须为空库):   BASE2 默认 http://localhost:8001
# 目标实例可用空库启动, 例如:
#   DB_PATH=./data/app-target.db PORT=8001 ORGANIZER_KEY=dev-organizer-key python -m app.main
# 用法: ./examples/migration.sh [BASE_URL] [BASE2_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
BASE2="${2:-http://localhost:8001}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
SNAP=/tmp/review-migration-snapshot.json

echo "== 1. 源实例录入 4 名评审人与论文 P1/P2 并发布 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-X"]}'
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P2", "manuscript": "P2 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-Y"]}'
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
SERIAL=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j 'd["serial"]')
echo "    已发布: revision=$REV serial=$SERIAL plan=$(curl -s "$BASE/assignment" -H "$ORG" | j 'd["plan"]')"

echo "== 2. 评审人确认并交正式评语, 会务方发布 P1 反馈快照 =="
for RID in R1 R2; do
  CRED=$( [ "$RID" = R1 ] && echo r1-secret || echo r2-secret )
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $RID" -H "X-Reviewer-Credential: $CRED" \
    -H 'Content-Type: application/json' -d '{"paper_id":"P1","decision":"confirm"}'
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
    -H "X-Reviewer-Id: $RID" -H "X-Reviewer-Credential: $CRED" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\":\"P1\",\"serial\":$SERIAL,\"score\":4,\"comment\":\"$RID 对 P1 的正式评语\"}"
done
CODE=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SERIAL}" | j 'd["access_code"]')
echo "    P1 反馈快照访问码: $CODE"

echo "== 3. 会务方导出一致业务数据快照 (与并发写入隔离, 附格式版本与校验和) =="
curl -s "$BASE/migration/export" -H "$ORG" > "$SNAP"
python3 - "$SNAP" <<'PY'
import json, sys
snap = json.load(open(sys.argv[1]))
data = snap["data"]
print(f"    format={snap['format']} version={snap['format_version']}")
print(f"    checksum={snap['checksum']}")
print(f"    revision={data['revision']} serial={data['published']['serial'] if data['published'] else None}")
print("    记录数:", {k: len(v) for k, v in data.items() if isinstance(v, list)})
PY

echo "== 4. 本地复算内容校验和 (核对快照完整) =="
python3 - "$SNAP" <<'PY'
import hashlib, json, sys
snap = json.load(open(sys.argv[1]))
canon = json.dumps(snap["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
digest = "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()
assert digest == snap["checksum"], "校验和不符, 快照已损坏!"
print("    校验和一致:", digest)
PY

echo "== 5. 空目标实例恢复 (目标非空/重复恢复/校验失败一律拒绝且不留部分数据) =="
curl -s -X POST "$BASE2/migration/restore" -H "$ORG" -H 'Content-Type: application/json' \
  --data-binary @"$SNAP" | j "{'ok': d['ok'], 'revision': d['revision'], 'serial': d['serial'], 'counts': d['counts']}"

echo "== 6. 重复恢复 -> 409; 非会务方 -> 401 =="
curl -s -o /dev/null -w '重复恢复           -> HTTP %{http_code}\n' -X POST "$BASE2/migration/restore" \
  -H "$ORG" -H 'Content-Type: application/json' --data-binary @"$SNAP"
curl -s -o /dev/null -w '无密钥导出         -> HTTP %{http_code}\n' "$BASE/migration/export"
curl -s -o /dev/null -w '无密钥恢复         -> HTTP %{http_code}\n' -X POST "$BASE2/migration/restore" \
  -H 'Content-Type: application/json' --data-binary @"$SNAP"

echo "== 7. 目标实例按原规则继续工作: 修订号/发布序号/凭据/访问码 =="
curl -s "$BASE2/meta" -H "$ORG" | j "{'revision': d['revision'], 'serial': d['published']['serial']}"
curl -s "$BASE2/assignment" -H "$ORG" | j 'd["plan"]'
curl -s "$BASE2/reviewer/assignments" -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  | j '[a["paper_id"] for a in d["assignments"]]'
curl -s "$BASE2/feedback-snapshots/$CODE" | j 'd["paper_id"], [r["label"] for r in d["reviews"]]'
echo "== 迁移完成: 源实例快照已一致恢复到空目标实例 =="
