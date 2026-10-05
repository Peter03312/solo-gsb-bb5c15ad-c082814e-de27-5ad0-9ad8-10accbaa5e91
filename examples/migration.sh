#!/usr/bin/env bash
# 本轮用法端到端示例: 实例迁移 (导出一致业务数据快照 -> 空实例恢复)
#   源实例录入/发布/确认/评语/快照/异议 -> 导出快照 (格式版本 + sha256 内容校验和) ->
#   空实例恢复 -> 修订号/发布序号/凭据/访问码按原规则工作 ->
#   重复恢复 409 / 篡改内容 422 / 非空目标 409
#
# 需要两个服务实例 (不同数据库文件):
#   ORGANIZER_KEY=dev-organizer-key python -m app.main                                    # 源: 8000
#   DB_PATH=./data/app-b.db PORT=8001 ORGANIZER_KEY=dev-organizer-key python -m app.main  # 目标(空实例): 8001
# Docker 方式: 第二个容器映射 8001:8000 并使用独立数据卷即可。
# 建议对新库运行 (源实例已有业务数据不影响演示, 但脚本中的编号冲突输出会不同)。
# 用法: ./examples/migration.sh [源BASE] [目标BASE]
set -euo pipefail

SRC="${1:-http://localhost:8000}"
DST="${2:-http://localhost:8001}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
SNAP=/tmp/review-migration-snapshot.json

echo "== 1. 源实例 $SRC: 录入 2 名评审人与论文 P1, 预演并发布 =="
curl -s -o /dev/null -X POST "$SRC/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R1", "credential": "r1-secret", "topics": ["AI"],
  "institution": "Inst-A", "capacity": 2, "avoid_papers": []}'
curl -s -o /dev/null -X POST "$SRC/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R2", "credential": "r2-secret", "topics": ["AI"],
  "institution": "Inst-B", "capacity": 2, "avoid_papers": []}'
curl -s -o /dev/null -X POST "$SRC/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-X"]}'
REV=$(curl -s -X POST "$SRC/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
SERIAL=$(curl -s -X POST "$SRC/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j 'd["serial"]')
echo "    已发布: revision=$REV serial=$SERIAL"

echo "== 2. 两名评审人确认并各交一份正式评语; 会务方发布反馈快照; 作者提交异议 =="
for R in R1 R2; do
  curl -s -o /dev/null -X POST "$SRC/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $R" -H "X-Reviewer-Credential: ${R,,}-secret" \
    -H 'Content-Type: application/json' -d '{"paper_id":"P1","decision":"confirm"}'
  curl -s -o /dev/null -X POST "$SRC/reviewer/assignments/P1/review" \
    -H "X-Reviewer-Id: $R" -H "X-Reviewer-Credential: ${R,,}-secret" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\":\"P1\",\"serial\":$SERIAL,\"score\":4,\"comment\":\"$R 的正式评语\"}"
done
CODE=$(curl -s -X POST "$SRC/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SERIAL}" | j 'd["access_code"]')
TOKEN=$(curl -s -X POST "$SRC/feedback-objections" -H 'Content-Type: application/json' \
  -d "{\"access_code\": \"$CODE\", \"label\": 1, \"reason\": \"评语一存在事实错误, 请核查\"}" \
  | j 'd["query_token"]')
echo "    快照访问码: $CODE ; 异议查询凭据: $TOKEN"

echo "== 3. 会务方导出迁移快照 (与并发写入隔离的一致快照, 附格式版本与内容校验和) =="
curl -s "$SRC/migration/export" -H "$ORG" > "$SNAP"
j 'd["format"]' < "$SNAP"; j 'd["format_version"]' < "$SNAP"
python3 -c '
import json
d = json.load(open("'"$SNAP"'"))
print("    checksum:", d["checksum"][:32], "...")
print("    revision:", d["revision"], " serial:", d["serial"])
print("    记录数:", {t: len(d["data"][t]) for t in (
    "papers","reviewers","assignment_decisions","submitted_reviews",
    "feedback_snapshots","feedback_objections")})
creds = [r["credential"] for r in d["data"]["reviewers"]]
codes = [s["access_code"] for s in d["data"]["feedback_snapshots"]]
tokens = [o["query_token"] for o in d["data"]["feedback_objections"]]
print("    保留评审人凭据:", creds, " 访问码:", codes, " 查询凭据:", tokens)
assert "'"${ORGANIZER_KEY:-dev-organizer-key}"'" not in json.dumps(d), "快照不得包含会务方密钥"
print("    快照不含会务方密钥")
'

echo "== 4. 空实例 $DST: 恢复迁移快照 (恢复前核对格式/校验和/记录引用) =="
curl -s -X POST "$DST/migration/restore" -H "$ORG" -H 'Content-Type: application/json' \
  -d @"$SNAP" | j "{'restored': d['restored'], 'revision': d['revision'], 'serial': d['serial'], 'counts': d['counts']}"

echo "== 5. 恢复后既有规则原样工作: 修订号/发布序号/凭据/访问码 =="
curl -s "$DST/meta" -H "$ORG" | j "{'revision': d['revision'], 'serial': d['published']['serial']}"
curl -s "$DST/reviewer/assignments" -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  | j "{'serial': d['serial'], 'assignments': [a['paper_id'] for a in d['assignments']]}"
curl -s "$DST/feedback-snapshots/$CODE" | j 'd["paper_id"]'
curl -s "$DST/feedback-objections/$TOKEN" | j "{'state': d['state'], 'paper_id': d['paper_id']}"

echo "== 6. 拒绝语义: 重复恢复 409; 篡改内容 422; 非空目标 409 =="
curl -s -o /dev/null -w '重复恢复             -> HTTP %{http_code} (409)\n' \
  -X POST "$DST/migration/restore" -H "$ORG" -H 'Content-Type: application/json' -d @"$SNAP"
python3 -c '
import json
d = json.load(open("'"$SNAP"'"))
d["data"]["papers"][0]["manuscript"] = "被篡改"
json.dump(d, open("/tmp/review-migration-tampered.json", "w"))
'
curl -s -o /dev/null -w '篡改内容(校验和不符) -> HTTP %{http_code} (422)\n' \
  -X POST "$DST/migration/restore" -H "$ORG" -H 'Content-Type: application/json' \
  -d @/tmp/review-migration-tampered.json
curl -s -o /dev/null -w '源实例(非空)恢复     -> HTTP %{http_code} (409)\n' \
  -X POST "$SRC/migration/restore" -H "$ORG" -H 'Content-Type: application/json' -d @"$SNAP"
echo "    以上拒绝均不留下部分数据; 目标实例数据保持不变:"
curl -s "$DST/meta" -H "$ORG" | j "{'revision': d['revision'], 'serial': d['published']['serial']}"
