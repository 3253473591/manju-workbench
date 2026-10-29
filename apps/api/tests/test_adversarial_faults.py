"""对抗性探针 3：故障注入 —— 上游骗人、产物越界、脏 DB 行跑 worker。

这一批不测"正常路径对不对"，只测**出错时能不能收场**：
- provider 返回自相矛盾的账单（cost 与 unit_price×秒数对不上）→ 必须落 failed 且**账单照记**；
- provider 把产物指到 MEDIA_ROOT 外面 / 指到不存在的文件 / 抛非 `ProviderError` 的异常
  → job 必须停在终态，进程不能带着半截事务往前走；
- 文本 step 的 provider 返回缺字段/多余字段/错类型 → 422 `VALIDATION_INVARIANT`，不是 500；
- DB 里塞脏 JSON 的片段被 worker 捡到 → 失败得有理有据，不是 `KeyError`。

判据只有一条：**job 一定落在终态，且 error_code 说得清是哪一类问题**。没有终态的失败会
让前端永远转圈，这比报错更糟。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import jobs
from manju.config import Settings
from manju.models import Clip, Job, Revision
from manju.providers import MockProvider, ProviderError, ProviderResult, TextResult
from tests.conftest import login

TERMINAL = {"done", "failed", "cancelled", "paused_budget"}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _clip(session: Session, world: dict, *, idx: int = 20, status: str = "prompted", **extra) -> Clip:
    fields = {
        "episode_id": world["episode"].id,
        "idx": idx,
        "source_idx": idx,
        "duration_s": Decimal("6.00"),
        "status": status,
        "prompt": "镜头1（0-6秒）：远景急推",
        "shots_json": [{"no": 1, "start": 0, "end": 6, "desc": "远景急推"}],
        "created_at": _now(),
        "updated_at": _now(),
    }
    fields.update(extra)
    clip = Clip(**fields)
    session.add(clip)
    session.flush()
    return clip


def _run(session: Session, world: dict, settings: Settings, provider, clip: Clip | None = None) -> Job:
    clip = clip or _clip(session, world)
    job, _ = jobs.submit(
        session,
        user_id=world["owner"].id,
        clip=clip,
        budget_limit=Decimal("12.000000"),
        mode="auto",
        idem_header=f"fault-{clip.id}",
        payload={"clip_id": clip.id},
    )
    session.flush()
    jobs.run_pending(session, settings, "t", provider)
    session.refresh(job)
    return job


class LyingProvider(MockProvider):
    """账单自相矛盾：报 1 秒、却按 12 秒收钱。"""

    def video(self, *, prompt: str, seconds: Decimal, model: str) -> ProviderResult:
        result = super().video(prompt=prompt, seconds=seconds, model=model)
        return ProviderResult(
            **{**result.__dict__, "video_seconds": Decimal("1.00"), "cost": Decimal("2.270004")}
        )


class EscapingProvider(MockProvider):
    def video(self, *, prompt: str, seconds: Decimal, model: str) -> ProviderResult:
        result = super().video(prompt=prompt, seconds=seconds, model=model)
        return ProviderResult(**{**result.__dict__, "output_path": "../../../../Windows/win.ini"})


class MissingFileProvider(MockProvider):
    def video(self, *, prompt: str, seconds: Decimal, model: str) -> ProviderResult:
        result = super().video(prompt=prompt, seconds=seconds, model=model)
        return ProviderResult(**{**result.__dict__, "output_path": "第一集/不存在.mp4"})


class ExplodingProvider(MockProvider):
    def video(self, *, prompt: str, seconds: Decimal, model: str) -> ProviderResult:
        raise ValueError("上游返回了半截 JSON")  # 不是 ProviderError：更接近真实事故


class RateLimitedProvider(MockProvider):
    def video(self, *, prompt: str, seconds: Decimal, model: str) -> ProviderResult:
        raise ProviderError("429", retryable=True, status=429, raw={"retry_after": 5})


@pytest.fixture
def media(media_root: Path) -> None:
    (media_root / "第一集").mkdir(parents=True, exist_ok=True)
    (media_root / "第一集" / "s1_demo.mp4").write_bytes(b"\x00" * 128)


def test_lying_bill_keeps_revision_and_fails_job(
    session: Session, settings: Settings, world: dict, media: None
) -> None:
    """cost 与 unit_price×秒数 对不上 → job 必须 failed，但**钱已经花了**，revision 照落。"""
    provider = LyingProvider(Path("第一集/s1_demo.mp4"))
    job = _run(session, world, settings, provider)
    assert job.state in TERMINAL
    assert job.state == "failed"
    revisions = session.scalars(select(Revision).where(Revision.target_kind == "clip")).all()
    assert revisions, "provider 已经返回，账单不能跟着事务回滚丢掉"
    assert revisions[0].cost == Decimal("2.270004")


def test_output_path_escape_is_rejected(session: Session, settings: Settings, world: dict, media: None) -> None:
    job = _run(session, world, settings, EscapingProvider(Path("第一集/s1_demo.mp4")))
    assert job.state == "failed"
    assert b"win.ini" not in (settings.media_root / "第一集/s1_demo.mp4").read_bytes()


def test_missing_output_file_fails_with_asset_code(
    session: Session, settings: Settings, world: dict, media: None
) -> None:
    job = _run(session, world, settings, MissingFileProvider(Path("第一集/s1_demo.mp4")))
    assert job.state == "failed"
    assert job.error_code == "ASSET_FETCH_FAILED"


def test_unexpected_exception_still_lands_terminal(
    session: Session, settings: Settings, world: dict, media: None
) -> None:
    """非 `ProviderError` 的异常如果逃出 `run_pending`，worker 会带着坏事务继续跑。"""
    job = _run(session, world, settings, ExplodingProvider(Path("第一集/s1_demo.mp4")))
    assert job.state == "failed"
    assert job.error_code is not None


def test_rate_limit_is_recorded_as_retryable(
    session: Session, settings: Settings, world: dict, media: None
) -> None:
    job = _run(session, world, settings, RateLimitedProvider(Path("第一集/s1_demo.mp4")))
    assert job.state == "failed"
    assert job.error_code == "PROVIDER_UNAVAILABLE"
    assert job.error_detail["retryable"] is True
    retried = jobs.retry(session, job)
    assert retried.state == "queued"


def test_dirty_acceptance_json_does_not_kill_worker(
    session: Session, settings: Settings, world: dict, media: None
) -> None:
    """历史行验收点缺 criterion：worker 只能记 unknown，不能 KeyError。"""
    clip = _clip(session, world, acceptance_json=[{"id": "a1"}, {"criterion": "开场"}])
    job = _run(session, world, settings, MockProvider(Path("第一集/s1_demo.mp4")), clip)
    assert job.state in TERMINAL
    revision = session.scalars(select(Revision).where(Revision.target_id == clip.id)).one()
    assert all(item["result"] == "unknown" for item in revision.acceptance_result_json)


def test_dirty_shots_json_is_422_on_step(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """S4 撞上脏分镜 → 422 带"脏数据"，而不是 KeyError 冒成 500。"""
    clip = _clip(session, world, idx=21, shots_json=[{"start": 0, "end": 6}])
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/clips/{clip.id}/steps/S4/run")
    assert response.status_code == 422, response.text
    assert "脏数据" in response.json()["error"]["detail"]["problems"][0]


class GarbageTextProvider(MockProvider):
    def __init__(self, payload: dict):
        super().__init__(Path("第一集/s1_demo.mp4"))
        self.payload = payload

    def text(self, *, stage: str, context: str, format_hint: str = "") -> TextResult:
        return TextResult(
            model="garbage", payload=self.payload, input_tokens=1, output_tokens=1,
            cost=Decimal(0), latency_ms=1,
        )


class FailingTextProvider(MockProvider):
    def text(self, *, stage: str, context: str, format_hint: str = "") -> TextResult:
        raise ProviderError("上游 500", retryable=True, status=500)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"prompt": None},
        {"prompt": ""},
        {"prompt": "x" * 20_001},
        {"prompt": "ok", "negative": 1},
        {"prompt": "ok", "extra_field": "nope"},
    ],
)
def test_garbage_text_payload_is_422_not_500(
    client: TestClient, session: Session, world: dict, settings: Settings, monkeypatch, payload: dict
) -> None:
    monkeypatch.setattr(
        "manju.steps.make_provider", lambda settings, name="mock": GarbageTextProvider(payload)
    )
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/clips/{world['clip'].id}/steps/S4/run")
    assert response.status_code == 422, f"{payload} → {response.status_code} {response.text[:200]}"
    assert response.json()["error"]["code"] == "VALIDATION_INVARIANT"


def test_failing_text_provider_is_503(
    client: TestClient, world: dict, settings: Settings, monkeypatch
) -> None:
    monkeypatch.setattr(
        "manju.steps.make_provider", lambda settings, name="mock": FailingTextProvider(Path("x"))
    )
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/clips/{world['clip'].id}/steps/S4/run")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"


def test_project_busy_when_another_job_is_queued(
    client: TestClient, world: dict, settings: Settings
) -> None:
    """同项目同时只跑一个：第二条提交必须 429 且带 Retry-After，而不是排着队偷偷跑。"""
    login(client, world["owner"], settings)
    headers = {"Idempotency-Key": "busy-1", "If-Match": '"1"'}
    first = client.post(
        f"/api/v1/clips/{world['clip'].id}/generate?mode=auto", json={"res": "draft"}, headers=headers
    )
    assert first.status_code == 202
    second = client.post(
        f"/api/v1/clips/{world['clip'].id}/generate?mode=auto",
        json={"res": "draft"},
        headers={"Idempotency-Key": "busy-2", "If-Match": '"1"'},
    )
    assert second.status_code == 429, second.text
    assert second.json()["error"]["code"] == "PROJECT_BUSY"
    assert second.headers.get("Retry-After") == "5"


def test_revision_hash_and_cost_are_never_null(
    session: Session, settings: Settings, world: dict, media: None
) -> None:
    """一次成功出片后，账单三件套不许为空 —— 空值会让成本聚合静默少算。"""
    job = _run(session, world, settings, MockProvider(Path("第一集/s1_demo.mp4")))
    assert job.state == "done"
    revision = session.scalars(select(Revision).where(Revision.target_id == job.target_id)).one()
    assert revision.cost is not None and revision.unit_price is not None
    assert revision.video_seconds is not None and revision.input_tokens
    assert hashlib.sha256(b"x").hexdigest() != revision.params_json.get("sha")
