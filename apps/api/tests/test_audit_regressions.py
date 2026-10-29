"""PRD v0.3 边界与审查中发现的运行缺陷。"""

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from threading import Barrier, Event, Timer
from uuid import uuid4

import jwt
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from manju import jobs, seed, steps
from manju.config import COOKIE_NAME
from manju.constants import BODY_MAX_BYTES
from manju.errors import ApiError, ErrorCode
from manju.models import Asset, Clip, Episode, Job, Project, ProjectMember, Revision, User
from manju.providers import MockProvider, TextResult
from tests.conftest import login


@pytest.mark.parametrize("as_owner", [False, True])
def test_admin_content_is_readonly_even_with_membership(client, session, world, settings, as_owner):
    if as_owner:
        world["project"].owner_id = world["admin"].id
    else:
        session.add(ProjectMember(project_id=world["project"].id, user_id=world["admin"].id,
                                  role="member", created_at=datetime.now()))
    session.flush()
    login(client, world["admin"], settings)
    assert client.get("/api/v1/auth/me").json()["capabilities"]["can_write_any_project"] is False
    response = client.patch(f"/api/v1/episodes/{world['episode'].id}", json={"title": "越权"},
                            headers={"If-Match": '"1"'})
    assert response.status_code == 403


def test_admin_cannot_create_content_project(client, world, settings):
    login(client, world["admin"], settings)
    assert client.get("/api/v1/auth/me").json()["capabilities"]["can_create_project"] is False
    assert client.post("/api/v1/projects", json={"title": "越权"}).status_code == 403


@pytest.mark.parametrize("model,key", [(Project, "project"), (Episode, "episode"), (Clip, "clip")])
def test_simultaneous_edits_do_not_overwrite(session, world, model, key):
    obj = world[key]
    with Session(bind=session.connection(), join_transaction_mode="create_savepoint") as other:
        stale = other.get(model, obj.id)
        obj.title = "先保存"
        obj.version += 1
        session.flush()
        stale.title = "后覆盖"
        stale.version += 1
        with pytest.raises(StaleDataError):
            other.flush()


@pytest.mark.parametrize("payload", [{"title": 1}, {"genre": {}}, {"tone": ["x"]}])
def test_wrong_string_types_are_validation_errors(client, world, settings, payload):
    login(client, world["owner"], settings)
    assert client.post("/api/v1/projects", json=payload).status_code == 422


@pytest.mark.parametrize("path,body,version", [("projects", {"status": None}, 3),
                                              ("clips", {"duration_s": None}, 1)])
def test_nonnullable_patch_returns_422(client, world, settings, path, body, version):
    login(client, world["owner"], settings)
    obj = world["project" if path == "projects" else "clip"]
    assert client.patch(f"/api/v1/{path}/{obj.id}", json=body,
                        headers={"If-Match": f'"{version}"'}).status_code == 422


@pytest.mark.parametrize("claim", [{}, {"sub": "bad"}, {"sub": "1"}, {"sub": str(2**64)}])
def test_invalid_signed_session_is_unauthenticated(client, settings, claim):
    payload = {"iat": int(time.time()), **claim}
    if claim.get("sub") != "1":
        payload["exp"] = int(time.time()) + 60
    client.cookies.set(COOKIE_NAME, jwt.encode(payload, settings.jwt_secret, algorithm="HS256"))
    assert client.get("/api/v1/auth/me").status_code == 401


def test_streaming_request_cannot_bypass_size_limit(client):
    response = client.post("/api/v1/auth/login", content=iter([b" " * BODY_MAX_BYTES, b"{}"]),
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_streaming_request_requires_json_content_type(client):
    response = client.post("/api/v1/auth/login", content=iter([json.dumps({"email": "x@y.co", "password": "x"}).encode()]),
                           headers={"Content-Type": "text/plain"})
    assert response.status_code == 415


@pytest.mark.parametrize("value", ["2026-02-31", "9999-12-31"])
def test_cost_dates_do_not_crash(client, world, settings, value):
    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/projects/{world['project'].id}/cost", params={"to": value})
    assert response.status_code == (422 if value == "2026-02-31" else 200)


def _submit(session, world, key="audit", budget=Decimal("12"), mode="default"):
    job, _ = jobs.submit(session, user_id=world["owner"].id, clip=world["clip"], budget_limit=budget,
                         mode=mode, idem_header=key, payload={"key": key})
    session.flush()
    return job


def test_two_waiting_jobs_cannot_both_be_confirmed(session, world):
    first = _submit(session, world, "first")
    second = _submit(session, world, "second")
    jobs.confirm(session, first)
    session.flush()
    with pytest.raises(ApiError) as error:
        jobs.confirm(session, second)
    assert error.value.code == ErrorCode.PROJECT_BUSY


def test_stale_cancellation_does_not_overwrite_completion(session, world):
    job = _submit(session, world)
    session.execute(update(Job).where(Job.id == job.id).values(state="done"), execution_options={"synchronize_session": False})
    with pytest.raises(ApiError) as error:
        jobs.cancel(session, job)
    assert error.value.code == ErrorCode.JOB_NOT_CANCELLABLE


class CountingProvider(MockProvider):
    calls = 0

    def video(self, **kwargs):
        self.calls += 1
        return super().video(**kwargs)


def test_budget_is_checked_before_provider(session, world, settings):
    job = _submit(session, world, budget=Decimal("1"), mode="auto")
    provider = CountingProvider(None)
    assert jobs.run_pending(session, settings, "audit", provider) == 1
    session.refresh(job)
    assert provider.calls == 0
    assert job.state == "paused_budget" and job.cost_used == 0
    assert session.scalar(select(Revision).where(Revision.target_id == world["clip"].id)) is None


@pytest.mark.parametrize("status", ["draft", "rejected"])
def test_generation_from_editable_states_finishes(session, world, settings, status):
    world["clip"].status = status
    session.flush()
    job = _submit(session, world, mode="auto")
    jobs.run_pending(session, settings, "audit", CountingProvider(None))
    session.refresh(job)
    assert job.state == "done"


def test_deleted_target_does_not_leave_running_job(session, world, settings):
    job = _submit(session, world, mode="auto")
    session.delete(world["clip"])
    session.flush()
    jobs.run_pending(session, settings, "audit", CountingProvider(None))
    session.refresh(job)
    assert job.state == "failed" and job.error_code == "NOT_FOUND"


def test_invalid_output_fails_once_and_keeps_bill(session, world, settings):
    job = _submit(session, world, mode="auto")
    provider = CountingProvider("missing.mp4")
    jobs.run_pending(session, settings, "audit", provider)
    session.refresh(job)
    assert job.state == "failed"
    assert job.cost_used == Decimal("2.270004")
    assert session.scalar(select(Revision.cost).where(Revision.target_id == world["clip"].id)) == job.cost_used
    assert jobs.run_pending(session, settings, "audit", provider) == 0
    assert provider.calls == 1


def test_s3_rejects_untyped_nested_output(session, world, settings):
    class BadProvider(MockProvider):
        def text(self, **kwargs):
            return TextResult("mock", {"clips": [{"idx": 2, "title": "坏分镜", "duration_s": "5",
                                                  "shots": [{"oops": 1}]}]}, 1, 1, Decimal(0), 0)
    with pytest.raises(ApiError) as error:
        steps.run_step(session, settings, stage="S3", target=world["episode"], provider=BadProvider(None))
    assert error.value.code == ErrorCode.VALIDATION_INVARIANT


def test_s4_cannot_write_invalid_prompted_clip(session, world, settings):
    world["clip"].status = "draft"
    world["clip"].shots_json = None
    with pytest.raises(ApiError) as error:
        steps.run_step(session, settings, stage="S4", target=world["clip"], provider=MockProvider(None))
    assert error.value.code == ErrorCode.VALIDATION_INVARIANT
    assert world["clip"].status == "draft"


def test_deleted_clip_slot_can_be_reused_at_capacity(client, session, world, settings):
    session.add(Clip(episode_id=world["episode"].id, idx=30, source_idx=30, duration_s=Decimal("5"),
                     status="draft", created_at=datetime.now(), updated_at=datetime.now()))
    session.flush()
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/episodes/{world['episode'].id}/clips", json={"duration_s": "5"})
    assert response.status_code == 201
    assert response.json()["idx"] == 2


def test_archive_is_idempotent(client, world, settings):
    login(client, world["owner"], settings)
    first = client.post(f"/api/v1/projects/{world['project'].id}/archive")
    second = client.post(f"/api/v1/projects/{world['project'].id}/archive")
    assert first.status_code == second.status_code == 200
    assert first.json()["version"] == second.json()["version"]


def test_generation_mode_is_part_of_idempotency(client, world, settings):
    login(client, world["owner"], settings)
    path = f"/api/v1/clips/{world['clip'].id}/generate"
    headers = {"If-Match": '"1"', "Idempotency-Key": "mode"}
    assert client.post(path, json={"res": "draft"}, headers=headers).status_code == 202
    assert client.post(path + "?mode=auto", json={"res": "draft"}, headers=headers).status_code == 409


def test_login_failure_cache_expires_and_has_a_bound(monkeypatch):
    from manju.api import auth
    auth._FAILURES.clear()
    try:
        monkeypatch.setattr(auth.time, "monotonic", lambda: 1.0)
        for i in range(auth._FAILURE_KEYS_MAX + 2):
            auth._note_failure("127.0.0.1", str(i))
        assert len(auth._FAILURES) == auth._FAILURE_KEYS_MAX
        monkeypatch.setattr(auth.time, "monotonic", lambda: 1.0 + auth.LOGIN_FAIL_WINDOW)
        auth._guard_login_attempts("127.0.0.1", "new")
        assert not auth._FAILURES
    finally:
        auth._FAILURES.clear()


def test_register_duplicate_race_returns_conflict(client, session, monkeypatch):
    from manju.api import auth
    original_hash = auth.hash_password

    def competing_insert(password):
        hashed = original_hash(password)
        session.add(User(email="race@example.com", pwd_hash=hashed, role="user", is_active=1, created_at=datetime.now()))
        session.flush()
        return hashed

    monkeypatch.setattr(auth, "hash_password", competing_insert)
    response = client.post("/api/v1/auth/register", json={"email": "race@example.com", "password": "testpass12345"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT_EMAIL"


@pytest.fixture
def committed_case(engine):
    """并发用独立连接与已提交数据，不共享 Session 或依赖测试事务可见性。"""
    with Session(engine, expire_on_commit=False) as session:
        user = User(email=f"{uuid4().hex}@example.com", pwd_hash="unused", role="lead", is_active=1, created_at=datetime.now())
        session.add(user)
        session.flush()
        project = Project(title="并发测试", owner_id=user.id, status="active", version=1,
                          created_at=datetime.now(), updated_at=datetime.now())
        session.add(project)
        session.flush()
        episode = Episode(project_id=project.id, idx=1, status="draft", version=1,
                          created_at=datetime.now(), updated_at=datetime.now())
        session.add(episode)
        session.flush()
        clip = Clip(episode_id=episode.id, idx=1, source_idx=1, duration_s=Decimal("5"), status="draft", version=1,
                    created_at=datetime.now(), updated_at=datetime.now())
        session.add(clip)
        session.commit()
        ids = user.id, project.id, clip.id
    try:
        yield ids
    finally:
        with engine.begin() as connection:
            connection.execute(delete(Job).where(Job.target_kind == "clip", Job.target_id == ids[2]))
            connection.execute(delete(Project).where(Project.id == ids[1]))
            connection.execute(delete(User).where(User.id == ids[0]))


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_queue_submission(engine, committed_case, same_key):
    user_id, _, clip_id = committed_case
    barrier = Barrier(2)

    def submit(index):
        with Session(engine) as session:
            clip = session.get(Clip, clip_id)
            barrier.wait(timeout=5)
            try:
                job, replayed = jobs.submit(session, user_id=user_id, clip=clip, budget_limit=Decimal("12"),
                                             mode="auto", idem_header="shared" if same_key else str(index), payload={"res": "draft"})
                session.commit()
                return "replayed" if replayed else "created"
            except ApiError as exc:
                return exc.code.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, i) for i in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(results) == sorted(["created", "replayed" if same_key else "PROJECT_BUSY"])
    with Session(engine) as session:
        assert len(session.scalars(select(Job).where(Job.target_id == clip_id)).all()) == 1


def test_concurrent_confirmation(engine, committed_case):
    user_id, _, clip_id = committed_case
    with Session(engine) as session:
        clip = session.get(Clip, clip_id)
        ids = [jobs.submit(session, user_id=user_id, clip=clip, budget_limit=Decimal("12"), mode="default",
                           idem_header=str(i), payload={"res": "draft"})[0].id for i in range(2)]
        session.commit()
    barrier = Barrier(2)

    def confirm(job_id):
        with Session(engine) as session:
            job = session.get(Job, job_id)
            barrier.wait(timeout=5)
            try:
                jobs.confirm(session, job)
                session.commit()
                return "queued"
            except ApiError as exc:
                return exc.code.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm, job_id) for job_id in ids]
        assert sorted(f.result(timeout=10) for f in futures) == ["PROJECT_BUSY", "queued"]


def test_concurrent_edit_on_independent_connections(engine, committed_case):
    clip_id = committed_case[2]
    barrier = Barrier(2)

    def edit(title):
        with Session(engine) as session:
            clip = session.get(Clip, clip_id)
            barrier.wait(timeout=5)
            clip.title = title
            clip.version += 1
            try:
                session.commit()
                return "saved"
            except StaleDataError:
                return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(edit, title) for title in ("甲", "乙")]
        assert sorted(f.result(timeout=10) for f in futures) == ["saved", "stale"]
    with Session(engine) as session:
        assert session.get(Clip, clip_id).version == 2


def test_confirmation_does_not_deadlock_with_running_worker(engine, committed_case, monkeypatch):
    user_id, project_id, clip_id = committed_case
    with Session(engine) as session:
        job, _ = jobs.submit(session, user_id=user_id, clip=session.get(Clip, clip_id), budget_limit=Decimal("12"),
                             mode="auto", idem_header="deadlock", payload={"res": "draft"})
        session.commit()
        job_id = job.id
    project_locked, confirmation_started = Event(), Event()
    original_lock = jobs._lock_project

    def lock_project(*args):
        result = original_lock(*args)
        project_locked.set()
        return result

    monkeypatch.setattr(jobs, "_lock_project", lock_project)

    def confirm():
        with Session(engine) as session:
            try:
                job = session.get(Job, job_id)
                confirmation_started.set()
                jobs.confirm(session, job)
                session.commit()
                return "queued"
            except ApiError as exc:
                return exc.code.value

    with ThreadPoolExecutor(max_workers=1) as pool, Session(engine) as worker:
        running = jobs.claim_next(worker, "worker")
        future = pool.submit(confirm)
        assert confirmation_started.wait(timeout=5)
        project_locked.wait(timeout=1)
        # 产物入仓的 project 外键检查也会请求此行的共享锁。
        worker.execute(select(Project.id).where(Project.id == project_id).with_for_update(read=True))
        running.state = "done"
        worker.commit()
        assert future.result(timeout=10) == "JOB_STATE_INVALID"


def test_workers_skip_jobs_locked_by_another_connection(engine, committed_case):
    user_id, _, clip_id = committed_case
    with Session(engine) as session:
        clip = session.get(Clip, clip_id)
        for i in range(2):
            jobs.submit(session, user_id=user_id, clip=clip, budget_limit=Decimal("12"), mode="default",
                        idem_header=str(i), payload={"res": "draft"})
        session.execute(update(Job).where(Job.target_id == clip_id).values(state="queued"))
        session.commit()
    with Session(engine) as first, Session(engine) as second:
        one = jobs.claim_next(first, "one")
        two = jobs.claim_next(second, "two")
        assert one is not None and two is not None and one.id != two.id


def test_shutdown_waits_for_worker_before_disposing_engine(engine, settings, monkeypatch):
    from fastapi.testclient import TestClient
    from manju import main
    entered, finish, completed = Event(), Event(), Event()

    def drain(*args):
        entered.set()
        assert finish.wait(timeout=5)
        completed.set()
        return 1

    def dispose():
        assert completed.is_set()

    monkeypatch.setattr(main, "_startup_problems", lambda settings: [])
    monkeypatch.setattr(main, "make_engine", lambda *args: engine)
    monkeypatch.setattr(main, "_drain", drain)
    monkeypatch.setattr(engine, "dispose", dispose)
    app = main.create_app(replace(settings, worker=True))
    with TestClient(app):
        assert entered.wait(timeout=5)
        Timer(0.1, finish.set).start()
    assert completed.is_set()


@pytest.mark.parametrize("probe", [False, True])
def test_seed_media_timeout_does_not_abort_import(tmp_path, monkeypatch, probe):
    def stalled(command, **kwargs):
        assert kwargs["timeout"] == (15 if probe else 60)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(seed.subprocess, "run", stalled)
    source = tmp_path / "broken.mp4"
    if probe:
        assert seed._probe_size(source) is None
    else:
        assert seed.poster(tmp_path, Asset(sha256="a" * 64), source) is None
