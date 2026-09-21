"""《2026-09-20 审查.md》修复项的回归用例。

按审查报告的分节组织：

- P1-1 生成任务不再永久停留在 running（兜底收敛 + 租约回收 + 审计）
- P1-2 视频方案创建的幂等与事务边界
- P1-3 多 worker 原子领取
- P1-4 供应商视频任务按 task id 恢复，不重复计费
- P2-5 剧本拆分始终使用绑定的设定 revision
- P2-6 Clip 优化候选复制参考资产与内部镜头
- P2-7 人物立绘规划按剧本分支严格过滤
- P2-8 结果节点继续编辑、优化、抽卡
- P2-9 保存 B 与提示词优化 attempt 分离
- P2-10 立绘派生使用当前选中的结果图

这些用例全部在 mock 模式下运行，不发任何外部模型请求。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from tests.conftest import accept, auth_header, submission_of, wait_succeeded
from tests.test_acceptance_extra import (
    _media_node,
    _media_node_id,
    prepare_image_stage,
    prepare_script_stage,
)

# --- P1-1 生成任务不会永久停留在 running ---------------------------------


def test_unhandled_exception_converges_to_failed_and_releases_lock(
    client: TestClient, user_token: str, runner, monkeypatch
) -> None:
    """ValueError 之类的未捕获异常也必须收敛，并且释放节点锁。"""
    from app.services import text_ops

    token = user_token
    stage = prepare_script_stage(client, token, runner, prefix="crash")

    def boom(db, submission):  # type: ignore[no-untyped-def]
        raise ValueError("模型返回了非法 clipNo")

    monkeypatch.setitem(text_ops.HANDLERS, text_ops.Operation.plan_characters, boom)

    submission_id = accept(
        client,
        client.post(
            f"/api/v1/documents/{stage['scriptDocId']}/actions/plan-characters",
            headers={**auth_header(token), "Idempotency-Key": "crash-plan-1"},
            json={},
        ),
    )
    runner()

    detail = submission_of(client, token, submission_id)
    assert detail["submission"]["logicalStatus"] == "failed"
    assert detail["submission"]["errorCode"] == "GENERATION_FAILED"
    assert "非法 clipNo" in detail["submission"]["errorMessage"]
    assert "unhandled exception" in detail["submission"]["recoveryReason"]

    # 关键：节点锁必须已经释放，同一节点可以重新发起
    monkeypatch.undo()
    again = accept(
        client,
        client.post(
            f"/api/v1/documents/{stage['scriptDocId']}/actions/plan-characters",
            headers={**auth_header(token), "Idempotency-Key": "crash-plan-2"},
            json={},
        ),
    )
    wait_succeeded(client, token, again, runner)


def test_illegal_clip_no_from_model_fails_as_provider_rejected(
    client: TestClient, user_token: str, runner, monkeypatch
) -> None:
    """审查报告点名的典型触发点：模型返回非法 clipNo。

    它必须被归为「供应商内容不合规」，而不是裸 ValueError → GENERATION_FAILED。
    """
    from app.providers.mock import MockTextProvider

    token = user_token
    stage = prepare_script_stage(client, token, runner, prefix="badclipno")

    original = MockTextProvider.generate

    def bad_generate(self, *, system_prompt, user_prompt, expect="text", json_mode=True, context=None):  # type: ignore[no-untyped-def]
        if expect != "clips":
            return original(
                self,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                expect=expect,
                json_mode=json_mode,
                context=context,
            )
        from app.providers.base import TextResult

        payload = {"clips": [{"clipNo": "第三场", "title": "坏数据", "shots": []}]}
        return TextResult(
            text=json.dumps(payload, ensure_ascii=False),
            input_tokens=1,
            output_tokens=1,
            provider_request_id="mock_bad_clipno",
            raw_meta={"mock": True},
        )

    monkeypatch.setattr(MockTextProvider, "generate", bad_generate)

    split_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{stage['scriptDocId']}/actions/split-clips",
            headers={**auth_header(token), "Idempotency-Key": "badclipno-split"},
            json={},
        ),
    )
    runner()
    detail = submission_of(client, token, split_sub)
    assert detail["submission"]["logicalStatus"] == "failed"
    assert detail["submission"]["errorCode"] == "PROVIDER_REJECTED"
    assert "clipNo" in detail["submission"]["errorMessage"]


def test_stale_lease_is_requeued_and_audited(client: TestClient, user_token: str) -> None:
    """租约过期的 running 任务被回收成 pending，并留下恢复审计。"""
    from datetime import timedelta

    from app.core.utils import utcnow
    from app.db.engine import session_scope
    from app.models.enums import LogicalStatus, Operation
    from app.models.generation import GenerationSubmission
    from app.services.generation_service import recover_stale_submissions

    with session_scope() as db:
        submission = GenerationSubmission(
            submission_key="lease-stale-1",
            operation=Operation.outline_optimize,
            logical_status=LogicalStatus.running,
            lock_key="node-lease",
            claimed_by="worker-a",
            claimed_at=utcnow() - timedelta(hours=2),
            heartbeat_at=utcnow() - timedelta(hours=2),
        )
        db.add(submission)
        db.flush()
        submission_id = submission.id

    with session_scope() as db:
        assert recover_stale_submissions(db, lease_seconds=60) == 1

    with session_scope() as db:
        row = db.get(GenerationSubmission, submission_id)
        assert row is not None
        assert row.logical_status == LogicalStatus.pending
        assert row.recovery_count == 1
        assert "lease expired" in row.recovery_reason
        assert row.claimed_by == ""


def test_submission_with_inflight_provider_task_is_not_requeued(
    client: TestClient, user_token: str
) -> None:
    """有在途供应商任务时不得按租约回收：那会再提交一次收费任务。"""
    from datetime import timedelta

    from app.core.utils import utcnow
    from app.db.engine import session_scope
    from app.models.enums import LogicalStatus, Operation, PhysicalStatus
    from app.models.generation import GenerationAttempt, GenerationSubmission
    from app.services.generation_service import recover_stale_submissions

    with session_scope() as db:
        submission = GenerationSubmission(
            submission_key="lease-inflight-1",
            operation=Operation.video_draw,
            logical_status=LogicalStatus.running,
            lock_key="node-inflight",
            heartbeat_at=utcnow() - timedelta(hours=2),
        )
        db.add(submission)
        db.flush()
        db.add(
            GenerationAttempt(
                submission_id=submission.id,
                operation=Operation.video_draw,
                physical_status=PhysicalStatus.accepted,
                provider_task_id="ark_task_111",
                is_mock=True,
            )
        )
        db.flush()
        submission_id = submission.id

    with session_scope() as db:
        assert recover_stale_submissions(db, lease_seconds=60) == 0

    with session_scope() as db:
        row = db.get(GenerationSubmission, submission_id)
        assert row is not None
        assert row.logical_status == LogicalStatus.running
        assert "reconciler" in row.recovery_reason


# --- P1-3 多 worker 原子领取 ---------------------------------------------


def test_concurrent_workers_claim_one_pending_submission(client: TestClient, user_token: str) -> None:
    """两个 worker 同时跑 run_pending，同一个 pending 只能被领取一次。"""
    from app.db.engine import session_scope
    from app.models.enums import LogicalStatus, Operation
    from app.models.generation import GenerationSubmission
    from app.services.generation_service import list_attempts, run_pending

    with session_scope() as db:
        submission = GenerationSubmission(
            submission_key="claim-once-1",
            operation=Operation.video_plan_optimize,
            logical_status=LogicalStatus.pending,
        )
        db.add(submission)
        db.flush()
        submission_id = submission.id

    def worker() -> int:
        with session_scope() as db:
            return run_pending(db, limit=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        totals = [f.result() for f in [pool.submit(worker), pool.submit(worker)]]

    assert sum(totals) == 1, f"同一个 pending 被领取了多次：{totals}"
    with session_scope() as db:
        assert len(list_attempts(db, submission_id)) <= 1


# --- P1-2 视频方案的幂等与事务边界 ---------------------------------------


def test_video_plan_retry_reuses_plan_and_leaves_no_orphans(
    client: TestClient, user_token: str, runner
) -> None:
    """同一个 Idempotency-Key 重试：复用原提交与原方案，不新增画布节点。"""
    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=1, prefix="vplanidem")
    headers = {**auth_header(token), "Idempotency-Key": "vplan-idem-key"}

    first = client.post(
        f"/api/v1/clips/{stage['clip']['id']}/video-plans", headers=headers, json={}
    )
    assert first.status_code == 202, first.text
    body = first.json()

    before = client.get(
        f"/api/v1/episodes/{stage['episodeId']}/canvas", headers=auth_header(token)
    ).json()["nodes"]

    second = client.post(
        f"/api/v1/clips/{stage['clip']['id']}/video-plans", headers=headers, json={}
    )
    assert second.status_code == 202, second.text
    replay = second.json()
    assert replay["submissionId"] == body["submissionId"]
    assert replay["mediaNode"]["id"] == body["mediaNode"]["id"]
    # 冻结参考图数量在重放时也必须如实返回，而不是 0
    assert replay["frozenReferenceCount"] == body["frozenReferenceCount"] == 1

    after = client.get(
        f"/api/v1/episodes/{stage['episodeId']}/canvas", headers=auth_header(token)
    ).json()["nodes"]
    assert len(after) == len(before), "重试不得留下多余方案节点"


def test_video_plan_second_click_is_blocked_by_clip_node_lock(
    client: TestClient, user_token: str, runner
) -> None:
    """不同 key 的重复点击由 Clip 节点锁拦下，且不创建孤儿节点。"""
    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=1, prefix="vplanlock")
    clip = stage["clip"]

    first = client.post(
        f"/api/v1/clips/{clip['id']}/video-plans",
        headers={**auth_header(token), "Idempotency-Key": "vplan-lock-a"},
        json={},
    )
    assert first.status_code == 202, first.text

    before = client.get(
        f"/api/v1/episodes/{stage['episodeId']}/canvas", headers=auth_header(token)
    ).json()["nodes"]

    second = client.post(
        f"/api/v1/clips/{clip['id']}/video-plans",
        headers={**auth_header(token), "Idempotency-Key": "vplan-lock-b"},
        json={},
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "GENERATION_IN_PROGRESS"

    after = client.get(
        f"/api/v1/episodes/{stage['episodeId']}/canvas", headers=auth_header(token)
    ).json()["nodes"]
    assert len(after) == len(before), "被锁拦下时不得创建空方案或孤儿画布节点"

    # 任务跑完后锁自动释放，可以再次发起
    wait_succeeded(client, token, first.json()["submissionId"], runner)
    third = client.post(
        f"/api/v1/clips/{clip['id']}/video-plans",
        headers={**auth_header(token), "Idempotency-Key": "vplan-lock-c"},
        json={},
    )
    assert third.status_code == 202, third.text


# --- P1-4 供应商任务恢复 -------------------------------------------------


def _force_timeout_then_restore(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """第一次调 video generate 时先落 task id 再抛超时，模拟进程/轮询中断。"""
    from app.providers.base import ProviderError
    from app.providers.mock import MockVideoProvider

    original_generate = MockVideoProvider.generate
    state = {"raised": False}

    def flaky_generate(self, **kwargs):  # type: ignore[no-untyped-def]
        if not state["raised"]:
            state["raised"] = True
            if self.on_accepted is not None:
                self.on_accepted("ark_task_timeout_1")
            raise ProviderError("等待供应商任务超时", code="PROVIDER_TIMEOUT_UNKNOWN")
        return original_generate(self, **kwargs)

    monkeypatch.setattr(MockVideoProvider, "generate", flaky_generate)
    return state


def test_provider_timeout_keeps_attempt_reconcilable_and_recovers(
    client: TestClient, user_token: str, runner, monkeypatch
) -> None:
    """供应商超时后：提交不标 failed，task id 保留，reconciler 完成交付。"""
    from app.db.engine import session_scope
    from app.models.enums import LogicalStatus, PhysicalStatus
    from app.models.generation import GenerationAttempt, GenerationSubmission
    from app.services.provider_reconciler import reconcile_provider_tasks

    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=1, prefix="reconcile")

    plan_response = client.post(
        f"/api/v1/clips/{stage['clip']['id']}/video-plans",
        headers={**auth_header(token), "Idempotency-Key": "reconcile-plan"},
        json={},
    )
    assert plan_response.status_code == 202, plan_response.text
    plan_node = plan_response.json()["mediaNode"]["id"]
    wait_succeeded(client, token, plan_response.json()["submissionId"], runner)

    _force_timeout_then_restore(monkeypatch)

    draw_sub = accept(
        client,
        client.post(
            f"/api/v1/media/{plan_node}/draw",
            headers={**auth_header(token), "Idempotency-Key": "reconcile-draw"},
            json={"count": 1, "seed": -1},
        ),
    )
    runner()
    monkeypatch.undo()

    detail = submission_of(client, token, draw_sub)
    # 已收费的供应商任务不能被抹掉：提交保持 running，等 reconciler 收口
    assert detail["submission"]["logicalStatus"] == "running", detail["submission"]
    assert detail["attempts"], "超时前创建的 attempt 必须保留"
    attempt = detail["attempts"][0]
    assert attempt["providerTaskId"] == "ark_task_timeout_1"
    assert attempt["physicalStatus"] == "unknown"

    with session_scope() as db:
        assert reconcile_provider_tasks(db, limit=4) == 1

    recovered = submission_of(client, token, draw_sub)
    assert recovered["submission"]["logicalStatus"] == "succeeded"
    assert recovered["producedNodeIds"], "恢复出来的结果也要落到画布"
    assert recovered["attempts"][0]["deliveryStatus"] == "verified"
    assert recovered["attempts"][0]["providerTaskId"] == "ark_task_timeout_1"

    result_node = _media_node(
        client, token, _media_node_id(client, token, recovered["producedNodeIds"][0])
    )
    assert result_node["media"]["decodeStatus"] == "verified"

    # 恢复不新建供应商任务：attempt 数量不变
    with session_scope() as db:
        attempts = db.exec(
            select(GenerationAttempt).where(GenerationAttempt.submission_id == draw_sub)
        ).all()
        submission = db.get(GenerationSubmission, draw_sub)
        assert len(attempts) == 1
        assert submission is not None
        assert submission.logical_status == LogicalStatus.succeeded


def test_unrecoverable_provider_task_stops_retrying_without_resubmitting(
    client: TestClient, user_token: str, runner, monkeypatch
) -> None:
    """供应商始终不明确时，有限次复查后收敛并释放锁，绝不自动重新提交。"""
    from app.core.config import settings
    from app.db.engine import session_scope
    from app.models.enums import PhysicalStatus
    from app.models.generation import GenerationAttempt
    from app.providers.base import ProviderError
    from app.providers.mock import MockVideoProvider
    from app.services.provider_reconciler import reconcile_provider_tasks

    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=1, prefix="unresolved")
    plan_response = client.post(
        f"/api/v1/clips/{stage['clip']['id']}/video-plans",
        headers={**auth_header(token), "Idempotency-Key": "unresolved-plan"},
        json={},
    )
    plan_node = plan_response.json()["mediaNode"]["id"]
    wait_succeeded(client, token, plan_response.json()["submissionId"], runner)

    _force_timeout_then_restore(monkeypatch)

    def always_timeout(self, *, task_id: str, product_label: str = ""):  # type: ignore[no-untyped-def]
        raise ProviderError("还是不知道", code="PROVIDER_TIMEOUT_UNKNOWN")

    monkeypatch.setattr(MockVideoProvider, "resume", always_timeout)
    monkeypatch.setattr(settings, "GENERATION_RECONCILE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(settings, "GENERATION_RECONCILE_INTERVAL_SECONDS", 0)

    draw_sub = accept(
        client,
        client.post(
            f"/api/v1/media/{plan_node}/draw",
            headers={**auth_header(token), "Idempotency-Key": "unresolved-draw"},
            json={"count": 1, "seed": -1},
        ),
    )
    runner()

    for _ in range(6):
        with session_scope() as db:
            reconcile_provider_tasks(db, limit=4)

    detail = submission_of(client, token, draw_sub)
    assert detail["submission"]["logicalStatus"] == "failed"
    assert detail["submission"]["errorCode"] == "PROVIDER_TASK_UNRESOLVED"
    assert "人工确认" in detail["submission"]["errorMessage"]

    with session_scope() as db:
        attempts = db.exec(
            select(GenerationAttempt).where(GenerationAttempt.submission_id == draw_sub)
        ).all()
        # 一条 attempt，任务 id 不变：没有重建过供应商任务
        assert len(attempts) == 1
        assert attempts[0].provider_task_id == "ark_task_timeout_1"
        assert attempts[0].physical_status == PhysicalStatus.failed
        assert attempts[0].reconcile_count >= 2

    # 锁已经释放
    monkeypatch.undo()
    again = client.post(
        f"/api/v1/media/{plan_node}/draw",
        headers={**auth_header(token), "Idempotency-Key": "unresolved-draw-2"},
        json={"count": 1, "seed": -1},
    )
    assert again.status_code == 202, again.text


# --- P2-5 绑定 setting revision ------------------------------------------


def test_split_clips_uses_bound_setting_revision_not_latest(
    client: TestClient, user_token: str, runner, monkeypatch
) -> None:
    """设定更新到 V2 后，绑定 V1 的剧本重拆仍发送 V1 正文。"""
    from app.providers.mock import MockTextProvider

    token = user_token
    stage = prepare_script_stage(client, token, runner, prefix="boundrev")
    script_doc_id = stage["scriptDocId"]

    detail = client.get(
        f"/api/v1/documents/{script_doc_id}", headers=auth_header(token)
    ).json()["document"]
    bound_revision_id = detail["boundSettingRevisionId"]
    assert bound_revision_id

    # 把设定改成一份全新的 V2（新的不可变版本）
    setting_doc_id = _setting_document_id(client, token, stage["episodeId"])
    client.patch(
        f"/api/v1/documents/{setting_doc_id}/draft",
        headers=auth_header(token),
        json={"body": "V2 全新人物设定：只有「新角色乙」。", "title": "人物设定"},
    )
    saved = client.post(
        f"/api/v1/documents/{setting_doc_id}/revisions",
        headers=auth_header(token),
        json={"body": "V2 全新人物设定：只有「新角色乙」。", "title": "人物设定"},
    )
    assert saved.status_code == 201, saved.text
    new_revision_id = saved.json()["document"]["currentRevisionId"]
    assert new_revision_id != bound_revision_id

    captured: dict[str, str] = {}
    original = MockTextProvider.generate

    def capture(self, *, system_prompt, user_prompt, expect="text", json_mode=True, context=None):  # type: ignore[no-untyped-def]
        captured["prompt"] = user_prompt
        return original(
            self,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            expect=expect,
            json_mode=json_mode,
            context=context,
        )

    monkeypatch.setattr(MockTextProvider, "generate", capture)

    split_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{script_doc_id}/actions/split-clips",
            headers={**auth_header(token), "Idempotency-Key": "boundrev-split"},
            json={},
        ),
    )
    wait_succeeded(client, token, split_sub, runner)

    # 发送给模型的是 V1（绑定版本），不是 V2（最新版本）
    assert "V2 全新人物设定" not in captured["prompt"]
    assert "身份" in captured["prompt"]

    from app.db.engine import session_scope
    from app.models.generation import GenerationAttempt
    from app.services.generation_service import list_attempts

    with session_scope() as db:
        attempt = list_attempts(db, split_sub)[-1]
        snapshot = json.loads(attempt.input_snapshot_json)
        assert snapshot["settingRevisionId"] == bound_revision_id
        assert snapshot["scriptDocumentId"] == script_doc_id
    assert attempt is not None


def test_split_clips_fails_loudly_when_bound_revision_is_gone(
    client: TestClient, user_token: str, runner
) -> None:
    """绑定版本不存在时明确报错，禁止静默回退到当前版本。"""
    from app.db.engine import session_scope
    from app.models.content import ContentDocument

    token = user_token
    stage = prepare_script_stage(client, token, runner, prefix="missingrev")

    with session_scope() as db:
        script = db.get(ContentDocument, stage["scriptDocId"])
        assert script is not None
        script.bound_setting_revision_id = "01MISSINGREVISION000000000"
        db.add(script)

    split_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{stage['scriptDocId']}/actions/split-clips",
            headers={**auth_header(token), "Idempotency-Key": "missingrev-split"},
            json={},
        ),
    )
    runner()
    detail = submission_of(client, token, split_sub)
    assert detail["submission"]["logicalStatus"] == "failed"
    assert detail["submission"]["errorCode"] == "BUSINESS_RULE_VIOLATION"
    assert "人物设定版本" in detail["submission"]["errorMessage"]


def _setting_document_id(client: TestClient, token: str, episode_id: str) -> str:
    from app.db.engine import session_scope
    from app.models.content import ContentDocument
    from app.models.enums import DocumentKind

    with session_scope() as db:
        row = db.exec(
            select(ContentDocument).where(
                ContentDocument.episode_id == episode_id,
                ContentDocument.kind == DocumentKind.setting,
            )
        ).first()
        assert row is not None
        return row.id


# --- P2-6 Clip 优化候选复制参考资产与内部镜头 ---------------------------


def test_clip_optimize_candidate_keeps_references_and_shots(
    client: TestClient, user_token: str, runner
) -> None:
    """优化候选必须带上原 Clip 的参考资产与结构化镜头，否则视频流程走不下去。"""
    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=1, prefix="clippcopy")
    clip = stage["clip"]

    before = client.get(f"/api/v1/clips/{clip['id']}", headers=auth_header(token)).json()
    assert before["references"], "前置条件：原 Clip 应有回填的参考资产"
    assert before["shots"], "前置条件：原 Clip 应有内部镜头"

    optimize_sub = accept(
        client,
        client.post(
            f"/api/v1/clips/{clip['id']}/actions/optimize",
            headers={**auth_header(token), "Idempotency-Key": "clippcopy-optimize"},
            json={"count": 1},
        ),
    )
    detail = wait_succeeded(client, token, optimize_sub, runner)
    assert detail["producedNodeIds"]

    candidate_id = _clip_id_of_canvas_node(
        client, token, detail["producedNodeIds"][0]
    )
    assert candidate_id != clip["id"]
    candidate = client.get(
        f"/api/v1/clips/{candidate_id}", headers=auth_header(token)
    ).json()

    assert [r["assetId"] for r in candidate["references"]] == [
        r["assetId"] for r in before["references"]
    ]
    assert [s["shotNo"] for s in candidate["shots"]] == [
        s["shotNo"] for s in before["shots"]
    ]
    assert [s["visualText"] for s in candidate["shots"]] == [
        s["visualText"] for s in before["shots"]
    ]


def _clip_id_of_canvas_node(client: TestClient, token: str, node_id: str) -> str:
    return client.get(f"/api/v1/canvas/nodes/{node_id}", headers=auth_header(token)).json()[
        "clip"
    ]["clip"]["id"]


# --- P2-7 剧本分支过滤 ---------------------------------------------------


def test_character_planning_stays_inside_script_branch(
    client: TestClient, user_token: str, runner
) -> None:
    """A/B 两个剧本同时存在时，从 A 发起规划不能混入 B 的 Clip。"""
    from app.db.engine import session_scope
    from app.models.content import ContentDocument
    from app.services.generation_service import list_attempts

    token = user_token
    stage = prepare_script_stage(client, token, runner, prefix="branch")
    episode_id = stage["episodeId"]

    # 制造一个 B 分支剧本（同级优化分支），并为它拆出自己的 Clip
    branch_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{stage['scriptDocId']}/actions/optimize",
            headers={**auth_header(token), "Idempotency-Key": "branch-script"},
            json={},
        ),
    )
    branch_detail = wait_succeeded(client, token, branch_sub, runner)
    branch_script_doc = client.get(
        f"/api/v1/canvas/nodes/{branch_detail['producedNodeIds'][0]}",
        headers=auth_header(token),
    ).json()["document"]["document"]["id"]
    assert branch_script_doc != stage["scriptDocId"]

    split_branch = accept(
        client,
        client.post(
            f"/api/v1/documents/{branch_script_doc}/actions/split-clips",
            headers={**auth_header(token), "Idempotency-Key": "branch-split"},
            json={},
        ),
    )
    wait_succeeded(client, token, split_branch, runner)

    clips = client.get(
        f"/api/v1/episodes/{episode_id}/clips", headers=auth_header(token)
    ).json()["items"]
    a_clips = _clip_ids_of_script(clips, stage["scriptDocId"])
    b_clips = _clip_ids_of_script(clips, branch_script_doc)
    assert a_clips and b_clips and set(a_clips).isdisjoint(b_clips)

    plan_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{stage['scriptDocId']}/actions/plan-characters",
            headers={**auth_header(token), "Idempotency-Key": "branch-plan"},
            json={},
        ),
    )
    wait_succeeded(client, token, plan_sub, runner)

    with session_scope() as db:
        snapshot = json.loads(list_attempts(db, plan_sub)[-1].input_snapshot_json)
    assert set(snapshot["clipIds"]) == set(a_clips), "规划不得混入 B 分支的 Clip"
    assert snapshot["scriptDocumentId"] == stage["scriptDocId"]
    assert snapshot["scriptRevisionId"] == _current_revision_of(
        stage["scriptDocId"]
    )


def _clip_ids_of_script(clips: list[dict], script_doc_id: str) -> list[str]:
    from app.db.engine import session_scope
    from app.models.content import Clip

    with session_scope() as db:
        rows = db.exec(select(Clip).where(Clip.document_id == script_doc_id)).all()
        return [row.id for row in rows if row.id in {c["id"] for c in clips}]


def _current_revision_of(document_id: str) -> str | None:
    from app.db.engine import session_scope
    from app.models.content import ContentDocument

    with session_scope() as db:
        row = db.get(ContentDocument, document_id)
        return row.current_revision_id if row else None


# --- P2-10 立绘派生使用当前选中的结果图 ---------------------------------


def test_derive_uses_currently_selected_result_image(
    client: TestClient, user_token: str, runner
) -> None:
    """派生必须基于用户当前选中的结果图，而不是该结果的来源图。"""
    from app.db.engine import session_scope
    from app.models.media import MediaNode
    from app.services.generation_service import list_attempts

    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=2, prefix="derivepick")
    first, second = stage["results"]

    second_node = _media_node(client, token, second)
    selected_media_id = second_node["media"]["id"]

    # 人为给结果节点挂一个与自身产物不同的「来源图」，复现
    # 「source_media_id 覆盖用户当次选中结果」的旧缺陷。
    decoy_media_id = _media_node(client, token, first)["media"]["id"]
    with session_scope() as db:
        row = db.get(MediaNode, second)
        assert row is not None
        row.source_media_id = decoy_media_id
        db.add(row)

    derive_sub = accept(
        client,
        client.post(
            f"/api/v1/media-nodes/{second}/derive",
            headers={**auth_header(token), "Idempotency-Key": "derivepick-1"},
            json={},
        ),
    )
    wait_succeeded(client, token, derive_sub, runner)

    with session_scope() as db:
        snapshots = [
            json.loads(a.input_snapshot_json) for a in list_attempts(db, derive_sub)
        ]
    assert snapshots
    for snapshot in snapshots:
        assert snapshot["sourceMediaId"] == selected_media_id
        assert snapshot["sourceMediaId"] != decoy_media_id
        assert snapshot["provenanceMediaId"] == decoy_media_id


# --- P2-8 / P2-9 结果节点继续迭代与提示词优化 attempt -------------------


def test_result_node_draw_creates_reusable_iteration_plan(
    client: TestClient, user_token: str, runner
) -> None:
    """结果节点继续抽卡：建立可复用的迭代方案，保留来源结果与快照。"""
    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=1, prefix="iterate")
    source_result = stage["results"][0]

    draw_sub = accept(
        client,
        client.post(
            f"/api/v1/media/{source_result}/draw",
            headers={**auth_header(token), "Idempotency-Key": "iterate-draw-1"},
            json={"count": 1, "seed": -1},
        ),
    )
    detail = wait_succeeded(client, token, draw_sub, runner)
    assert detail["producedNodeIds"], "结果节点要继续能出下一版"

    plan_node_id = detail["submission"]["targetId"]
    plan = _media_node(client, token, plan_node_id)
    assert plan["mediaNode"]["mediaKind"] == "image_plan"
    assert plan["mediaNode"]["params"]["iterationPlanOf"] == source_result
    assert plan["mediaNode"]["params"]["sourceResultNodeId"] == source_result
    assert plan["mediaNode"]["params"]["sourceMediaItemId"]
    assert plan["mediaNode"]["sourceMediaId"] == plan["mediaNode"]["params"]["sourceMediaItemId"]

    # 再点一次复用同一个迭代方案，不制造孤儿方案节点
    again_sub = accept(
        client,
        client.post(
            f"/api/v1/media/{source_result}/draw",
            headers={**auth_header(token), "Idempotency-Key": "iterate-draw-2"},
            json={"count": 1, "seed": -1},
        ),
    )
    again = wait_succeeded(client, token, again_sub, runner)
    assert again["submission"]["targetId"] == plan_node_id


def test_prompt_optimize_records_attempt_while_saving_b_does_not(
    client: TestClient, user_token: str, runner
) -> None:
    """「发送优化」落独立 attempt；「保存用户编辑」只加 prompt version。"""
    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=0, prefix="promptsep")
    plan_node = stage["planNode"]

    detail_before = _media_node(client, token, plan_node)
    versions_before = len(detail_before["promptVersions"])

    # 保存 B：不产生 attempt
    saved = client.post(
        f"/api/v1/media-nodes/{plan_node}/prompt-versions",
        headers=auth_header(token),
        json={"visibleText": "用户手写的可见描述", "select": True},
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["promptVersion"]["source"] == "user"
    detail_after_save = _media_node(client, token, plan_node)
    assert len(detail_after_save["promptVersions"]) == versions_before + 1

    # 发送优化：产生一次独立 attempt，并追加一版 AI 版本
    optimize_sub = accept(
        client,
        client.post(
            f"/api/v1/media-nodes/{plan_node}/prompt-optimize",
            headers={**auth_header(token), "Idempotency-Key": "promptsep-optimize"},
            json={"requirement": "让光影更具体"},
        ),
    )
    optimized = wait_succeeded(client, token, optimize_sub, runner)
    assert len(optimized["attempts"]) == 1
    assert optimized["attempts"][0]["operation"] == "image_plan_optimize"

    detail_after_optimize = _media_node(client, token, plan_node)
    assert len(detail_after_optimize["promptVersions"]) == versions_before + 2
    assert detail_after_optimize["promptVersions"][-1]["source"] == "ai"


def test_prompt_optimize_on_result_node_builds_iteration_plan(
    client: TestClient, user_token: str, runner
) -> None:
    """结果节点也能继续优化：落到迭代方案上，来源结果可追溯。"""
    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=1, prefix="resultopt")
    source_result = stage["results"][0]

    optimize_sub = accept(
        client,
        client.post(
            f"/api/v1/media-nodes/{source_result}/prompt-optimize",
            headers={**auth_header(token), "Idempotency-Key": "resultopt-1"},
            json={"requirement": "更贴近雨夜氛围"},
        ),
    )
    detail = wait_succeeded(client, token, optimize_sub, runner)
    assert detail["attempts"][0]["operation"] == "image_plan_optimize"

    plan = _media_node(client, token, detail["submission"]["targetId"])
    assert plan["mediaNode"]["params"]["sourceResultNodeId"] == source_result
    assert plan["visiblePrompt"], "迭代方案要有可继续编辑的可见提示词"


def test_result_node_visible_prompt_can_be_edited(
    client: TestClient, user_token: str, runner, kind: str = "image_result"
) -> None:
    """结果节点本身不拥有提示词：编辑写入它的迭代方案，并立刻可读回。"""
    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=1, prefix=f"edit{kind}")
    result = stage["results"][0]

    response = client.post(
        f"/api/v1/media-nodes/{result}/prompt-versions",
        headers=auth_header(token),
        json={"visibleText": "在结果节点上继续编辑", "select": True},
    )
    assert response.status_code == 201, response.text

    # 版本链挂在迭代方案上，且从结果节点能读回来
    detail = _media_node(client, token, result)
    texts = [v["visibleText"] for v in detail["promptVersions"]]
    assert "在结果节点上继续编辑" in texts
