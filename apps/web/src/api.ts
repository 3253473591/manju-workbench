/**
 * API 客户端：只做四件事 —— 带 Cookie、解析错误封装、写请求带 If-Match、生成类带幂等键。
 *
 * 前端不猜状态码语义：错误一律按 `code` 分支（`02` §8 的约定），HTTP 码只用于粗分流。
 */

export interface ApiErrorBody {
  code: string;
  message: string;
  detail: Record<string, unknown>;
  request_id: string | null;
}

export class ApiError extends Error {
  constructor(readonly body: ApiErrorBody, readonly status: number) {
    super(body.message);
  }
}

const BASE = "/api/v1";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(`${BASE}${path}`, {
    ...init, headers, credentials: "same-origin", signal: init.signal ?? AbortSignal.timeout(30_000),
  });
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    throw new ApiError({ code: "INTERNAL", message: `接口返回格式异常（${response.status}）`, detail: {}, request_id: null }, response.status);
  }
  if (!response.ok) {
    const body: ApiErrorBody = payload?.error ?? {
      code: "INTERNAL",
      message: `请求失败（${response.status}）`,
      detail: {},
      request_id: null,
    };
    throw new ApiError(body, response.status);
  }
  return payload as T;
}

/** 幂等键：一次逻辑提交生成一次，重试复用同一个（`02` §8）。 */
export function idemKey(): string {
  return crypto.randomUUID();
}

// ---------------------------------------------------------------- 类型（只写前端用到的字段）

export interface User {
  id: number;
  email: string;
  role: "user" | "lead" | "admin";
  created_at: string;
}

export interface Capabilities {
  can_create_project: boolean;
  can_write_global: boolean;
  can_write_any_project: boolean;
}

export interface Me {
  user: User;
  capabilities: Capabilities;
}

export interface Project {
  id: number;
  title: string;
  owner_id: number;
  genre: string | null;
  tone: string | null;
  logline: string | null;
  status: "active" | "archived";
  version: number;
}

export interface Episode {
  id: number;
  project_id: number;
  idx: number;
  title: string | null;
  synopsis: string | null;
  status: string;
  version: number;
}

export interface Slot {
  idx: number;
  kind: "image" | "video" | "audio";
  asset_id: number | null;
  asset_ref: string | null;
  script_ref: string | null;
  role: string;
  note: string;
  url: string | null;
  thumb_url: string | null;
}

export interface Clip {
  id: number;
  episode_id: number;
  idx: number;
  source_idx: number;
  title: string | null;
  duration_s: string;
  status: string;
  prompt: string | null;
  shots: { no: number; start: string; end: string; desc: string }[] | null;
  dialogue: { speaker: string; text: string; shot_no: number | null; on_screen: boolean }[] | null;
  acceptance: { id: string; criterion: string; superseded_by: string | null }[] | null;
  ref_slots: Slot[] | null;
  current_revision_id: number | null;
  output_asset_id: number | null;
  cost_total: string;
  version: number;
}

export interface TraceEntry {
  ts: string;
  step: string;
  verdict: string | null;
  ms: number | null;
  detail: Record<string, unknown> | null;
}

export interface Job {
  id: number;
  state: string;
  attempts: number;
  max_attempts: number;
  budget_limit: string;
  cost_used: string;
  error_code: string | null;
  model: string | null;
}

export interface GenerateResult {
  job: Job;
  preview: {
    prompt_head: string;
    tokens_est: number;
    cost_est: string;
    duration_s: string;
  };
}

export interface CostItem {
  key: string;
  calls: number;
  tokens: number;
  seconds: string;
  cost: string;
}

export interface Revision {
  id: number;
  seq: number | null;
  prompt: string | null;
  cost: string;
  motion: { samples?: number; freeze?: number; avg?: number; max?: number } | null;
  acceptance_result: { id: string; result: string; score?: number | null; evidence: string }[] | null;
}

export interface AssetItem {
  id: number;
  project_id: number;
  kind: "image" | "video" | "audio";
  path: string;
  thumb_path: string | null;
  bytes: number;
  w: number | null;
  h: number | null;
  url: string;
  thumb_url: string;
}

/** 资产被引用的地方（删除返回 409 时带出来）。 */
export interface Usage {
  clip_id?: number;
  clip_idx?: number;
  episode_id?: number;
  slot_idx?: number;
  revision_id?: number;
  target_kind?: string;
  target_id?: number;
}

export interface StepTemplate {
  stage: string;
  label: string;
  target: "project" | "episode" | "clip";
  skill: string | null;
  sections: number;
  tokens_est: number;
}

export interface StepRun {
  stage: string;
  revision_id: number;
  payload: Record<string, unknown>;
  written: string[];
  input_tokens: number;
  output_tokens: number;
  cost: string;
  skill_sections: number;
  context_trimmed: boolean;
}

export interface RevisionDiff {
  a: number;
  b: number;
  prompt_diff: string;
  cost_delta: string;
}

// ---------------------------------------------------------------- Skill（P1-1）

export interface Skill {
  id: number;
  name: string;
  stage: "S1" | "S2" | "S3" | "S4" | "S5" | "review";
  status: "active" | "disabled";
  created_at: string;
}

export interface SkillVersion {
  id: number;
  skill_id: number;
  version: number;
  state: "draft" | "active" | "superseded";
  sections: { rule_id: string; title: string; tokens_est: number; tags: string[] }[];
  tool_ref: string | null;
  checksum: string;
  note: string | null;
  dry_run_ok_at: string | null;
  superseded_at: string | null;
  created_at: string;
}

export interface SkillVersionCreated {
  id: number;
  skill_id: number;
  version: number;
  state: "draft" | "active" | "superseded";
  checksum: string;
  sections: number;
  tokens_total: number;
}

export interface DryRun {
  injected: { rule_id: string; title: string; tokens_est: number; selected: boolean }[];
  budget: { input_limit: number; used_by_skill: number; used_total: number; trimmed: boolean };
  prompt_preview: string;
  diff_vs_active: { added_rules: string[]; removed_rules: string[]; tokens_delta: number };
}

export interface ProviderChannel {
  model: string;
  real: boolean;
  /** 运行时开关（存 DB，多副本共享）：停用后调上游前就 503。 */
  enabled: boolean;
  reason: string | null;
  updated_at: string | null;
  actor_id: number | null;
}

export interface ProviderStatus {
  provider: string;
  base_url: string | null;
  text: ProviderChannel;
  vision: ProviderChannel;
  video: ProviderChannel;
  image: ProviderChannel;
  timeout_s: number;
  note: string;
}

export interface ProviderCheck {
  ok: boolean;
  provider: string;
  model: string | null;
  latency_ms: number | null;
  detail: string;
}

// ---------------------------------------------------------------- 端点

export const api = {
  login: (email: string, password: string) =>
    request<User>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),

  logout: () => request<void>("/auth/logout", { method: "POST" }),

  me: () => request<Me>("/auth/me"),

  projects: () => request<{ items: Project[]; next_cursor: number | null }>("/projects"),

  episodes: (projectId: number) => request<Episode[]>(`/projects/${projectId}/episodes`),

  clips: (episodeId: number) => request<Clip[]>(`/episodes/${episodeId}/clips`),

  /** 项目级镜头墙：跨集拉全部片段（多集项目用）。 */
  projectClips: (projectId: number) =>
    request<{ items: Clip[]; next_cursor: number | null }>(`/projects/${projectId}/clips?limit=200`),

  revisions: (clipId: number) => request<{ items: Revision[] }>(`/clips/${clipId}/revisions`),

  cost: (
    projectId: number,
    groupBy: "model" | "day" | "clip" | "episode" = "model",
  ) =>
    request<{ group_by: string; items: CostItem[]; total: { calls: number; cost: string } }>(
      `/projects/${projectId}/cost?group_by=${groupBy}`,
    ),

  /** 付费点提交：`If-Match` 用列表拿到的 `clip.version`，幂等键由调用方生成一次并复用。 */
  generate: (clip: Clip, budget: string, key: string) =>
    request<GenerateResult>(`/clips/${clip.id}/generate`, {
      method: "POST",
      headers: { "If-Match": `"${clip.version}"`, "Idempotency-Key": key },
      body: JSON.stringify({ res: "draft", budget_limit: budget }),
    }),

  /** 打回后重发：源状态必须是 rejected，走的还是同一个付费点（`02` §6.2）。 */
  reroll: (clip: Clip, budget: string, key: string) =>
    request<GenerateResult>(`/clips/${clip.id}/reroll`, {
      method: "POST",
      headers: { "If-Match": `"${clip.version}"`, "Idempotency-Key": key },
      body: JSON.stringify({ res: "draft", budget_limit: budget }),
    }),

  rejectClip: (clip: Clip) =>
    request<Clip>(`/clips/${clip.id}`, {
      method: "PATCH",
      headers: { "If-Match": `"${clip.version}"` },
      body: JSON.stringify({ status: "rejected" }),
    }),

  confirm: (jobId: number) => request<Job>(`/jobs/${jobId}/confirm`, { method: "POST" }),

  cancel: (jobId: number) => request<Job>(`/jobs/${jobId}/cancel`, { method: "POST" }),

  job: (jobId: number) => request<Job>(`/jobs/${jobId}`),

  /** P1-4：job 的执行时间线，最多 TRACE_KEEP 条。 */
  jobTrace: (jobId: number) => request<TraceEntry[]>(`/jobs/${jobId}/trace`),

  approveClip: (clip: Clip) =>
    request<Clip>(`/clips/${clip.id}`, {
      method: "PATCH",
      headers: { "If-Match": `"${clip.version}"` },
      body: JSON.stringify({ status: "approved" }),
    }),

  /** 资产库：项目级列表 —— 同一张图在任意集都能挑到（跨集复用的入口）。kind 省略=全部。 */
  assets: (projectId: number, kind?: "image" | "video" | "audio") =>
    request<{ items: AssetItem[]; next_cursor: number | null }>(
      `/assets?project_id=${projectId}${kind ? `&kind=${kind}` : ""}&limit=200`,
    ),

  /** 删资产：被引用会 409，`detail.usages` 列出引用方（集/片段/槽位）。 */
  deleteAsset: (assetId: number, purge = false) =>
    request<void>(`/assets/${assetId}${purge ? "?purge=1" : ""}`, { method: "DELETE" }),

  saveSlots: (clip: Clip, slots: unknown[]) =>
    request<Clip>(`/clips/${clip.id}`, {
      method: "PATCH",
      headers: { "If-Match": `"${clip.version}"` },
      body: JSON.stringify({ ref_slots: slots }),
    }),

  /** 出图（人物卡/参考图）：同步返回，产物直接进项目资产库。 */
  generateImage: (projectId: number, prompt: string) =>
    request<{
      asset_id: number;
      path: string;
      source: string;
      provider_source: string;
      deduped: boolean;
      model: string;
      cost: string;
      latency_ms: number;
      url: string;
      thumb_url: string;
    }>(`/projects/${projectId}/images`, { method: "POST", body: JSON.stringify({ prompt }) }),

  // ------------------------------------------------------------ 流水线（P0-3）

  stepTemplates: () => request<StepTemplate[]>("/steps/templates"),

  /** 五个 stage 的 URL 形状不同（`02` §3.4）：S1/S5 打项目，S2/S3 打集，S4 打片段。 */
  runStep: (stage: string, target: { projectId: number; episodeId?: number | null; clipId?: number | null }) => {
    const { projectId, episodeId, clipId } = target;
    const path =
      stage === "S1" || stage === "S5"
        ? `/projects/${projectId}/steps/${stage}/run`
        : stage === "S4"
          ? `/clips/${clipId}/steps/S4/run`
          : `/episodes/${episodeId}/steps/${stage}/run`;
    return request<StepRun>(path, { method: "POST" });
  },

  createProject: (title: string) =>
    request<Project>("/projects", { method: "POST", body: JSON.stringify({ title }) }),

  createEpisode: (projectId: number, title: string) =>
    request<Episode>(`/projects/${projectId}/episodes`, { method: "POST", body: JSON.stringify({ title }) }),

  // ------------------------------------------------------------ 版本链（P0-8）

  rollbackClip: (clip: Clip, revisionId: number) =>
    request<Clip>(`/clips/${clip.id}/rollback`, {
      method: "POST",
      body: JSON.stringify({ revision_id: revisionId }),
    }),

  revisionDiff: (leftId: number, rightId: number) =>
    request<RevisionDiff>(`/revisions/${leftId}/diff/${rightId}`),

  // ------------------------------------------------------------ Skill 维护（P1-1，仅 admin）

  skills: () => request<Skill[]>("/skills"),

  skillVersions: (skillId: number) => request<SkillVersion[]>(`/skills/${skillId}/versions`),

  createSkillVersion: (skillId: number, bodyMd: string, note: string | null) =>
    request<{ skill_version: SkillVersionCreated }>(`/skills/${skillId}/versions`, {
      method: "POST",
      body: JSON.stringify({ body_md: bodyMd, note }),
    }),

  /** 试跑：只在内存里组装。目标必须对上 stage（S1/S5→项目、S2/S3→集、S4→片段）。 */
  dryRunSkill: (
    skillId: number,
    version: number,
    target: { project_id?: number; episode_id?: number; clip_id?: number },
  ) =>
    request<DryRun>(`/skills/${skillId}/versions/${version}/dry-run`, {
      method: "POST",
      body: JSON.stringify(target),
    }),

  publishSkillVersion: (skillId: number, version: number) =>
    request<{ skill: { id: number; active_version: number }; superseded: number | null }>(
      `/skills/${skillId}/versions/${version}/publish`,
      { method: "POST" },
    ),

  rollbackSkill: (skillId: number, version: number) =>
    request<{ skill: { id: number; active_version: number }; superseded: number | null }>(
      `/skills/${skillId}/rollback`,
      { method: "POST", body: JSON.stringify({ version }) },
    ),

  patchSkill: (skillId: number, status: "active" | "disabled") =>
    request<Skill>(`/skills/${skillId}`, { method: "PATCH", body: JSON.stringify({ status }) }),

  // ------------------------------------------------------------ provider（admin 只读 + 自检）

  providerStatus: () => request<ProviderStatus>("/admin/providers"),

  checkProvider: () => request<ProviderCheck>("/admin/providers/check", { method: "POST" }),

  /** 停用/启用一条通道。只改运行态，key 与模型仍来自环境变量。 */
  setProviderState: (channel: "text" | "vision" | "video" | "image", enabled: boolean, reason?: string) =>
    request<ProviderStatus>(`/admin/providers/${channel}/state`, {
      method: "POST",
      body: JSON.stringify({ enabled, reason }),
    }),
};

export function thumbUrl(assetId: number | null): string | null {
  return assetId ? `${BASE}/assets/${assetId}/file?thumb=1` : null;
}
