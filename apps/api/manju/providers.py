"""provider 适配层（`00` §3.1）。

这一层**不认识 clip/project**：只认"一次调用 + 参数 + 预算"，回报用量与产物地址。
所有上游差异（鉴权、重试、限流、usage 字段名）都吸收在这里，上层拿到的是统一的
`ProviderResult`（`00` §3.1 的"normalize at the boundary"）。

三种实现，取舍写明白：

- `mock`：零成本回放，按实测常数折算 tokens/成本，产物指向已有成片 —— 演示链路是真的，只是不花钱；
- `openai`：**任何 OpenAI 兼容的 `/chat/completions`** 都能接（OpenAI / DeepSeek / 通义 / vLLM …），
  `text()` 与 `vision()` 是真调用；**`video()` 明确不实现**：视频厂商的接口形态差异太大
  （task 型 / 回调型 / 直出二进制），猜一个比不猜更糟 —— job 会落 `PROVIDER_UNAVAILABLE` 说清楚原因；
- 真视频通道落地时新增一个类、在 `make_provider` 里注册即可，上层不改。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import httpx

from manju.config import Settings
from manju.constants import TOKENS_PER_VIDEO_SECOND

# 实测单价（`01` §8.5：12 s / 2.27 元 → 0.189167 元/秒）。真 provider 由配置下发。
MOCK_UNIT_PRICE = Decimal("0.189167")
MOCK_MODEL = "seedance-2.0-mini"

# mock 的视觉判定是**预设表**，不是真看懂了图：真 provider 读联络图，mock 只按判据关键词
# 回放结论，每条 evidence 都带 `[mock]` 前缀 —— 验收结果里一眼能看出这不是模型判的（`02` §11.1）。
MOCK_VISION_RULES = (
    (("功德环", "光环", "光效"), "pass", "金色功德环贯穿可见"),
    (("巨物", "压迫感", "体量"), "pass", "主体体量明显大于人"),
    (("字幕", "文字", "水印"), "fail", "画面出现烧入文字"),
)


@dataclass(frozen=True)
class ProviderResult:
    model: str
    input_tokens: int
    output_tokens: int
    video_seconds: Decimal | None
    unit_price: Decimal | None
    cost: Decimal
    latency_ms: int
    output_path: str | None = None  # 相对 MEDIA_ROOT；真实 provider 给的是签名直链
    motion: dict | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TextResult:
    model: str
    payload: dict
    input_tokens: int
    output_tokens: int
    cost: Decimal
    latency_ms: int
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ImageResult:
    """一次出图。`path` 是相对 `MEDIA_ROOT` 的落盘位置，`source` 说明是真出图还是回放。"""

    model: str
    path: str
    bytes: int
    width: int | None
    height: int | None
    cost: Decimal
    latency_ms: int
    source: str = "gen"
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VisionResult:
    """一次看图判定。`verdicts` 只给有结论的判据，判不了的由实现方略过。"""

    model: str
    verdicts: list[dict]
    input_tokens: int
    cost: Decimal
    latency_ms: int
    raw: dict = field(default_factory=dict)


class Provider:
    """协议：一次视频生成 + 一次文本结构化生成 + 一次看图判定。实现方可以抛 `ProviderError`。"""

    name = "base"

    def estimate(self, seconds: Decimal) -> Decimal:
        """预估成本，用于付费点前的展示与预算预判（不发起调用）。"""
        raise NotImplementedError

    def video(self, *, prompt: str, seconds: Decimal, model: str) -> ProviderResult:
        raise NotImplementedError

    def text(self, *, stage: str, context: str, format_hint: str = "") -> TextResult:
        """返回**结构化** payload。解析与校验在上层（steps），这里只负责拿到东西。

        `format_hint` 是上层给模型的**输出契约**（pydantic schema 文本）。真模型不给契约就会
        自己包一层信封（实测 DeepSeek 首次返回 `{clip, edit, output, project, stage, …}`），
        mock 用不到、直接忽略。
        """
        raise NotImplementedError

    def vision(self, *, image: Path, criteria: list[dict]) -> VisionResult:
        """看图判 `criteria`。`image` 是一张联络图，视频抽帧是上层的事。"""
        raise NotImplementedError

    def image(self, *, prompt: str, model: str | None = None) -> ImageResult:
        """出一张图（人物卡/参考图）。产物必须落进 `MEDIA_ROOT`，上层只管用它建 asset。"""
        raise NotImplementedError


class ProviderError(Exception):
    """上游失败。`retryable` 决定 jobs 层是退避重试还是直接失败（`03` §3 矩阵）。"""

    def __init__(self, message: str, *, retryable: bool, status: int | None = None, raw: dict | None = None):
        self.retryable = retryable
        self.status = status
        self.raw = raw or {}
        super().__init__(message)


class MockProvider(Provider):
    """零成本 provider：用量按 `TOKENS_PER_VIDEO_SECOND` 真实常数算，产物指向已有成片。

    `fixture` 由 `make_provider` 从 MEDIA_ROOT 里挑一个真实 mp4 —— provider 自己不看
    项目结构，只认"有个文件可以当产物"。
    """

    name = "mock"

    def __init__(self, fixture: Path | None, motion: dict | None = None, media_root: Path | None = None):
        self.fixture = fixture
        self.media_root = media_root
        self.motion = motion or {"samples": 23, "freeze": 0, "avg": 29.82, "max": 51.20}

    def estimate(self, seconds: Decimal) -> Decimal:
        return (MOCK_UNIT_PRICE * seconds).quantize(Decimal("0.000001"))

    def text(self, *, stage: str, context: str, format_hint: str = "") -> TextResult:
        """Mock **不做创作**：它回放与 stage 匹配的结构，并把上下文的指纹带回去。

        这样能验证"上下文确实被组装进来了"（每条输出都带 `echo`），又不用假装它会写剧本。
        `echo` 取第一条**正文**（跳过 `#` 开头的标题行），否则回放出来的文本会是一串标题。
        """
        head = next(
            (line.strip()[:60] for line in context.splitlines() if line.strip() and not line.startswith("#")),
            "",
        )
        payloads = {
            "S1": {"logline": f"{head}｜一句话概述", "genre": "修仙", "tone": "荒诞反差喜剧"},
            "S2": {"synopsis": f"【mock】{head} 的分集剧情：按场景推进，每场给出动作与对白。"},
            "S3": {
                "clips": [
                    {
                        "idx": 1,
                        "title": f"{head}·片段一",
                        "duration_s": "6.00",
                        "shots": [
                            {"no": 1, "start": "0", "end": "3.00", "desc": "远景急推，交代环境"},
                            {"no": 2, "start": "3.00", "end": "6.00", "desc": "贴脸特写，情绪落点"},
                        ],
                        "dialogue": [{"speaker": "徒弟", "text": "退钱", "shot_no": 2, "on_screen": True}],
                        "acceptance": [{"id": "a1", "criterion": "开场不是定镜"}],
                    }
                ]
            },
            "S4": {
                "prompt": f"{head}\n镜头1（0-3秒）：…\n镜头2（3-6秒）：…「退钱」",
                "negative": "无字幕，避免生成任何文字或字幕",
            },
            "S5": {"characters": [{"name": "徒弟", "anchor": "玄黑滚金边魔袍、亮蓝手套、脑后金色功德环"}]},
        }
        payload = payloads.get(stage, {"echo": head})
        return TextResult(
            model="mock-text",
            payload=payload,
            input_tokens=max(1, len(context) // 2),
            output_tokens=len(str(payload)) // 2,
            cost=Decimal(0),  # 文本单价还没定（03 §5 待实测），先按 0 记账但保留 token
            latency_ms=len(context) // 20,
            raw={"provider": "mock", "echo": head},
        )

    def video(self, *, prompt: str, seconds: Decimal, model: str) -> ProviderResult:
        tokens = int(TOKENS_PER_VIDEO_SECOND * float(seconds))
        cost = (MOCK_UNIT_PRICE * seconds).quantize(Decimal("0.000001"))
        return ProviderResult(
            model=model or MOCK_MODEL,
            input_tokens=tokens,
            output_tokens=tokens,
            video_seconds=seconds,
            unit_price=MOCK_UNIT_PRICE,
            cost=cost,
            latency_ms=int(seconds) * 15_000,  # 实测约 3 分钟/12 s
            output_path=str(self.fixture) if self.fixture else None,
            motion=self.motion,
            raw={"provider": "mock", "note": "零成本回放，用量按实测常数折算"},
        )

    def vision(self, *, image: Path, criteria: list[dict]) -> VisionResult:
        """按预设表回放判定。命中不了任何词就 `unknown` —— 半懂装懂比不判更糟。"""
        verdicts = []
        for item in criteria:
            text = item.get("criterion", "")
            landed = next(
                (rule for rule in MOCK_VISION_RULES if any(word in text for word in rule[0])), None
            )
            if landed is None:
                verdicts.append({"id": item["id"], "result": "unknown", "evidence": "[mock] 无预设判据"})
            else:
                verdicts.append({"id": item["id"], "result": landed[1], "evidence": f"[mock] {landed[2]}"})
        return VisionResult(
            model="mock-vision",
            verdicts=verdicts,
            input_tokens=max(1, sum(len(item.get("criterion", "")) for item in criteria) // 2),
            cost=Decimal(0),
            latency_ms=300,
            raw={"provider": "mock", "image": image.name},
        )

    def image(self, *, prompt: str, model: str | None = None) -> ImageResult:
        """mock 出图 = **指向**一张已有素材，不复制文件。

        早期实现会先 cp 一份到 `.gen/`，但入仓时按 sha256 去重会命中那张原素材，
        于是每次 mock 出图都在 `.gen/` 留一个没人引用的孤儿文件（实测）。直接返回原素材的
        相对路径，`source="mock"` + `raw.replay` 说清是回放。
        """
        if self.media_root is None:
            raise ProviderError("mock 出图需要 media_root", retryable=False)
        source = _find_reference_image(self.media_root)
        if source is None:
            raise ProviderError("mock 出图找不到可复用的素材（MEDIA_ROOT/00_静态资源 下没有图）", retryable=False)
        payload = source.read_bytes()
        return ImageResult(
            model="mock-image",
            path=source.relative_to(self.media_root).as_posix(),
            bytes=len(payload),
            width=None,
            height=None,
            cost=Decimal(0),
            latency_ms=120,
            source="mock",
            raw={"provider": "mock", "replay": source.name, "prompt": prompt[:120]},
        )


class OpenAICompatProvider(Provider):
    """OpenAI 兼容 `/chat/completions` 的真实调用（text + vision）。

    **只依赖协议、不依赖厂商**：base_url 指到哪家都行，模型名由配置给。没配的模型不会被猜。
    计费口径：文本/视觉的单价还没定（`03` §5 待实测），所以 `cost` 记 0 且 `unit_price=None` ——
    宁可缺价格，不可编价格；`input_tokens`/`output_tokens` 照上游 usage 记真的。
    """

    name = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        text_model: str,
        vision_model: str = "",
        timeout: int = 120,
        unit_price: Decimal | None = None,
        client_factory: Callable[..., httpx.Client] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.text_model = text_model
        self.vision_model = vision_model or text_model
        self.timeout = timeout
        self.unit_price = unit_price
        self._client_factory = client_factory or httpx.Client

    # ---------------------------------------------------------------- 视频：明确不做

    def estimate(self, seconds: Decimal) -> Decimal:
        return (self.unit_price * seconds).quantize(Decimal("0.000001")) if self.unit_price else Decimal(0)

    def video(self, *, prompt: str, seconds: Decimal, model: str) -> ProviderResult:
        raise ProviderError(
            "视频通道尚未接入：provider=openai 只实现 text/vision。"
            "把视频厂商接进 providers.OpenAICompatProvider 之外的适配类（或先用 PROVIDER=mock 演示）",
            retryable=False,
        )

    # ---------------------------------------------------------------- 文本

    def text(self, *, stage: str, context: str, format_hint: str = "") -> TextResult:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是漫剧工作台的文本 worker。只输出一个 JSON 对象，不要解释、不要代码围栏。"
                    "对象的字段必须**严格**符合用户给出的 JSON Schema：不要塞 type/response_format/"
                    "stage/model 之类的元信息字段，也不要包一层 data/output 信封。"
                ),
            },
            {
                "role": "user",
                "content": f"stage={stage}\n\n{context}" + (f"\n\n---\n输出契约（必须严格遵守，不要包信封）：\n{format_hint}" if format_hint else ""),
            },
        ]
        payload, usage, latency_ms = self._chat(messages, model=self.text_model, json_mode=True)
        return TextResult(
            model=self.text_model,
            payload=payload,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cost=Decimal(0),
            latency_ms=latency_ms,
            raw={"provider": self.name, "usage": usage},
        )

    # ---------------------------------------------------------------- 视觉

    def vision(self, *, image: Path, criteria: list[dict]) -> VisionResult:
        instruction = (
            "下面是一张由视频均匀抽帧拼成的联络图。请逐条判断验收点是否成立，"
            "只输出 JSON：{\"verdicts\":[{\"id\":\"...\",\"result\":\"pass|fail|unknown\",\"evidence\":\"...\"}]}。"
            "看不出来就判 unknown，不要猜。\n"
            + "\n".join(f"- id={item['id']}：{item['criterion']}" for item in criteria)
        )
        messages = [
            {"role": "system", "content": "你是严格但保守的视觉验收员。只输出 JSON。"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url", "image_url": {"url": _data_url(image)}},
                ],
            },
        ]
        payload, usage, latency_ms = self._chat(messages, model=self.vision_model, json_mode=True)
        return VisionResult(
            model=self.vision_model,
            verdicts=_clean_verdicts(payload),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            cost=Decimal(0),
            latency_ms=latency_ms,
            raw={"provider": self.name, "usage": usage},
        )

    # ---------------------------------------------------------------- 传输层

    def _chat(self, messages: list[dict], *, model: str, json_mode: bool) -> tuple[dict, dict, int]:
        body: dict = {"model": model, "messages": messages, "temperature": 0.2}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            with self._client_factory(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"上游超时（>{self.timeout}s）", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"上游连接失败：{type(exc).__name__}", retryable=True) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code in (401, 403):
            raise ProviderError("上游鉴权失败（检查 PROVIDER_API_KEY）", retryable=False, status=response.status_code)
        if response.status_code == 429:
            raise ProviderError("上游限流", retryable=True, status=429, raw=_retry_after(response))
        if response.status_code >= 500:
            raise ProviderError(f"上游 {response.status_code}", retryable=True, status=response.status_code)
        if response.status_code >= 400:
            raise ProviderError(
                f"上游拒绝了请求（{response.status_code}）", retryable=False, status=response.status_code,
                raw={"body": response.text[:400]},
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise ProviderError("上游返回结构不认识", retryable=False, raw={"body": response.text[:400]}) from exc
        usage = data.get("usage") or {}
        return _parse_json(content), usage, latency_ms


class ArkVideoProvider(Provider):
    """火山方舟 Seedance 真出片（`/contents/generations/tasks`，task 型 + 轮询）。

    实测口径（2026-10-19，2.0mini / 480p / 4 s / 含音轨）：
    创建 → `{"id": "cgt-…"}`；轮询 8 次（约 80 s）后 `succeeded`；
    `content.video_url` 是 24 h 有效的签名直链；**计费按 token**：`usage.completion_tokens = 40594`。
    所以成本记 `tokens × 元/千token`，`unit_price=None` —— 计费口径不是元/秒，不要造一个假的秒价。

    上游给什么就记什么：`duration`/`resolution`/`ratio`/`fps` 都从任务详情回读，
    不按我们请求时的想法写。
    """

    name = "ark"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        resolution: str = "720p",
        price_per_1k_tokens: Decimal = Decimal("0.008727"),
        media_root: Path | None = None,
        poll_interval: int = 5,
        timeout: int = 900,
        client_factory: Callable[..., httpx.Client] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.resolution = resolution
        self.price_per_1k_tokens = price_per_1k_tokens
        self.media_root = media_root
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._client_factory = client_factory or httpx.Client

    # ---------------------------------------------------------------- 预算预估

    def estimate(self, seconds: Decimal) -> Decimal:
        """按 720p 的实测常数估：`TOKENS_PER_VIDEO_SECOND × 秒 × 元/千token`。

        480p 实际约为一半，也就是**估得偏高** —— 预算前置宁可偏高，不能先花再发现穿线。
        """
        tokens = Decimal(TOKENS_PER_VIDEO_SECOND) * seconds
        return (tokens / 1000 * self.price_per_1k_tokens).quantize(Decimal("0.000001"))

    # ---------------------------------------------------------------- 文本/视觉不在这条通道

    def text(self, *, stage: str, context: str, format_hint: str = "") -> TextResult:
        raise ProviderError("provider=ark 只做视频；文本请用 PROVIDER=openai", retryable=False)

    def vision(self, *, image: Path, criteria: list[dict]) -> VisionResult:
        raise ProviderError("provider=ark 只做视频；视觉请用 PROVIDER=openai", retryable=False)

    # ---------------------------------------------------------------- 出片

    def video(self, *, prompt: str, seconds: Decimal, model: str) -> ProviderResult:
        task_id = self._create(prompt, seconds)
        payload = self._wait(task_id)
        url = ((payload.get("content") or {}).get("video_url")) or ""
        if not url:
            raise ProviderError("任务成功但没有 video_url", retryable=False, raw={"task": task_id})
        path = self._download(url, task_id)
        usage = payload.get("usage") or {}
        tokens = int(usage.get("completion_tokens") or usage.get("total_tokens") or 0)
        cost = (Decimal(tokens) / 1000 * self.price_per_1k_tokens).quantize(Decimal("0.000001"))
        duration = payload.get("duration")
        return ProviderResult(
            model=str(payload.get("model") or self.model),
            input_tokens=tokens,
            output_tokens=tokens,
            video_seconds=Decimal(str(duration)) if duration is not None else seconds,
            unit_price=None,  # 按 token 计费，元/秒是伪口径
            cost=cost,
            latency_ms=int(payload.get("_latency_ms") or 0),
            output_path=str(path),
            raw={"provider": self.name, "task_id": task_id, "usage": usage, "resolution": payload.get("resolution")},
        )

    def _create(self, prompt: str, seconds: Decimal) -> str:
        text = (
            f"{prompt} --resolution {self.resolution} --duration {int(Decimal(seconds))} "
            f"--ratio 16:9 --watermark false"
        )
        body = {"model": self.model, "content": [{"type": "text", "text": text}]}
        try:
            with self._client_factory(timeout=60) as client:
                response = client.post(
                    f"{self.base_url}/contents/generations/tasks",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                )
        except httpx.TimeoutException as exc:
            raise ProviderError("创建任务超时", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"创建任务失败：{type(exc).__name__}", retryable=True) from exc
        if response.status_code in (401, 403):
            raise ProviderError("Ark 鉴权失败（检查 PROVIDER_VIDEO_API_KEY）", retryable=False, status=response.status_code)
        if response.status_code == 429:
            raise ProviderError("Ark 限流", retryable=True, status=429, raw=_retry_after(response))
        if response.status_code >= 400:
            raise ProviderError(
                f"Ark 拒绝创建任务（{response.status_code}）", retryable=False, status=response.status_code,
                raw={"body": response.text[:400]},
            )
        task_id = response.json().get("id")
        if not task_id:
            raise ProviderError("Ark 没有返回任务 id", retryable=False)
        return task_id

    def _wait(self, task_id: str) -> dict:
        started = time.perf_counter()
        deadline = started + self.timeout
        with self._client_factory(timeout=60) as client:
            while time.perf_counter() < deadline:
                time.sleep(self.poll_interval)
                response = client.get(
                    f"{self.base_url}/contents/generations/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                if response.status_code >= 500:
                    continue  # 查询抖动不判死刑，下一轮再看
                if response.status_code >= 400:
                    raise ProviderError(
                        f"查询任务失败（{response.status_code}）", retryable=False, status=response.status_code
                    )
                payload = response.json()
                status = payload.get("status")
                if status == "succeeded":
                    payload["_latency_ms"] = int((time.perf_counter() - started) * 1000)
                    return payload
                if status in ("failed", "cancelled", "expired"):
                    raise ProviderError(
                        f"任务 {status}：{(payload.get('error') or {}).get('message', '')[:200]}",
                        retryable=False,
                        raw={"task_id": task_id},
                    )
        raise ProviderError(f"任务 {task_id} 超时（>{self.timeout}s）", retryable=True, raw={"task_id": task_id})

    def _download(self, url: str, task_id: str) -> Path:
        """产物落 `MEDIA_ROOT/.gen/cgt-<id>.mp4`，返回**相对 MEDIA_ROOT** 的路径（`_resolve_output` 要这个）。"""
        if self.media_root is None:
            raise ProviderError("provider 没有 media_root，无法落盘", retryable=False)
        folder = self.media_root / ".gen"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{task_id}.mp4"
        try:
            with self._client_factory(timeout=300) as client:
                response = client.get(url)
                if response.status_code >= 400:
                    raise ProviderError(
                        f"下载产物失败（{response.status_code}）", retryable=True, status=response.status_code
                    )
                target.write_bytes(response.content)
        except httpx.HTTPError as exc:
            raise ProviderError(f"下载产物失败：{type(exc).__name__}", retryable=True) from exc
        if not target.is_file() or target.stat().st_size == 0:
            raise ProviderError("产物写盘后是空文件", retryable=True)
        return target.relative_to(self.media_root).as_posix()


class ImagesProvider(Provider):
    """OpenAI Images 兼容的出图通道（`/v1/images/generations`）。

    只管"一句话 → 一张图"，承不承认参考图由厂商决定；这里不做 prompt 工程（那是 Skill 的事）。
    产物落 `MEDIA_ROOT/.gen/img-<sha>.png`，返回相对路径，上层建 asset 时再补宽高/海报。
    计费：图片按张计费，单价由 `PROVIDER_IMAGE_PRICE_PER_IMAGE` 给；没给就记 0（不编价格）。
    """

    name = "images"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        size: str = "1024x1024",
        quality: str = "low",
        price_per_image: Decimal = Decimal(0),
        media_root: Path | None = None,
        timeout: int = 300,
        client_factory: Callable[..., httpx.Client] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.size = size
        self.quality = quality
        self.price_per_image = price_per_image
        self.media_root = media_root
        self.timeout = timeout
        self._client_factory = client_factory or httpx.Client

    def estimate(self, seconds: Decimal) -> Decimal:
        return self.price_per_image

    def text(self, *, stage: str, context: str, format_hint: str = "") -> TextResult:
        raise ProviderError("provider=images 只做图；文本请用 PROVIDER=openai", retryable=False)

    def video(self, *, prompt: str, seconds: Decimal, model: str) -> ProviderResult:
        raise ProviderError("provider=images 只做图；视频请用 PROVIDER_VIDEO=ark", retryable=False)

    def vision(self, *, image: Path, criteria: list[dict]) -> VisionResult:
        raise ProviderError("provider=images 只做图；视觉判定请用 PROVIDER=openai", retryable=False)

    def image(self, *, prompt: str, model: str | None = None) -> ImageResult:
        body = {
            "model": model or self.model,
            "prompt": prompt,
            "size": self.size,
            "quality": self.quality,
            "n": 1,
        }
        started = time.perf_counter()
        try:
            with self._client_factory(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/images/generations",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"出图超时（>{self.timeout}s）", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"出图连接失败：{type(exc).__name__}", retryable=True) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code in (401, 403):
            raise ProviderError("出图通道鉴权失败（检查 PROVIDER_IMAGE_API_KEY）", retryable=False, status=response.status_code)
        if response.status_code == 429:
            raise ProviderError("出图通道限流", retryable=True, status=429, raw=_retry_after(response))
        if response.status_code >= 500:
            raise ProviderError(f"出图通道 {response.status_code}", retryable=True, status=response.status_code)
        if response.status_code >= 400:
            raise ProviderError(
                f"出图请求被拒（{response.status_code}）", retryable=False, status=response.status_code,
                raw={"body": response.text[:400]},
            )

        try:
            data = response.json()["data"][0]
        except (ValueError, KeyError, IndexError) as exc:
            raise ProviderError("出图返回结构不认识", retryable=False, raw={"body": response.text[:400]}) from exc
        payload = _image_bytes(data)
        path, digest = self._persist(payload)
        return ImageResult(
            model=str(response.json().get("model") or body["model"]),
            path=str(path),
            bytes=len(payload),
            width=None,
            height=None,
            cost=self.price_per_image,
            latency_ms=latency_ms,
            source="gen",
            raw={"provider": self.name, "sha256": digest, "size": self.size, "quality": self.quality},
        )

    def _persist(self, payload: bytes) -> tuple[Path, str]:
        if self.media_root is None:
            raise ProviderError("provider 没有 media_root，无法落盘", retryable=False)
        digest = hashlib.sha256(payload).hexdigest()
        folder = self.media_root / ".gen"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"img-{digest[:16]}.png"
        target.write_bytes(payload)
        return target.relative_to(self.media_root).as_posix(), digest


def _image_bytes(data: dict) -> bytes:
    """兼容 `b64_json` 与 `url` 两种返回（`02` 的文档里两种都写了）。"""
    raw = data.get("b64_json")
    if raw:
        import base64

        return base64.b64decode(raw)
    url = data.get("url")
    if not url:
        raise ProviderError("出图返回里既没有 b64_json 也没有 url", retryable=False)
    try:
        response = httpx.get(url, timeout=120)
    except httpx.HTTPError as exc:
        raise ProviderError(f"下载生成图失败：{type(exc).__name__}", retryable=True) from exc
    if response.status_code >= 400:
        raise ProviderError(f"下载生成图失败（{response.status_code}）", retryable=True, status=response.status_code)
    return response.content


def _find_reference_image(media_root: Path) -> Path | None:
    for folder in ("00_静态资源", "人物卡/results"):
        directory = media_root / folder
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*")):
            if candidate.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") and candidate.is_file():
                return candidate
    return None


def _data_url(image: Path) -> str:
    suffix = image.suffix.lower().lstrip(".") or "jpeg"
    media = "image/jpeg" if suffix in ("jpg", "jpeg") else f"image/{suffix}"
    return f"data:{media};base64,{base64.b64encode(image.read_bytes()).decode()}"


def _parse_json(content: str) -> dict:
    """模型偶尔会套 ```json 围栏或加一句解释 —— 剥掉再解析，失败就当成上游违规。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ProviderError("模型没有返回 JSON 对象", retryable=False, raw={"content": content[:400]})
    try:
        payload = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ProviderError("模型返回的 JSON 解析失败", retryable=False, raw={"content": content[:400]}) from exc
    if not isinstance(payload, dict):
        raise ProviderError("模型返回的不是 JSON 对象", retryable=False, raw={"content": content[:400]})
    return payload


def _clean_verdicts(payload: dict) -> list[dict]:
    """只收协议内的三种结论；模型编出来的值一律丢掉（上层会把它当 unknown）。"""
    verdicts = []
    for item in payload.get("verdicts") or []:
        if not isinstance(item, dict) or "id" not in item:
            continue
        result = item.get("result")
        if result not in ("pass", "fail", "unknown"):
            continue
        verdicts.append({"id": item["id"], "result": result, "evidence": str(item.get("evidence") or "")[:300]})
    return verdicts


def _retry_after(response: httpx.Response) -> dict:
    value = response.headers.get("retry-after")
    return {"retry_after": value} if value else {}


def _find_fixture(media_root: Path) -> Path | None:
    folder = media_root / "第一集"
    if not folder.is_dir():
        return None
    candidates = sorted(folder.glob("*.mp4"))
    return candidates[0] if candidates else None


def make_provider(settings: Settings, name: str | None = None) -> Provider:
    """按名字造 provider；不给名字就用配置里的那条通道（`PROVIDER`）。

    注意 `name` 的默认值是 **None 而不是 "mock"**：`"mock"` 是 truthy，会把 `settings.provider`
    直接吃掉 —— 实测导致"配了真视觉却还在用 mock 判定"。
    """
    chosen = (name or settings.provider or "mock").strip().lower()
    if chosen == "mock":
        fixture = _find_fixture(settings.media_root)
        # 产物路径要相对 MEDIA_ROOT
        return MockProvider(
            fixture.relative_to(settings.media_root) if fixture else None, media_root=settings.media_root
        )
    if chosen == "openai":
        return OpenAICompatProvider(
            base_url=settings.provider_base_url,
            api_key=settings.provider_api_key,
            text_model=settings.provider_text_model,
            vision_model=settings.provider_vision_model,
            timeout=settings.provider_timeout,
        )
    if chosen == "ark":
        return ArkVideoProvider(
            base_url=settings.provider_video_base_url,
            api_key=settings.provider_video_api_key,
            model=settings.provider_video_model,
            resolution=settings.provider_video_resolution,
            price_per_1k_tokens=Decimal(settings.provider_video_price_per_1k_tokens),
            media_root=settings.media_root,
        )
    raise ValueError(f"未知 provider：{chosen}（可选：mock / openai / ark）")


def make_image_provider(settings: Settings) -> Provider:
    """出图通道（`PROVIDER_IMAGE`）。与文本/视频一样是**独立的一条**，不共用 `make_provider`。"""
    if settings.provider_image == "images":
        return ImagesProvider(
            base_url=settings.provider_image_base_url,
            api_key=settings.provider_image_api_key,
            model=settings.provider_image_model,
            size=settings.provider_image_size,
            quality=settings.provider_image_quality,
            price_per_image=Decimal(settings.provider_image_price_per_image or "0"),
            media_root=settings.media_root,
        )
    if settings.provider_image != "mock":
        raise ValueError(f"未知出图 provider：{settings.provider_image}（可选：mock / images）")
    return make_provider(settings, "mock")


def provider_status(settings: Settings) -> dict:
    """`/admin/providers` 与启动日志共用的能力视图：哪条通道是真的、哪条还是 mock。"""
    chosen = settings.provider
    video_real = settings.provider_video == "ark" and bool(settings.provider_video_api_key)
    real_text = chosen != "mock" and bool(settings.provider_text_model)
    real_vision = chosen != "mock" and bool(settings.provider_vision_model or settings.provider_text_model)
    return {
        "provider": chosen,
        "base_url": settings.provider_base_url or None,
        "text": {"model": settings.provider_text_model or "mock-text", "real": real_text},
        "vision": {"model": settings.provider_vision_model or settings.provider_text_model or "mock-vision", "real": real_vision},
        # 视频通道单独选（PROVIDER_VIDEO）：ark = Seedance 真出片，mock = 回放已有成片
        "video": {
            "model": settings.provider_video_model or MOCK_MODEL,
            "real": video_real,
            "channel": settings.provider_video,
            "base_url": settings.provider_video_base_url if settings.provider_video != "mock" else None,
            "resolution": settings.provider_video_resolution if settings.provider_video != "mock" else None,
        },
        "image": {
            "model": settings.provider_image_model,
            "real": settings.provider_image == "images" and bool(settings.provider_image_api_key),
            "channel": settings.provider_image,
            "base_url": settings.provider_image_base_url if settings.provider_image != "mock" else None,
        },
        "timeout_s": settings.provider_timeout,
    }
