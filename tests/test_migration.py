"""本轮功能测试: 实例迁移 (业务数据快照导出/恢复)。

覆盖需求:
- 会务方凭密钥导出一致的业务数据快照: 涵盖全部现存业务记录 (论文/评审人资料、
  资格变更、硬回避、修订号与发布版、锁定、机构归并、保障等级、截止、撤回、
  决定、评语、更正、匿名反馈快照与异议), 保留评审人凭据、反馈访问码与异议
  查询凭据, 不包含会务方密钥; 附格式版本与 sha256 内容校验和;
- 仅会务方可导出/恢复 (401);
- 恢复前核对格式版本、内容完整性 (校验和) 与符合历史保留规则的记录引用;
  只接受尚无业务记录的空实例; 校验失败/目标非空/重复恢复均拒绝且不留部分数据;
- 恢复成功后修订号、发布序号、历史记录与既有凭据/访问码按原规则工作,
  既有 HTTP API 契约保持可用。
"""
import copy
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

_TMPDIR = tempfile.mkdtemp(prefix="review-migration-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "source.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}
SOURCE_DB = os.path.join(_TMPDIR, "source.db")
TARGET_DB = os.path.join(_TMPDIR, "target.db")


def _remove_db_files(path):
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)


def _use_fresh_db(path):
    """关闭当前连接并切换到全新的数据库文件 (模拟一个全新的空实例)。"""
    db.reset_for_tests()
    _remove_db_files(path)
    db.DB_PATH = path
    db.reset_for_tests()


def switch_to_target():
    _use_fresh_db(TARGET_DB)


def switch_to_source():
    db.reset_for_tests()
    db.DB_PATH = SOURCE_DB


@pytest.fixture(autouse=True)
def clean_db():
    original = db.DB_PATH
    _use_fresh_db(SOURCE_DB)
    yield
    db.DB_PATH = original
    db.reset_for_tests()


@pytest.fixture
def client():
    return TestClient(app)


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


def add_reviewer(client, rid, institution, capacity=3, topics=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution,
            "capacity": capacity,
            "avoid_papers": [],
        },
    )


def add_paper(client, pid, institutions=None, topics=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics is not None else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def current_revision(client):
    return client.get("/meta", headers=ORG).json()["revision"]


def confirm(client, rid, pid):
    r = client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "decision": "confirm"},
    )
    assert r.status_code == 200, r.text
    return r


def recuse(client, rid, pid, reason="与作者存在合作, 声明回避"):
    r = client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "decision": "recuse", "reason": reason},
    )
    assert r.status_code == 200, r.text
    return r


def submit_review(client, rid, pid, serial, score=4, comment=None):
    r = client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={
            "paper_id": pid,
            "serial": serial,
            "score": score,
            "comment": comment if comment is not None else f"{rid} 对 {pid} 的正式评语",
        },
    )
    assert r.status_code == 200, r.text
    return r


def build_rich_state(client):
    """构造覆盖全部业务表的状态, 返回导出核对所需的关键标识。"""
    for rid, inst in (
        ("R1", "Inst-A"), ("R2", "Inst-B"), ("R3", "Inst-C"),
        ("R4", "Inst-D"), ("R5", "Acme-U"),
    ):
        add_reviewer(client, rid, inst)
    add_paper(client, "P1", institutions=["Univ-X"])
    add_paper(client, "P2", institutions=["Univ-Y"])
    add_paper(client, "P3", institutions=["Acme University"])

    # 保障等级: P1=high, P2=medium
    r = client.post(
        "/papers/P1/guarantee-level", headers=ORG,
        json={"paper_id": "P1", "level": "high", "base_revision": current_revision(client)},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/papers/P2/guarantee-level", headers=ORG,
        json={"paper_id": "P2", "level": "medium", "base_revision": current_revision(client)},
    )
    assert r.status_code == 200, r.text

    # 机构归并: Acme-U (R5) 与 Acme University (P3 作者) 同组
    r = client.post(
        "/institutions/merge-groups", headers=ORG,
        json={"name_a": "Acme-U", "name_b": "Acme University",
              "base_revision": current_revision(client)},
    )
    assert r.json()["changed"] is True

    # 普通发布 (serial 1)
    r = client.post(
        "/assignment/publish", headers=ORG,
        json={"base_revision": current_revision(client)},
    )
    assert r.status_code == 200, r.text
    serial = r.json()["serial"]
    plan = client.get("/assignment", headers=ORG).json()["plan"]

    # P1: 两名评审人确认并各交一份正式评语
    for rid in plan["P1"]:
        confirm(client, rid, "P1")
        submit_review(client, rid, "P1", serial)

    # 发布反馈快照 v1 -> 访问码 code1; 作者对标号 1 提交异议 -> 查询凭据 token1
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert r.status_code == 200, r.text
    code1 = r.json()["access_code"]
    r = client.post(
        "/feedback-objections",
        json={"access_code": code1, "label": 1, "reason": "评语引用的实验并非本文方法"},
    )
    assert r.status_code == 200, r.text
    token1 = r.json()["query_token"]

    # 会务方受理异议 -> 走更正流程 (code1 立即失效); 评审人提交更正
    objections = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    objection_id = objections[0]["objection_id"]
    label1_reviewer = objections[0]["reviewer_id"]
    r = client.post(
        f"/objections/{objection_id}/decision", headers=ORG,
        json={"decision": "accept", "reason": "异议成立, 请更正评语"},
    )
    assert r.status_code == 200, r.text
    tasks = client.get(
        "/reviewer/review-corrections", headers=reviewer_headers(label1_reviewer)
    ).json()["corrections"]
    original_receipt = tasks[0]["original"]["receipt"]
    r = client.post(
        "/reviewer/review-corrections/P1",
        headers=reviewer_headers(label1_reviewer),
        json={
            "paper_id": "P1", "serial": serial, "original_receipt": original_receipt,
            "score": 5, "comment": "更正: 复核后调整评价",
        },
    )
    assert r.status_code == 200, r.text

    # 更正完成后重发快照 -> v2 与新访问码 code2
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    code2 = r.json()["access_code"]

    # 为 serial 1 设置统一 UTC 评审截止 (未来时刻, 不触发逾期)
    deadline = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat().replace("+00:00", "Z")
    r = client.post(
        "/review-deadline", headers=ORG,
        json={"base_revision": current_revision(client), "base_serial": serial,
              "deadline_at": deadline},
    )
    assert r.json()["changed"] is True

    # P2: 一人确认, 一人回避 (硬回避) -> 补位发布 (serial 2)
    confirm(client, plan["P2"][0], "P2")
    recuse(client, plan["P2"][1], "P2")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True, dry
    r = client.post(
        "/assignment/backfill/publish", headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    serial2 = r.json()["serial"]
    assert serial2 == serial + 1

    # serial 2 下 P1 确认与评语沿用 -> 重发快照 v3 (当前有效访问码 code3)
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial2})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 3
    code3 = r.json()["access_code"]

    # 撤回 P3
    r = client.post(
        "/papers/P3/withdrawal", headers=ORG,
        json={"paper_id": "P3", "base_revision": current_revision(client),
              "reason": "作者声明一稿多投, 经确认撤稿"},
    )
    assert r.json()["changed"] is True

    # 停用 R4 (资格变更记录)
    r = client.post(
        "/reviewers/R4/status", headers=ORG,
        json={"reviewer_id": "R4", "base_revision": current_revision(client),
              "active": False, "reason": "长期休假暂停本轮评审"},
    )
    assert r.json()["changed"] is True

    # 普通分配锁定表: P1 锁定一人
    r = client.post(
        "/assignment/locks", headers=ORG,
        json={"base_revision": current_revision(client),
              "locks": {"P1": [plan["P1"][0]]}},
    )
    assert r.json()["changed"] is True

    meta = client.get("/meta", headers=ORG).json()
    return {
        "revision": meta["revision"],
        "serial": serial2,
        "plan": plan,
        "code1": code1, "code2": code2, "code3": code3,
        "token1": token1,
        "objection_id": objection_id,
        "label1_reviewer": label1_reviewer,
    }


def export_doc(client):
    r = client.get("/migration/export", headers=ORG)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------ 鉴权

def test_export_requires_organizer_key(client):
    assert client.get("/migration/export").status_code == 401
    assert client.get(
        "/migration/export", headers={"X-Organizer-Key": "wrong"}
    ).status_code == 401


def test_restore_requires_organizer_key(client):
    doc = export_doc(client)
    assert client.post("/migration/restore", json=doc).status_code == 401
    assert client.post(
        "/migration/restore", json=doc, headers={"X-Organizer-Key": "wrong"}
    ).status_code == 401


# ------------------------------------------------------------ 导出

def test_export_empty_instance(client):
    doc = export_doc(client)
    assert doc["format"] == main.MIGRATION_FORMAT
    assert doc["format_version"] == main.MIGRATION_FORMAT_VERSION
    assert doc["revision"] == 0
    assert doc["serial"] is None
    assert doc["checksum"] == main._migration_checksum(doc["data"])
    assert doc["checksum"].startswith("sha256:")
    assert doc["data"]["meta"] == {"revision": 0}
    assert doc["data"]["published"] is None
    for table in main._MIGRATION_TABLES:
        assert doc["data"][table] == []


def test_export_covers_all_business_records(client):
    state = build_rich_state(client)
    doc = export_doc(client)
    data = doc["data"]

    # 顶层: 格式版本 + 内容校验和 + 当前修订号/发布序号
    assert doc["format"] == main.MIGRATION_FORMAT
    assert doc["format_version"] == 1
    assert doc["revision"] == state["revision"]
    assert doc["serial"] == state["serial"]
    assert doc["checksum"] == main._migration_checksum(data)

    # 论文与评审人资料 (含评审人凭据); P3 已撤回
    assert {p["paper_id"] for p in data["papers"]} == {"P1", "P2", "P3"}
    assert next(p for p in data["papers"] if p["paper_id"] == "P3")["withdrawn"] is True
    creds = {r["reviewer_id"]: r["credential"] for r in data["reviewers"]}
    assert creds == {f"R{i}": f"cred-R{i}" for i in range(1, 6)}
    assert next(r for r in data["reviewers"] if r["reviewer_id"] == "R4")["active"] is False

    # 发布版 (含发布序号) 与决定/评语/硬回避
    assert data["published"]["serial"] == state["serial"]
    assert data["published"]["plan"]["P1"] == state["plan"]["P1"]
    assert len(data["assignment_decisions"]) >= 4
    assert any(d["state"] == "recused" for d in data["assignment_decisions"])
    assert len(data["submitted_reviews"]) >= 2
    assert data["reviewer_recusals"] != []

    # 资格变更记录
    assert len(data["reviewer_status_changes"]) == 1
    assert data["reviewer_status_changes"][0]["reviewer_id"] == "R4"
    assert data["reviewer_status_changes"][0]["active"] is False

    # 匿名反馈快照 (含访问码) 与作者异议 (含查询凭据)
    codes = {s["access_code"] for s in data["feedback_snapshots"]}
    assert codes == {state["code1"], state["code2"], state["code3"]}
    assert len(data["feedback_objections"]) == 1
    objection = data["feedback_objections"][0]
    assert objection["query_token"] == state["token1"]
    assert objection["state"] == "accepted"
    assert objection["correction_id"] is not None

    # 评语更正 / 锁定 / 机构归并 / 保障等级 / 截止 / 撤回
    assert len(data["review_corrections"]) == 1
    assert data["review_corrections"][0]["state"] == "completed"
    assert data["assignment_locks"] == [
        {"paper_id": "P1", "reviewers": [state["plan"]["P1"][0]],
         "updated_at": data["assignment_locks"][0]["updated_at"]}
    ]
    assert {tuple(sorted((m["name_a"], m["name_b"]))) for m in data["institution_merges"]} == {
        ("Acme University", "Acme-U")
    }
    assert {lv["paper_id"]: lv["level"] for lv in data["paper_guarantee_levels"]} == {
        "P1": "high", "P2": "medium",
    }
    assert len(data["review_deadlines"]) == 1
    assert data["review_deadlines"][0]["serial"] == 1
    assert [w["paper_id"] for w in data["paper_withdrawals"]] == ["P3"]
    assert data["paper_withdrawals"][0]["published_serial"] == 2

    # 快照绝不包含会务方密钥
    assert "test-organizer-key" not in json.dumps(doc, ensure_ascii=False)


def test_export_is_consistent_and_deterministic(client):
    """同一业务状态下两次导出的 data 与校验和完全一致 (exported_at 除外)。"""
    build_rich_state(client)
    doc1 = export_doc(client)
    doc2 = export_doc(client)
    assert doc1["data"] == doc2["data"]
    assert doc1["checksum"] == doc2["checksum"]


# ------------------------------------------------------------ 恢复: 成功路径

def test_restore_roundtrip_preserves_everything(client):
    state = build_rich_state(client)
    doc = export_doc(client)

    switch_to_target()
    r = client.post("/migration/restore", headers=ORG, json=doc)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored"] is True
    assert body["revision"] == state["revision"]
    assert body["serial"] == state["serial"]
    assert body["counts"]["papers"] == 3
    assert body["counts"]["reviewers"] == 5

    # 恢复后的实例再导出: 业务数据与校验和与源完全一致
    doc2 = export_doc(client)
    assert doc2["data"] == doc["data"]
    assert doc2["checksum"] == doc["checksum"]

    # 修订号与发布序号按原值工作
    meta = client.get("/meta", headers=ORG).json()
    assert meta["revision"] == state["revision"]
    assert meta["published"]["serial"] == state["serial"]
    assignment = client.get("/assignment", headers=ORG).json()
    assert assignment["serial"] == state["serial"]
    assert assignment["plan"]["P1"] == state["plan"]["P1"]
    assert assignment["withdrawn_papers"] == ["P3"]

    # 评审人凭据: 启用者可取稿, 停用者立即 403, 错误凭据 401
    r = client.get("/reviewer/assignments", headers=reviewer_headers("R1"))
    assert r.status_code == 200
    assert "P1" in [a["paper_id"] for a in r.json()["assignments"]]
    assert client.get("/reviewer/assignments", headers=reviewer_headers("R4")).status_code == 403
    assert client.get(
        "/reviewer/assignments",
        headers={"X-Reviewer-Id": "R1", "X-Reviewer-Credential": "wrong"},
    ).status_code == 401

    # 反馈访问码: 当前有效码可读, 已失效码统一 404
    r = client.get(f"/feedback-snapshots/{state['code3']}")
    assert r.status_code == 200
    assert r.json()["paper_id"] == "P1"
    assert client.get(f"/feedback-snapshots/{state['code2']}").status_code == 404
    assert client.get(f"/feedback-snapshots/{state['code1']}").status_code == 404

    # 异议查询凭据在恢复后仍可查状态
    r = client.get(f"/feedback-objections/{state['token1']}")
    assert r.status_code == 200
    assert r.json()["state"] == "accepted"

    # 会务方视图: 锁定表 / 保障等级 / 归并组 / 撤回记录 / 资格历史 / 评语与更正 / 快照版本
    locks = client.get("/assignment/locks", headers=ORG).json()
    assert locks["locks"] == {"P1": [state["plan"]["P1"][0]]}
    levels = client.get("/papers/guarantee-levels", headers=ORG).json()
    assert levels["levels"] == {"P1": "high", "P2": "medium"}
    groups = client.get("/institutions/merge-groups", headers=ORG).json()["groups"]
    assert groups[0]["names"] == ["Acme University", "Acme-U"]
    withdrawal = client.get("/papers/P3/withdrawal", headers=ORG).json()
    assert withdrawal["withdrawal"]["reason"] == "作者声明一稿多投, 经确认撤稿"
    status = client.get("/reviewers/R4/status", headers=ORG).json()
    assert status["active"] is False
    assert len(status["history"]) == 1
    reviews = client.get("/papers/P1/reviews", headers=ORG).json()
    assert reviews["progress"]["complete"] is True
    assert len(reviews["corrections"]) == 1
    snapshots = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"]
    assert [s["version"] for s in snapshots] == [1, 2, 3]
    assert [s["active"] for s in snapshots] == [False, False, True]
    objections = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    assert objections[0]["state"] == "accepted"
    assert objections[0]["reviewer_id"] == state["label1_reviewer"]

    # 当前发布版 (serial 2) 未设截止; 旧版截止行保留在快照数据中
    deadline_view = client.get("/review-deadline", headers=ORG).json()
    assert deadline_view["serial"] == state["serial"]
    assert deadline_view["review_deadline"] is None


def test_restored_instance_continues_original_rules(client):
    """恢复后: 修订号/发布序号门禁、决定提交、快照幂等重发、新异议均按原规则工作。"""
    state = build_rich_state(client)
    doc = export_doc(client)
    switch_to_target()
    assert client.post("/migration/restore", headers=ORG, json=doc).status_code == 200

    # 过期修订号发布 -> 409 (修订号门禁按原值工作)
    r = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": state["revision"] - 1}
    )
    assert r.status_code == 409

    # 当前发布版中 P2 的 pending 槽位可正常确认, 并推进修订号
    assignment = client.get("/assignment", headers=ORG).json()
    pending = [
        (pid, s["reviewer_id"])
        for pid, info in assignment["papers"].items()
        for s in info["slots"]
        if s["state"] == "pending" and not info.get("withdrawn")
    ]
    assert pending, "应存在待确认槽位"
    pid, rid = pending[0]
    r = client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "decision": "confirm"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == state["revision"] + 1
    assert r.json()["serial"] == state["serial"]

    # 相同序号与评语收据重发快照 -> 幂等返回原访问码 (changed=false)
    r = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": state["serial"]}
    )
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["access_code"] == state["code3"]

    # 作者凭恢复后的有效访问码提交新异议 -> 成功并发放新查询凭据
    r = client.post(
        "/feedback-objections",
        json={"access_code": state["code3"], "label": 2, "reason": "对评语二有异议"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["query_token"].startswith("obj-")
    assert client.get(f"/feedback-objections/{r.json()['query_token']}").status_code == 200


def test_restore_empty_snapshot_then_reject_duplicate(client):
    doc = export_doc(client)  # 空实例快照
    switch_to_target()
    r = client.post("/migration/restore", headers=ORG, json=doc)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 0
    # 空快照恢复后实例仍无业务记录, 但恢复标记使重复恢复被拒绝
    r = client.post("/migration/restore", headers=ORG, json=doc)
    assert r.status_code == 409


# ------------------------------------------------------------ 恢复: 拒绝路径

def test_restore_rejects_non_empty_target(client):
    doc = export_doc(client)
    # 源实例本身已有业务记录 (build 状态前也已有 revision 推进)
    add_paper(client, "PX")
    r = client.post("/migration/restore", headers=ORG, json=doc)
    assert r.status_code == 409
    # 非空目标未被改动
    assert client.get("/papers", headers=ORG).json()["papers"][0]["paper_id"] == "PX"


def test_restore_rejects_duplicate(client):
    build_rich_state(client)
    doc = export_doc(client)
    switch_to_target()
    assert client.post("/migration/restore", headers=ORG, json=doc).status_code == 200
    r = client.post("/migration/restore", headers=ORG, json=doc)
    assert r.status_code == 409
    # 重复恢复未改动任何数据
    assert export_doc(client)["data"] == doc["data"]


def test_restore_rejects_bad_format_and_version(client):
    doc = export_doc(client)
    switch_to_target()
    bad = copy.deepcopy(doc)
    bad["format"] = "something-else"
    assert client.post("/migration/restore", headers=ORG, json=bad).status_code == 422
    bad = copy.deepcopy(doc)
    bad["format_version"] = 99
    assert client.post("/migration/restore", headers=ORG, json=bad).status_code == 422
    bad = copy.deepcopy(doc)
    del bad["checksum"]
    assert client.post("/migration/restore", headers=ORG, json=bad).status_code == 422
    # 非对象请求体
    assert client.post("/migration/restore", headers=ORG, json=[1, 2, 3]).status_code == 422
    assert client.post("/migration/restore", headers=ORG, json="doc").status_code == 422
    # 全部拒绝后目标仍为空, 有效快照仍可恢复
    assert client.post("/migration/restore", headers=ORG, json=doc).status_code == 200


def test_restore_rejects_tampered_checksum_and_leaves_no_partial_data(client):
    build_rich_state(client)
    doc = export_doc(client)
    switch_to_target()
    tampered = copy.deepcopy(doc)
    tampered["data"]["papers"][0]["manuscript"] = "被篡改的内容"
    r = client.post("/migration/restore", headers=ORG, json=tampered)
    assert r.status_code == 422
    assert "校验和" in json.dumps(r.json()["detail"], ensure_ascii=False)
    # 不留下部分数据: 目标实例仍为空, 原快照随后可正常恢复
    meta = client.get("/meta", headers=ORG).json()
    assert meta["revision"] == 0
    assert meta["published"] is None
    assert client.get("/papers", headers=ORG).json()["papers"] == []
    assert client.post("/migration/restore", headers=ORG, json=doc).status_code == 200


def test_restore_rejects_broken_references(client):
    build_rich_state(client)
    doc = export_doc(client)
    switch_to_target()

    # 异议指向不存在的快照 (重算校验和以通过完整性核对, 引用核对必须拒绝)
    broken = copy.deepcopy(doc)
    broken["data"]["feedback_objections"][0]["snapshot_id"] = 99999
    broken["checksum"] = main._migration_checksum(broken["data"])
    r = client.post("/migration/restore", headers=ORG, json=broken)
    assert r.status_code == 422
    assert "快照" in json.dumps(r.json()["detail"], ensure_ascii=False)

    # 更正记录引用的评语不存在
    broken = copy.deepcopy(doc)
    broken["data"]["submitted_reviews"] = [
        rv for rv in broken["data"]["submitted_reviews"]
        if not (
            rv["serial"] == broken["data"]["review_corrections"][0]["serial"]
            and rv["paper_id"] == broken["data"]["review_corrections"][0]["paper_id"]
            and rv["reviewer_id"] == broken["data"]["review_corrections"][0]["reviewer_id"]
        )
    ]
    broken["checksum"] = main._migration_checksum(broken["data"])
    r = client.post("/migration/restore", headers=ORG, json=broken)
    assert r.status_code == 422
    assert "评语" in json.dumps(r.json()["detail"], ensure_ascii=False)

    # 撤回记录缺失 (论文标记已撤回但无撤回记录)
    broken = copy.deepcopy(doc)
    broken["data"]["paper_withdrawals"] = []
    broken["checksum"] = main._migration_checksum(broken["data"])
    r = client.post("/migration/restore", headers=ORG, json=broken)
    assert r.status_code == 422
    assert "撤回" in json.dumps(r.json()["detail"], ensure_ascii=False)

    # 发布序号超前于当前发布版
    broken = copy.deepcopy(doc)
    broken["data"]["review_deadlines"][0]["serial"] = doc["data"]["published"]["serial"] + 1
    broken["checksum"] = main._migration_checksum(broken["data"])
    r = client.post("/migration/restore", headers=ORG, json=broken)
    assert r.status_code == 422

    # 全部拒绝后目标仍为空, 有效快照仍可恢复
    assert client.get("/meta", headers=ORG).json()["revision"] == 0
    assert client.post("/migration/restore", headers=ORG, json=doc).status_code == 200


def test_restore_rejects_structural_violations(client):
    build_rich_state(client)
    doc = export_doc(client)
    switch_to_target()

    # 缺表
    broken = copy.deepcopy(doc)
    del broken["data"]["reviewers"]
    broken["checksum"] = main._migration_checksum(broken["data"])
    assert client.post("/migration/restore", headers=ORG, json=broken).status_code == 422

    # 行字段类型错误
    broken = copy.deepcopy(doc)
    broken["data"]["reviewers"][0]["capacity"] = "三"
    broken["checksum"] = main._migration_checksum(broken["data"])
    assert client.post("/migration/restore", headers=ORG, json=broken).status_code == 422

    # 唯一性冲突: 两个快照共用同一访问码
    broken = copy.deepcopy(doc)
    broken["data"]["feedback_snapshots"][1]["access_code"] = (
        broken["data"]["feedback_snapshots"][0]["access_code"]
    )
    broken["checksum"] = main._migration_checksum(broken["data"])
    assert client.post("/migration/restore", headers=ORG, json=broken).status_code == 422

    # 顶层 revision 与 data 不一致
    broken = copy.deepcopy(doc)
    broken["revision"] = doc["revision"] + 1
    broken["checksum"] = main._migration_checksum(broken["data"])
    assert client.post("/migration/restore", headers=ORG, json=broken).status_code == 422

    # 目标实例全程保持为空
    assert client.get("/meta", headers=ORG).json()["revision"] == 0
    assert client.post("/migration/restore", headers=ORG, json=doc).status_code == 200
