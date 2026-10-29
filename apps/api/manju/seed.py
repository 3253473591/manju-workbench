"""把 `demo/seed/*.json` 灌进 MySQL —— `01` §8 的落库侧。

天然键幂等：重跑不会多出 clip，也不会往 append-only 的 `revision` 里追加第二份。
产物只登记相对路径 + sha256，不搬二进制。
时间一律存 UTC 的 naive `DATETIME(3)`（`00` §3.2）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from re import match
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import clip_rules
from manju.models import Asset, Clip, Episode, Project, Revision, User
from manju.security import hash_password

SEED_OWNER_EMAIL = "seed@example.com"
SEED_MODEL = "seedance-2.0-mini"
# seed 所有者是演示用的可写账号（项目的 owner 就是它）。口令写死在这里是有意的：
# 这是 Demo，README 要能直接给出可用账号；换真实部署前必须改。
SEED_OWNER_PASSWORD = "seedpass12345"

THUMB_DIR = ".thumbs"
THUMB_MAX_EDGE = 720


def load_seed(session: Session, seed_dir: Path, media_root: Path, *, thumbs: bool = False) -> dict[str, int]:
    media_root = media_root.resolve()
    stats = {
        "project": 0,
        "episode": 0,
        "clip": 0,
        "asset": 0,
        "revision": 0,
        "revision_skipped": 0,
        "slots_resolved": 0,
        "slots_unresolved": 0,
        "clips_with_output": 0,
        "output_missing": 0,
        "assets_missing": 0,
        "thumbs_made": 0,
        "thumbs_reused": 0,
        "thumbs_failed": 0,
    }
    owner = _owner(session)
    project = _project(session, owner, _read(seed_dir / "project.json"), stats)

    episodes: dict[int, Episode] = {}
    for payload in _read(seed_dir / "episodes.json"):
        episode = _episode(session, project, payload, stats)
        episodes[payload["idx"]] = episode

    assets = _assets(session, project, _read(seed_dir / "assets.json"), media_root, stats)
    if thumbs:
        _generate_thumbs(assets, media_root, stats)

    revisions_by_clip: dict[str, list[dict]] = {}
    for payload in _read(seed_dir / "revisions.json"):
        revisions_by_clip.setdefault(payload["clip_key"], []).append(payload)

    for payload in _read(seed_dir / "clips.json"):
        episode_idx = int(match(r"ep(\d+)\.", payload["clip_key"]).group(1))
        episode = episodes[episode_idx]
        clip = _clip(session, episode, payload, assets, stats)
        _revisions(session, clip, revisions_by_clip.get(payload["clip_key"], []), stats)
        _link_output(session, clip, payload, assets, stats)

    episode.status = "done"
    session.commit()
    return stats


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _owner(session: Session) -> User:
    user = session.scalar(select(User).where(User.email == SEED_OWNER_EMAIL))
    if user is None:
        user = User(
            email=SEED_OWNER_EMAIL,
            pwd_hash=hash_password(SEED_OWNER_PASSWORD),
            role="lead",
            is_active=1,
            created_at=_utc_now(),
        )
        session.add(user)
        session.flush()
    elif not user.pwd_hash.startswith("$argon2"):
        # 早期版本把 seed 所有者的哈希写成了不可用占位值（`!seed-no-login`），
        # 那样它就没法登录；这里补成真实口令，其他情况不覆盖已有口令。
        user.pwd_hash = hash_password(SEED_OWNER_PASSWORD)
    return user


def _project(session: Session, owner: User, payload: dict, stats: dict[str, int]) -> Project:
    project = session.scalar(
        select(Project).where(Project.owner_id == owner.id, Project.title == payload["title"])
    )
    if project is None:
        project = Project(
            owner_id=owner.id,
            title=payload["title"],
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        session.add(project)
        session.flush()
        stats["project"] += 1
    project.genre = payload.get("genre")
    project.tone = payload.get("tone")
    project.logline = payload.get("logline")
    return project


def _episode(session: Session, project: Project, payload: dict, stats: dict[str, int]) -> Episode:
    episode = session.scalar(
        select(Episode).where(Episode.project_id == project.id, Episode.idx == payload["idx"])
    )
    if episode is None:
        episode = Episode(
            project_id=project.id,
            idx=payload["idx"],
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        session.add(episode)
        session.flush()
        stats["episode"] += 1
    episode.title = payload.get("title")
    episode.synopsis = payload.get("synopsis")
    episode.status = payload.get("status", "storyboarded")
    return episode


def _assets(
    session: Session, project: Project, payloads: list[dict], media_root: Path, stats: dict[str, int]
) -> dict[str, Asset]:
    """返回 basename → Asset，供参考槽位与交付成片回填 asset_id。"""
    root = media_root.resolve()
    known = {asset.sha256: asset for asset in session.scalars(select(Asset).where(Asset.project_id == project.id))}
    by_name: dict[str, Asset] = {}
    for payload in payloads:
        path = (root / payload["rel_path"]).resolve()
        if root not in path.parents:
            raise ValueError(f"asset 越出 MEDIA_ROOT：{payload['rel_path']}")
        if not path.is_file():
            stats["assets_missing"] += 1
            continue
        digest = _sha256(path)
        asset = known.get(digest)
        if asset is None:
            asset = Asset(
                project_id=project.id,
                kind=payload["kind"],
                path=payload["rel_path"],
                sha256=digest,
                bytes=path.stat().st_size,
                source="seed",
                created_at=_utc_now(),
            )
            session.add(asset)
            session.flush()
            known[digest] = asset
            stats["asset"] += 1
        by_name[path.name.lower()] = asset
    return by_name


def _find_asset(assets: dict[str, Asset], name: str | None) -> Asset | None:
    """按文件主干（去扩展名）找资产：剧本写 `00人物-徒弟-喜剧版-猪圈接生`，磁盘上是同名 `.png`。"""
    if not name:
        return None
    stem = Path(name).stem.lower()
    for filename, asset in assets.items():
        if Path(filename).stem == stem:
            return asset
    return None


def _probe_size(source: Path) -> tuple[int, int] | None:
    """ffprobe 读原片宽高。读不到就返回 None（尺寸未知不影响出海报）。"""
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(source),
    ]
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
        )
        width, height = result.stdout.strip().split("x")[:2]
        return int(width), int(height)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return None


class Poster(NamedTuple):
    relative: str
    width: int | None
    height: int | None
    thumb_w: int | None
    thumb_h: int | None


def poster(media_root: Path, asset: Asset, source: Path) -> Poster | None:
    """抽首帧/缩放当海报（长边 ≤720），返回相对 `MEDIA_ROOT` 的路径与两组尺寸。

    视频先 seek 0.5 s 取一帧（避开黑场），**图片直接缩放** —— 静态参考图以前没有海报，
    `?thumb=1` 会回落成原图，实测一张人物立绘 9.6 MB。
    抽不出来就返回 `None`：一张海报失败不该让整个种子导入失败，取图端点会回落到原片。
    写到 `MEDIA_ROOT/.thumbs/<sha前12位>.jpg` —— 隐藏目录、只增不改，不碰原始素材。
    """
    folder = media_root / THUMB_DIR
    folder.mkdir(exist_ok=True)
    target = folder / f"{asset.sha256[:12]}.jpg"
    command = ["ffmpeg", "-y", "-loglevel", "error"]
    if asset.kind == "video":
        command += ["-ss", "0.5"]
    command += [
        "-i", str(source),
        "-frames:v", "1",
        "-vf",
        f"scale='min({THUMB_MAX_EDGE},iw)':'min({THUMB_MAX_EDGE},ih)':force_original_aspect_ratio=decrease",
        "-q:v", "3", str(target),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    if not target.is_file() or target.stat().st_size == 0:
        return None
    size = _probe_size(source)
    thumb_w = thumb_h = None
    if size is not None:
        width, height = size
        ratio = min(1.0, THUMB_MAX_EDGE / max(width, height))
        thumb_w, thumb_h = round(width * ratio), round(height * ratio)
    return Poster(
        relative=f"{THUMB_DIR}/{target.name}",
        width=size[0] if size else None,
        height=size[1] if size else None,
        thumb_w=thumb_w,
        thumb_h=thumb_h,
    )


def _generate_thumbs(assets: dict[str, Asset], media_root: Path, stats: dict[str, int]) -> None:
    for asset in assets.values():
        # 图片也要海报：资产库/槽位面板按 `thumb=1` 取图，没有缩略图就会回落下发原图
        if asset.kind not in ("video", "image"):
            continue
        if asset.thumb_path and (media_root / asset.thumb_path).is_file():
            # 海报已在：只补尺寸（前端要拿宽高留占位），不重新渲染
            if asset.thumb_w is None:
                size = _probe_size(media_root / asset.path)
                if size is not None:
                    asset.w, asset.h = size
                    ratio = min(1.0, THUMB_MAX_EDGE / max(size))
                    asset.thumb_w, asset.thumb_h = round(size[0] * ratio), round(size[1] * ratio)
            stats["thumbs_reused"] += 1
            continue
        result = poster(media_root, asset, media_root / asset.path)
        if result is None:
            stats["thumbs_failed"] += 1
            continue
        asset.thumb_path = result.relative
        asset.w, asset.h = result.width, result.height
        asset.thumb_w, asset.thumb_h = result.thumb_w, result.thumb_h
        stats["thumbs_made"] += 1


def _clip(
    session: Session, episode: Episode, payload: dict, assets: dict[str, Asset], stats: dict[str, int]
) -> Clip:
    clip = session.scalar(select(Clip).where(Clip.episode_id == episode.id, Clip.idx == payload["idx"]))
    slots = _slots(payload["ref_slots"], assets, stats)
    # seed 写的是状态为 approved 的成片，所以按"分镜已完成"的口径校验（shots 必须完整）
    clip_rules.enforce_clip(
        session,
        project_id=episode.project_id,
        duration_s=payload["duration_s"],
        shots=payload["shots"],
        ref_slots=slots,
        dialogue=payload["dialogue"],
        acceptance=payload["acceptance"],
        prompt=payload["prompt"],
        require_shots=True,
    )
    if clip is None:
        # seed 导入的是已完成的真实成片，所以直接落 approved；线上流程仍从 draft 走状态机
        clip = Clip(
            episode_id=episode.id,
            idx=payload["idx"],
            source_idx=payload["source_idx"],
            duration_s=Decimal(payload["duration_s"]),
            status="approved",
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        session.add(clip)
        session.flush()
        stats["clip"] += 1
    clip.prompt = payload["prompt"]
    clip.negative = payload["negative"]
    clip.title = payload["title"]
    clip.shots_json = payload["shots"]
    clip.dialogue_json = payload["dialogue"]
    clip.acceptance_json = payload["acceptance"]
    clip.ref_slots_json = slots
    return clip


def _slots(payloads: list[dict], assets: dict[str, Asset], stats: dict[str, int]) -> list[dict]:
    """参考槽位。优先用剧本参考资产回填出来的真实文件（`script_ref`），
    其次才是 `_krea2_pipeline` 的作业副本名（`sk1_scene.png`，本目录里没有）。

    解析不出 `asset_id` 的槽位保留 `asset_ref` 占位，等资产库按导出作业回填（`01` §8.4 第 4 条）。
    """
    out = []
    for slot in payloads:
        asset = _find_asset(assets, slot.get("script_ref")) or _find_asset(assets, slot["asset_ref"])
        if asset is None:
            stats["slots_unresolved"] += 1
        else:
            stats["slots_resolved"] += 1
        out.append(
            {
                "idx": slot["idx"],
                "kind": slot["kind"],
                "asset_id": asset.id if asset else None,
                "asset_ref": slot["asset_ref"],
                "script_ref": slot.get("script_ref"),
                "role": slot["role"],
                "note": slot["note"],
            }
        )
    return out


def _revisions(session: Session, clip: Clip, payloads: list[dict], stats: dict[str, int]) -> None:
    seen = set()
    for params in session.scalars(
        select(Revision.params_json).where(
            Revision.target_kind == "clip", Revision.target_id == clip.id
        )
    ):
        params = params or {}
        seen.add(params.get("task_id") or params.get("version"))

    ordered = sorted(payloads, key=_version_order)
    latest: Revision | None = None
    for payload in ordered:
        key = payload.get("task_id") or payload.get("version_tag")
        if key in seen:
            stats["revision_skipped"] += 1
            continue
        revision = Revision(
            target_kind="clip",
            target_id=clip.id,
            negative=clip.negative,
            params_json={
                "res": payload.get("resolution"),
                "task_id": payload.get("task_id"),
                "version": payload.get("version_tag"),
                "mode": "draft",
            },
            provider="seedance",
            model_requested=SEED_MODEL,
            model_used=payload.get("model_used") or SEED_MODEL,
            input_tokens=payload.get("input_tokens"),
            video_seconds=Decimal(payload["video_seconds"]) if payload.get("video_seconds") else None,
            cost=Decimal(payload["cost"]) if payload.get("cost") else Decimal(0),
            latency_ms=payload.get("latency_ms"),
            motion_json=payload.get("motion"),
            acceptance_result_json=payload.get("acceptance_result") or None,
            parent_revision_id=latest.id if latest else None,
            created_at=_task_time(payload.get("task_id")),
        )
        revision.unit_price = _unit_price(revision)
        clip_rules.enforce_cost(
            revision.cost, revision.unit_price, revision.video_seconds, revision.input_tokens
        )
        session.add(revision)
        session.flush()
        latest = revision
        stats["revision"] += 1

    if latest is not None:
        # 只有最新版留提示词全文：更早版本的正文在真实数据里已不可得，编一份假的更糟
        latest.prompt = clip.prompt
        clip.current_revision_id = latest.id


def _find_delivered(assets: dict[str, Asset], stem: str) -> Asset | None:
    """交付成片的文件名可能带变体：`s3b_…`（替换件）、`s1-V2_…`（第二集重出）。

    优先精确 `<stem>_`，其次 `<stem>` + 单个字母/连字符；`s10_` 不会被 `s1` 抢走。
    """
    exact: list[Asset] = []
    variant: list[Asset] = []
    for name, asset in assets.items():
        if asset.kind != "video" or not name.startswith(stem):
            continue
        rest = name[len(stem):]
        if rest.startswith("_"):
            exact.append(asset)
        elif rest[:1] in {"-", "a", "b", "c", "v"}:
            variant.append(asset)
    return exact[0] if exact else (variant[0] if variant else None)


def _link_output(
    session: Session, clip: Clip, payload: dict, assets: dict[str, Asset], stats: dict[str, int]
) -> None:
    """把交付成片挂到最新一次 revision 上：`clip → current_revision → output_asset`。

    交付名主干由解析器给出（`s7` / `s6b`），比按标题猜文件可靠 —— 片段8 是插入镜，
    文件名主干是 `s6b`，但它的源片段号是 8、播放序是 7，三种编号都不能互相推。

    没有 take 记录的片段（第二集的终剪文档没有 revision 段）补一条 0 成本的 revision：
    成片确实存在，镜头墙该看得到；`params_json.cost_unknown` 标清"这一条没有实测成本"。
    """
    stem = (payload.get("delivered_stem") or "").lower()
    if not stem:
        return
    asset = _find_delivered(assets, stem)
    if asset is None:
        stats["output_missing"] += 1
        return
    revision = session.get(Revision, clip.current_revision_id) if clip.current_revision_id else None
    if revision is None:
        revision = Revision(
            target_kind="clip",
            target_id=clip.id,
            prompt=payload.get("prompt"),
            negative=payload.get("negative"),
            params_json={"source": "seed_delivered", "cost_unknown": True},
            provider="seed",
            cost=Decimal(0),
            created_at=_utc_now(),
        )
        session.add(revision)
        session.flush()
        clip.current_revision_id = revision.id
        stats["revision"] += 1
    revision.output_asset_id = asset.id
    asset.revision_id = revision.id
    stats["clips_with_output"] += 1


def _probe_duration(source: Path) -> Decimal | None:
    """ffprobe 读时长（`dur_s`）。真 provider 出片后靠它回填，读不到就留空。"""
    command = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(source),
    ]
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
        )
        return Decimal(result.stdout.strip()).quantize(Decimal("0.01"))
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return None


def enrich_asset(asset: Asset, media_root: Path) -> None:
    """给刚入仓的资产补**宽高 / 时长 / 海报**。

    真 provider 出片后若不做这一步：镜头墙的 `?thumb=1` 会 404（前端裂图），成本看板的
    "秒数"栏也是空的 —— 这是接真视频通道时才暴露的缺口，mock 走 seed 早就补过了。
    ffmpeg/ffprobe 缺失时全部留空，不阻断链路。
    """
    source = media_root / asset.path
    if not source.is_file():
        return
    if asset.kind == "video":
        size = _probe_size(source)
        if size is not None:
            asset.w, asset.h = size
        if asset.dur_s is None:
            asset.dur_s = _probe_duration(source)
    if asset.thumb_path and (media_root / asset.thumb_path).is_file():
        return
    result = poster(media_root, asset, source)
    if result is None:
        return
    asset.thumb_path = result.relative
    asset.w = asset.w or result.width
    asset.h = asset.h or result.height
    asset.thumb_w, asset.thumb_h = result.thumb_w, result.thumb_h


def _version_order(payload: dict) -> int:
    tag = payload.get("version_tag") or ""
    return int(tag[1:]) if tag[:1] == "v" and tag[1:].isdigit() else 99


def _unit_price(revision: Revision) -> Decimal | None:
    if revision.cost and revision.video_seconds:
        return (revision.cost / revision.video_seconds).quantize(Decimal("0.000001"))
    return None


def _task_time(task_id: str | None) -> datetime:
    """`cgt-20261007125206-oP769` 里的时间就是真实提交时刻，比 `now()` 诚实得多。"""
    stamp = match(r"cgt-(\d{8})(\d{6})-", task_id or "")
    if stamp:
        return datetime.strptime(stamp.group(1) + stamp.group(2), "%Y%m%d%H%M%S")
    return _utc_now()
