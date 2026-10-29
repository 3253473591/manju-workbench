"""彩排：按演示脚本的顺序，把每一拍的关键动作真打一遍。

和录屏的区别是"不看画面"：断言的是**接口返回与状态变化**（409/503 也要按预期出现），
把"现场会不会卡"提前暴露。mock provider + 本地库，零成本。

用法：
    python scripts/demo_rehearsal.py --api http://127.0.0.1:8040
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

import httpx

OWNER = ("seed@example.com", "seedpass12345")
ADMIN = ("admin@example.com", "admin12345")
PROJECT_ID = 1


@dataclass
class Beat:
    name: str
    ok: bool = False
    ms: int = 0
    notes: list[str] = field(default_factory=list)


class Demo:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(timeout=120, headers={"Origin": "http://127.0.0.1:8040"})
        self.beats: list[Beat] = []

    def login(self, account: tuple[str, str]) -> None:
        self.client.cookies.clear()
        response = self.client.post(f"{self.base}/api/v1/auth/login", json={"email": account[0], "password": account[1]})
        response.raise_for_status()

    def beat(self, name: str):
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            item = Beat(name=name)
            started = time.perf_counter()
            try:
                yield item
                item.ok = True
            except AssertionError as exc:
                item.notes.append(f"断言失败：{exc}")
            except Exception as exc:  # noqa: BLE001
                item.notes.append(f"{type(exc).__name__}: {exc}")
            finally:
                item.ms = int((time.perf_counter() - started) * 1000)
                self.beats.append(item)

        return _ctx()


def rehearsal(api: str) -> int:
    demo = Demo(api)
    base = demo.base

    with demo.beat("0:00 打开第一集（数据就位）") as beat:
        demo.login(OWNER)
        projects = demo.client.get(f"{base}/api/v1/projects").json()["items"]
        project = next(p for p in projects if p["id"] == PROJECT_ID)
        episodes = demo.client.get(f"{base}/api/v1/projects/{PROJECT_ID}/episodes").json()
        clips = demo.client.get(f"{base}/api/v1/projects/{PROJECT_ID}/clips").json()["items"]
        beat.notes.append(f"{project['title']} · {len(episodes)} 集 · {len(clips)} 片段")
        assert len(episodes) >= 2 and len(clips) >= 10

    with demo.beat("0:15 建项目 → S1/S2/S3") as beat:
        created = demo.client.post(
            f"{base}/api/v1/projects",
            json={"title": "彩排项目", "genre": "修仙荒诞", "tone": "荒诞", "logline": "彩排用：一个镇子把麻烦推给外人。"},
        )
        # 同 body_md 再建会命中 checksum 复用，返回 200；新内容才 201
        assert created.status_code in (200, 201), created.text
        pid = created.json()["id"]
        episode = demo.client.post(f"{base}/api/v1/projects/{pid}/episodes", json={"title": "彩排第一集"})
        assert episode.status_code == 201, episode.text
        eid = episode.json()["id"]
        for stage in ("S1", "S2", "S3"):
            if stage == "S1":
                run = demo.client.post(f"{base}/api/v1/projects/{pid}/steps/{stage}/run")
            else:
                run = demo.client.post(f"{base}/api/v1/episodes/{eid}/steps/{stage}/run")
            beat.notes.append(f"{stage}={run.status_code}")
            assert run.status_code == 200, f"{stage}: {run.text[:200]}"
        clips = demo.client.get(f"{base}/api/v1/episodes/{eid}/clips").json()
        beat.notes.append(f"片段={len(clips)}")
        assert len(clips) >= 1
        demo.rehearsal_pid = pid
        demo.rehearsal_eid = eid

    with demo.beat("0:40 S5 → 用人物卡出图") as beat:
        pid = getattr(demo, "rehearsal_pid", PROJECT_ID)
        s5 = demo.client.post(f"{base}/api/v1/projects/{pid}/steps/S5/run")
        assert s5.status_code == 200, s5.text
        characters = s5.json()["payload"]["characters"]
        prompt = "；".join(f"{c['name']}：{c['anchor']}" for c in characters)[:1500] + "。国风修仙人物立绘，全身正面，纯色背景"
        image = demo.client.post(f"{base}/api/v1/projects/{pid}/images", json={"prompt": prompt})
        assert image.status_code == 201, image.text
        body = image.json()
        beat.notes.append(f"asset={body['asset_id']} provider_source={body['provider_source']} deduped={body['deduped']}")

    with demo.beat("1:00 第二集槽位复用第一集的图") as beat:
        ep2 = demo.client.get(f"{base}/api/v1/projects/{PROJECT_ID}/episodes").json()[-1]
        ep2_clips = demo.client.get(f"{base}/api/v1/episodes/{ep2['id']}/clips").json()
        target = next(c for c in ep2_clips if c.get("prompt"))
        # 找一个被两集引用的资产（asset 6）
        shared = 6
        original = target.get("ref_slots") or []
        slots = list(original)
        if not any(slot.get("asset_id") == shared for slot in slots):
            slots.append({"idx": len(slots) + 1, "kind": "image", "role": "实景", "asset_id": shared, "asset_ref": "a", "note": "彩排"})
        patched = demo.client.patch(
            f"{base}/api/v1/clips/{target['id']}",
            json={"ref_slots": slots},
            headers={"If-Match": f'"{target["version"]}"'},
        )
        assert patched.status_code == 200, patched.text
        beat.notes.append(f"clip={target['id']} 槽位={len(patched.json()['ref_slots'])}（演完需还原）")
        restore = demo.client.patch(
            f"{base}/api/v1/clips/{target['id']}",
            json={"ref_slots": original},
            headers={"If-Match": f'"{patched.json()["version"]}"'},
        )
        assert restore.status_code == 200, restore.text
        beat.notes.append("已还原")

    job_id = None
    with demo.beat("1:20 付费点：提交 → 确认") as beat:
        ep2 = demo.client.get(f"{base}/api/v1/projects/{PROJECT_ID}/episodes").json()[-1]
        clip = next(c for c in demo.client.get(f"{base}/api/v1/episodes/{ep2['id']}/clips").json() if c.get("prompt"))
        submitted = demo.client.post(
            f"{base}/api/v1/clips/{clip['id']}/generate?mode=default",
            json={"res": "draft", "budget_limit": "12.000000"},
            headers={"Idempotency-Key": f"rehearse-{int(time.time())}", "If-Match": f'"{clip["version"]}"'},
        )
        assert submitted.status_code == 202, submitted.text
        job = submitted.json()["job"]
        beat.notes.append(f"job={job['id']} 状态={job['state']} 预估=¥{submitted.json()['preview']['cost_est']}")
        assert job["state"] == "waiting_confirm"
        confirmed = demo.client.post(f"{base}/api/v1/jobs/{job['id']}/confirm")
        assert confirmed.status_code == 200, confirmed.text
        job_id = job["id"]
        peak = time.time()
        while time.time() - peak < 90:
            state = demo.client.get(f"{base}/api/v1/jobs/{job_id}").json()
            if state["state"] in ("done", "failed", "cancelled"):
                break
            time.sleep(1)
        beat.notes.append(f"终态={state['state']} attempts={state['attempts']} 花费=¥{state['cost_used']}")
        assert state["state"] in ("done", "failed")

    with demo.beat("1:45 质检结论 + 执行时间线") as beat:
        trace = demo.client.get(f"{base}/api/v1/jobs/{job_id}/trace").json()
        rows = trace if isinstance(trace, list) else trace["items"]
        steps = [entry["step"] for entry in rows]
        beat.notes.append(f"trace={steps[:8]}")
        assert "claim" in steps and "review" in steps

    with demo.beat("2:05 打回 → 重发 → 回滚（用彩排片段，不碰演示数据）") as beat:
        # 演示库里的 review 片段会因此被消耗（rejected 之后回不到 review），
        # 所以彩排用自己第 0:15 拍建的片段：先把状态推到 review。
        eid = getattr(demo, "rehearsal_eid", None)
        assert eid, "0:15 那拍没建出集"
        own = demo.client.get(f"{base}/api/v1/episodes/{eid}/clips").json()
        clip = own[0]
        if clip["status"] != "review":
            submitted = demo.client.post(
                f"{base}/api/v1/clips/{clip['id']}/generate?mode=default",
                json={"res": "draft", "budget_limit": "12.000000"},
                headers={"Idempotency-Key": f"rehearse-own-{int(time.time())}", "If-Match": f'"{clip["version"]}"'},
            )
            assert submitted.status_code == 202, submitted.text
            demo.client.post(f"{base}/api/v1/jobs/{submitted.json()['job']['id']}/confirm")
            import time as _time
            for _ in range(60):
                clip = demo.client.get(f"{base}/api/v1/clips/{clip['id']}").json()
                if clip["status"] not in ("generating", "draft", "prompted"):
                    break
                _time.sleep(0.5)
            if clip["status"] == "approved":  # 没有验收点会被放行；重开审批再演打回
                reopened = demo.client.patch(
                    f"{base}/api/v1/clips/{clip['id']}", json={"status": "review"},
                    headers={"If-Match": f'"{clip["version"]}"'},
                )
                assert reopened.status_code == 200, reopened.text
                clip = reopened.json()
        assert clip["status"] == "review", f"彩排片段没到 review：{clip['status']}"
        rejected = demo.client.patch(
            f"{base}/api/v1/clips/{clip['id']}", json={"status": "rejected"},
            headers={"If-Match": f'"{clip["version"]}"'},
        )
        assert rejected.status_code == 200, rejected.text
        beat.notes.append(f"reject → {rejected.json()['status']}")
        rerolled = demo.client.post(
            f"{base}/api/v1/clips/{clip['id']}/reroll",
            json={"res": "draft"},
            headers={"Idempotency-Key": f"rehearse-reroll-{int(time.time())}", "If-Match": f'"{rejected.json()["version"]}"'},
        )
        assert rerolled.status_code == 202, rerolled.text
        beat.notes.append(f"reroll job={rerolled.json()['job']['id']}")
        revisions = demo.client.get(f"{base}/api/v1/clips/{clip['id']}/revisions").json()["items"]
        target = next((r for r in revisions if r.get("output_asset_id")), None)
        assert target is not None, "没有带产物的 revision 可回滚"
        rolled = demo.client.post(f"{base}/api/v1/clips/{clip['id']}/rollback", json={"revision_id": target["id"]})
        assert rolled.status_code == 200, rolled.text
        beat.notes.append(f"rollback → revision={rolled.json()['current_revision_id']} 状态={rolled.json()['status']}")

    with demo.beat("2:30 资产库删除被拦 + 成本台账") as beat:
        blocked = demo.client.delete(f"{base}/api/v1/assets/6")
        assert blocked.status_code == 409, blocked.text
        usages = blocked.json()["error"]["detail"]["usages"]
        episodes = {u.get("episode_id") for u in usages}
        beat.notes.append(f"409 · 引用 {len(usages)} 处 · 涉及集 {episodes}")
        assert len(episodes) >= 2, "应看到跨集引用"
        cost = demo.client.get(f"{base}/api/v1/projects/{PROJECT_ID}/cost?group_by=episode").json()
        beat.notes.append(f"按集成本 keys={[i['key'] for i in cost['items']]}")

    with demo.beat("2:50 Skill 试跑（发布单向门）") as beat:
        demo.login(ADMIN)
        skills = demo.client.get(f"{base}/api/v1/skills").json()
        rows = skills if isinstance(skills, list) else skills.get("items", [])
        skill = next(s for s in rows if s.get("stage") == "S4")
        active_before = next(
            (v["version"] for v in demo.client.get(f"{base}/api/v1/skills/{skill['id']}/versions").json()
             if v["state"] == "active"),
            None,
        )
        # 每次用唯一正文 → 必然是**新版本**（否则会按 checksum 命中已有版本，甚至命中试跑过的版本）
        created = demo.client.post(
            f"{base}/api/v1/skills/{skill['id']}/versions",
            json={"body_md": f"## #110 巨物参照\n彩排 {int(time.time())}。", "note": "彩排"},
        )
        assert created.status_code == 201, created.text
        version = created.json()["skill_version"]["version"]
        pending = demo.client.post(f"{base}/api/v1/skills/{skill['id']}/versions/{version}/publish")
        assert pending.status_code == 409 and pending.json()["error"]["code"] == "SKILL_NOT_DRYRUN", pending.text
        beat.notes.append("未试跑就发布 → 409 SKILL_NOT_DRYRUN")
        ep1_clip = demo.client.get(f"{base}/api/v1/episodes/{getattr(demo, 'rehearsal_eid')}/clips").json()[0]
        dry = demo.client.post(
            f"{base}/api/v1/skills/{skill['id']}/versions/{version}/dry-run",
            json={"clip_id": ep1_clip["id"]},
        )
        assert dry.status_code == 200, dry.text
        active_after = next(
            (v["version"] for v in demo.client.get(f"{base}/api/v1/skills/{skill['id']}/versions").json()
             if v["state"] == "active"),
            None,
        )
        assert active_after == active_before, f"彩排不该改 active（{active_before} → {active_after}）"
        beat.notes.append(f"试跑通过 v{version} · active 仍为 v{active_before}")

    with demo.beat("备用 A 止血开关（停用→503→启用）") as beat:
        demo.login(ADMIN)
        off = demo.client.post(f"{base}/api/v1/admin/providers/text/state", json={"enabled": False, "reason": "彩排"})
        assert off.status_code == 200, off.text
        demo.login(OWNER)
        blocked = demo.client.post(f"{base}/api/v1/projects/{PROJECT_ID}/steps/S1/run")
        assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "PROVIDER_DISABLED", blocked.text
        beat.notes.append("停用后 S1 → 503 PROVIDER_DISABLED")
        demo.login(ADMIN)
        on = demo.client.post(f"{base}/api/v1/admin/providers/text/state", json={"enabled": True})
        assert on.status_code == 200 and on.json()["text"]["enabled"] is True, on.text
        beat.notes.append("已启用")

    print(f"{'拍':<40}{'结果':<6}{'耗时':>8}  说明")
    for item in demo.beats:
        mark = "OK" if item.ok else "FAIL"
        print(f"{item.name:<40}{mark:<6}{item.ms:>6}ms  {' · '.join(item.notes)}")
    failed = [item for item in demo.beats if not item.ok]
    print()
    print(f"{'✅ 彩排通过' if not failed else f'❌ {len(failed)} 拍失败'}（{sum(i.ms for i in demo.beats)} ms）")
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8040")
    args = parser.parse_args()
    return rehearsal(args.api)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
