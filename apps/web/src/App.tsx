import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  api,
  idemKey,
  thumbUrl,
  type Clip,
  type DryRun,
  type Episode,
  type Me,
  type Project,
  type ProviderChannel,
  type Revision,
  type Slot,
  type StepRun,
  type StepTemplate,
  type TraceEntry,
  type Usage,
} from "./api";

const TERMINAL = ["done", "failed", "cancelled", "paused_budget"];

export function App() {
  const queryClient = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, retry: false });

  if (me.isLoading) return <div className="boot">载入中…</div>;
  if (me.isError) {
    const error = me.error as ApiError;
    if (error.status === 401) {
      return <Login onDone={() => queryClient.invalidateQueries({ queryKey: ["me"] })} />;
    }
    return <div className="boot">接口不可用：{error.message}</div>;
  }
  return <Workspace me={me.data!} />;
}

function Login({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState("seed@example.com");
  const [password, setPassword] = useState("seedpass12345");
  const [error, setError] = useState<string | null>(null);
  const login = useMutation({
    mutationFn: () => api.login(email, password),
    onSuccess: onDone,
    onError: (err: ApiError) => setError(err.message),
  });

  return (
    <div className="mj-login">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          login.mutate();
        }}
      >
        <h1>漫剧工作台</h1>
        <p className="mj-hint">演示账号：seed@example.com（lead，可写） / admin@example.com（只读）</p>
        <label>
          邮箱
          <input value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="username" />
        </label>
        <label>
          口令
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
          />
        </label>
        {error && <p className="mj-error">{error}</p>}
        <button type="submit" disabled={login.isPending}>
          {login.isPending ? "登录中…" : "登录"}
        </button>
      </form>
    </div>
  );
}

type Tab = "pipeline" | "table" | "wall" | "assets" | "cost" | "skills";
type ChannelName = "text" | "vision" | "video" | "image";

function Workspace({ me }: { me: Me }) {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("pipeline");
  const [projectId, setProjectId] = useState<number | null>(null);
  const [episodeId, setEpisodeId] = useState<number | null>(null);
  const [payPoint, setPayPoint] = useState<{ clip: Clip; mode: "generate" | "reroll" } | null>(null);
  const [slotClip, setSlotClip] = useState<Clip | null>(null);
  const [creating, setCreating] = useState<"project" | "episode" | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [sse, setSse] = useState<"connecting" | "live" | "offline">("connecting");
  const canWrite = me.capabilities.can_write_any_project;

  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  // 库里长期躺着多个 active 项目（真实剧集 + 手写大纲跑出来的新项目），只看第一个等于别的项目都进不去
  const project =
    projects.data?.items.find((item) => item.id === projectId) ??
    projects.data?.items.find((item) => item.status === "active") ??
    projects.data?.items[0];

  const episodes = useQuery({
    queryKey: ["episodes", project?.id],
    queryFn: () => api.episodes(project!.id),
    enabled: Boolean(project),
  });
  const episode = episodes.data?.find((item) => item.id === episodeId) ?? episodes.data?.[0] ?? null;

  const clips = useQuery({
    queryKey: ["clips", episode?.id],
    queryFn: () => api.clips(episode!.id),
    enabled: Boolean(episode),
  });

  const total = useMemo(
    () => (clips.data ?? []).reduce((sum, clip) => sum + Number(clip.cost_total), 0).toFixed(6),
    [clips.data],
  );

  const create = useMutation({
    mutationFn: async () => {
      const title = draftTitle.trim();
      if (!title) throw new ApiError(
        { code: "VALIDATION_SHAPE", message: "标题不能为空", detail: {}, request_id: null }, 422,
      );
      return creating === "project"
        ? api.createProject(title)
        : api.createEpisode(project!.id, title);
    },
    onSuccess: (row) => {
      setCreating(null);
      setDraftTitle("");
      if ("project_id" in row) setEpisodeId(row.id);
      else setProjectId(row.id);
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      queryClient.invalidateQueries({ queryKey: ["episodes"] });
    },
  });

  // 写操作与事件流都走这一处：片段、成本、版本链，**以及项目/集的元数据**
  // （S1 会改 genre/tone、S2/S3 会改 episode.status，只刷 clips 会让头部一直显示旧值）
  const refresh = useCallback(() => {
    for (const key of ["projects", "episodes", "clips", "projectClips", "cost", "jobs", "revisions"]) {
      queryClient.invalidateQueries({ queryKey: [key] });
    }
  }, [queryClient]);

  // P1-3：事件流取代 2s 轮询。断线时浏览器 EventSource 自己重连（带 Last-Event-ID），
  // 页面上的轮询仍留作兜底 —— 事件丢了也不会看到过期数据。
  useEffect(() => {
    if (!project) return;
    const source = new EventSource(`/api/v1/projects/${project.id}/events`);
    const onJob = (event: MessageEvent) => {
      refresh();
      const payload = JSON.parse(event.data) as { job?: { id?: number } };
      if (payload.job?.id) queryClient.invalidateQueries({ queryKey: ["job", payload.job.id] });
    };
    source.addEventListener("job.updated", onJob as EventListener);
    source.addEventListener("clip.updated", refresh as EventListener);
    source.addEventListener("cost.updated", refresh as EventListener);
    source.addEventListener("resync", refresh as EventListener);
    source.onopen = () => setSse("live");
    source.onerror = () => setSse("offline");
    setSse("connecting");
    return () => source.close();
  }, [project?.id, queryClient, refresh]);

  // SSE 断了就把"轮询兜底"这句话兑现：状态栏写着兜底，就得真的有轮询
  useEffect(() => {
    if (sse !== "offline") return;
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [sse, refresh]);

  if (projects.isError) return <div className="boot">{String((projects.error as ApiError).message)}</div>;
  if (!project) {
    return (
      <div className="boot">
        <p>还没有项目。</p>
        {me.capabilities.can_create_project ? (
          <CreateForm
            label="项目标题"
            value={draftTitle}
            onChange={setDraftTitle}
            onSubmit={() => create.mutate()}
            pending={create.isPending}
            error={create.error ? (create.error as ApiError).message : null}
          />
        ) : (
          <p>当前账号不能建项目（只有 lead 能建）。先跑一次 seed，或用 lead 账号登录。</p>
        )}
      </div>
    );
  }

  return (
    <div className="mj-shell">
      <header className="mj-head">
        <div>
          <strong>{project.title}</strong>
          <span className="mj-sub">
            {project.genre ?? "未定类型"} · {project.tone ?? "未定基调"}
          </span>
        </div>
        <div className="mj-head-right">
          <select
            value={project.id}
            onChange={(event) => {
              setProjectId(Number(event.target.value));
              setEpisodeId(null);
            }}
          >
            {(projects.data?.items ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.title}
                {item.status === "archived" ? "（已归档）" : ""}
              </option>
            ))}
          </select>
          {me.capabilities.can_create_project && (
            <button onClick={() => (setCreating(creating === "project" ? null : "project"), setDraftTitle(""))}>
              ＋项目
            </button>
          )}
          <select
            value={episode?.id ?? ""}
            onChange={(event) => setEpisodeId(Number(event.target.value))}
          >
            {(episodes.data ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                第 {item.idx} 集 · {item.title ?? "未命名"}（{item.status}）
              </option>
            ))}
          </select>
          {canWrite && (
            <button onClick={() => (setCreating(creating === "episode" ? null : "episode"), setDraftTitle(""))}>
              ＋分集
            </button>
          )}
          <span className="mj-user">
            {sse === "live" ? "实时" : sse === "connecting" ? "连接中" : "轮询兜底"} · {me.user.email} · {me.user.role}
            {!canWrite && " · 内容只读"}
          </span>
          <button
            onClick={async () => {
              await api.logout();
              location.reload();
            }}
          >
            退出
          </button>
        </div>
      </header>

      {creating && (
        <CreateForm
          label={creating === "project" ? "项目标题" : `第 ${(episodes.data?.length ?? 0) + 1} 集标题`}
          value={draftTitle}
          onChange={setDraftTitle}
          onSubmit={() => create.mutate()}
          pending={create.isPending}
          error={create.error ? (create.error as ApiError).message : null}
        />
      )}

      <nav className="mj-tabs">
        {(
          [
            ["pipeline", "流水线"],
            ["table", "分镜表"],
            ["wall", "镜头墙"],
            ["assets", "资产库"],
            ["cost", "成本台账"],
            ...(me.capabilities.can_write_global ? [["skills", "Skill 维护"] as [Tab, string]] : []),
          ] as [Tab, string][]
        ).map(([key, label]) => (
          <button key={key} className={tab === key ? "on" : ""} onClick={() => setTab(key)}>
            {label}
          </button>
        ))}
        <span className="mj-total">本集合计 ¥{total}</span>
      </nav>

      {clips.isLoading && <div className="boot">载入分镜…</div>}
      {clips.isError && <div className="boot">{(clips.error as ApiError).message}</div>}

      {tab === "pipeline" && (
        <Pipeline
          project={project}
          episode={episode}
          clips={clips.data ?? []}
          canWrite={canWrite}
          onChanged={refresh}
        />
      )}

      {tab === "table" && clips.data && (
        <table className="mj-table">
          <thead>
            <tr>
              <th>#</th>
              <th>标题</th>
              <th className="num">时长</th>
              <th>状态</th>
              <th className="num">镜头</th>
              <th className="num">槽位</th>
              <th className="num">成本</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {clips.data.map((clip) => (
              <Row
                key={clip.id}
                clip={clip}
                canWrite={canWrite}
                onSlots={() => setSlotClip(clip)}
                onPayPoint={(mode) => setPayPoint({ clip, mode })}
              />
            ))}
          </tbody>
        </table>
      )}

      {tab === "wall" && (
        <Wall
          projectId={project.id}
          episodes={episodes.data ?? []}
          episode={episode}
          clips={clips.data ?? []}
          onPayPoint={(item, mode) => setPayPoint({ clip: item, mode })}
          canWrite={canWrite}
        />
      )}

      {tab === "assets" && <AssetLibrary project={project} canWrite={canWrite} />}

      {tab === "cost" && <CostTable projectId={project.id} />}

      {tab === "skills" && me.capabilities.can_write_global && <SkillEditor />}

      {payPoint && (
        <PayPoint
          // 用当前列表里的最新一份：快照里的 current_revision_id/version 在出片后已经过期，
          // 版本链会把刚产出的那一版标成"回滚到此版"而不是"当前"
          clip={clips.data?.find((item) => item.id === payPoint.clip.id) ?? payPoint.clip}
          mode={payPoint.mode}
          canWrite={canWrite}
          onClose={() => setPayPoint(null)}
        />
      )}
      {slotClip && (
        <SlotPanel clip={slotClip} projectId={project.id} canWrite={canWrite} onClose={() => setSlotClip(null)} />
      )}
    </div>
  );
}

function CreateForm({
  label,
  value,
  onChange,
  onSubmit,
  pending,
  error,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  onSubmit: () => void;
  pending: boolean;
  error: string | null;
}) {
  return (
    <form
      className="mj-newform"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <label>
        {label}
        <input value={value} onChange={(event) => onChange(event.target.value)} autoFocus maxLength={200} />
      </label>
      <button type="submit" disabled={pending}>
        {pending ? "提交中…" : "确定"}
      </button>
      {error && <p className="mj-error">{error}</p>}
    </form>
  );
}

/** 五个 stage 的说明：DoD 要求"空白项目 → 有分镜表"全程不碰数据库，所以每个 stage 都要能点。 */
function describeTarget(target: StepTemplate["target"]): string {
  return target === "project" ? "作用于项目" : target === "episode" ? "作用于本集" : "作用于单个片段";
}

function summarize(stage: string, run: StepRun): string {
  const payload = run.payload as Record<string, unknown>;
  const lines: string[] = [`写入：${run.written.join("、") || "（无）"} · tokens ${run.input_tokens}→${run.output_tokens}`];
  if (stage === "S1") {
    lines.push(`logline：${payload.logline ?? "—"}`);
    lines.push(`类型/基调：${payload.genre ?? "—"} / ${payload.tone ?? "—"}`);
  } else if (stage === "S2") {
    lines.push(`分集剧情：${String(payload.synopsis ?? "—").slice(0, 160)}…`);
  } else if (stage === "S3") {
    const clips = (payload.clips as unknown[] | undefined) ?? [];
    lines.push(`生成了 ${clips.length} 个片段（右侧"分镜表"里可见）`);
  } else if (stage === "S4") {
    lines.push(`提示词：${String(payload.prompt ?? "—").slice(0, 160)}…`);
    lines.push(`负面：${payload.negative ?? "—"}`);
  } else if (stage === "S5") {
    const characters = (payload.characters as { name?: string }[] | undefined) ?? [];
    lines.push(`人物卡：${characters.map((item) => item.name ?? "?").join("、") || "—"}`);
    lines.push("（只产出不落库：要图请点下面的「用人物卡出图」）");
  }
  return lines.join("\n");
}

function Pipeline({
  project,
  episode,
  clips,
  canWrite,
  onChanged,
}: {
  project: Project;
  episode: Episode | null;
  clips: Clip[];
  canWrite: boolean;
  onChanged: () => void;
}) {
  const templates = useQuery({ queryKey: ["stepTemplates"], queryFn: api.stepTemplates });
  const [clipId, setClipId] = useState<number | null>(null);
  const [results, setResults] = useState<Record<string, StepRun>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  const targetClip = clips.find((item) => item.id === clipId) ?? clips.find((item) => item.prompt) ?? clips[0] ?? null;

  const run = useMutation({
    mutationFn: (stage: string) =>
      api.runStep(stage, { projectId: project.id, episodeId: episode?.id, clipId: targetClip?.id }),
    onSuccess: (data, stage) => {
      setResults((prev) => ({ ...prev, [stage]: data }));
      setErrors((prev) => ({ ...prev, [stage]: "" }));
      onChanged();
    },
    onError: (error: ApiError, stage) => setErrors((prev) => ({ ...prev, [stage]: error.message })),
  });

  // S5 出人物卡后，可以直接让出图通道画一张（mock 通道 = 复制已有素材，真通道 = 真生成）
  const [card, setCard] = useState<{ asset_id: number; thumb_url: string; label: string } | null>(null);
  const draw = useMutation({
    mutationFn: (prompt: string) => api.generateImage(project.id, prompt),
    onSuccess: (data) => {
      // 命中已有资产时 `source` 可能是 seed/upload，真正该显示的是 provider_source
      const label = data.provider_source === "mock" ? "mock 回放" : "真出图";
      setCard({
        asset_id: data.asset_id,
        thumb_url: data.thumb_url,
        label: data.deduped ? `${label} · 命中已有资产` : label,
      });
      onChanged();
    },
    onError: (error: ApiError) => setErrors((prev) => ({ ...prev, S5: error.message })),
  });

  if (templates.isLoading) return <div className="boot">载入流水线…</div>;
  if (templates.isError) return <div className="boot">{(templates.error as ApiError).message}</div>;

  return (
    <div className="mj-pipe">
      {(templates.data ?? []).map((template) => {
        const need =
          template.target === "clip" ? (targetClip ? null : "先有片段") :
          template.target === "episode" ? (episode ? null : "先有分集") : null;
        const blocked = !canWrite || need !== null;
        const error = errors[template.stage];
        const result = results[template.stage];
        return (
          <section key={template.stage} className="mj-stage">
            <header>
              <b>{template.stage}</b>
              <span>{template.label}</span>
            </header>
            <p className="mj-hint">
              {describeTarget(template.target)} · Skill {template.skill ?? "无"}（{template.sections} 节 ≈{" "}
              {template.tokens_est} tokens）
              {need && ` · ${need}`}
              {!canWrite && " · 只读账号"}
            </p>
            <div className="mj-actions">
              {template.stage === "S4" && clips.length > 0 && (
                <select
                  value={targetClip?.id ?? ""}
                  onChange={(event) => setClipId(Number(event.target.value))}
                >
                  {clips.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.idx}. {item.title ?? "未命名"}
                    </option>
                  ))}
                </select>
              )}
              <button
                onClick={() => run.mutate(template.stage)}
                disabled={blocked || (run.isPending && run.variables === template.stage)}
              >
                {run.isPending && run.variables === template.stage ? "运行中…" : `运行 ${template.stage}`}
              </button>
            </div>
            {error && <p className="mj-error">{error}</p>}
            {result && <pre className="mj-payload">{summarize(template.stage, result)}</pre>}
            {template.stage === "S5" && result && (
              <div className="mj-cardgen">
                <button
                  onClick={() => {
                    const characters = (result.payload as { characters?: { name?: string; anchor?: string }[] }).characters ?? [];
                    const prompt = characters
                      .map((item) => `${item.name ?? "角色"}：${item.anchor ?? ""}`)
                      .join("；")
                      .slice(0, 1_500);
                    if (prompt) draw.mutate(`${prompt}。国风修仙人物立绘，全身正面，纯色浅灰背景，无文字无水印`);
                  }}
                  disabled={draw.isPending}
                >
                  {draw.isPending ? "出图中…（10~30s）" : "用人物卡出图"}
                </button>
                {card && (
                  <figure className="mj-cardgen-figure">
                    <img src={card.thumb_url} alt="人物卡" />
                    <figcaption>
                      asset #{card.asset_id} · {card.label}
                    </figcaption>
                  </figure>
                )}
              </div>
            )}
          </section>
        );
      })}
      <p className="mj-hint mj-pipe-note">
        S3 会按剧本新建片段；本集已有片段时会返回 409（重跑分镜请新建一集，别把已出的片冲掉）。
      </p>
    </div>
  );
}

function Row({
  clip,
  canWrite,
  onSlots,
  onPayPoint,
}: {
  clip: Clip;
  canWrite: boolean;
  onSlots: () => void;
  onPayPoint: (mode: "generate" | "reroll") => void;
}) {
  const queryClient = useQueryClient();
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["clips"] });
    queryClient.invalidateQueries({ queryKey: ["projectClips"] });
  };
  const approve = useMutation({ mutationFn: () => api.approveClip(clip), onSuccess: refresh });
  const reject = useMutation({ mutationFn: () => api.rejectClip(clip), onSuccess: refresh });
  // 412（别处改过这个片段）以前是**静默失败**：不刷新、不报错，点完像没点
  const failure = (approve.error ?? reject.error) as ApiError | null;
  const slotCount = clip.ref_slots?.length ?? 0;
  const filled = clip.ref_slots?.filter((slot) => slot.asset_id).length ?? 0;

  return (
    <tr>
      <td>
        {clip.idx}
        {clip.source_idx !== clip.idx && <span className="mj-src" title="播放序 ≠ 源片段号">源{clip.source_idx}</span>}
      </td>
      <td>{clip.title ?? <span className="mj-dash">—</span>}</td>
      <td className="num">{clip.duration_s}s</td>
      <td>
        <span className={`mj-chip ${clip.status}`}>{clip.status}</span>
      </td>
      <td className="num">{clip.shots?.length ?? 0}</td>
      <td className="num" title={`${filled}/${slotCount} 个槽位已回填`}>
        {filled}/{slotCount}
      </td>
      <td className="num">¥{clip.cost_total}</td>
      <td className="mj-actions">
        {canWrite && clip.status === "review" && (
          <>
            <button onClick={() => approve.mutate()} disabled={approve.isPending}>
              通过
            </button>
            <button onClick={() => reject.mutate()} disabled={reject.isPending} title="打回后可改提示词再重发">
              打回
            </button>
          </>
        )}
        <button onClick={onSlots}>槽位</button>
        {clip.status === "rejected" ? (
          <button onClick={() => onPayPoint("reroll")} disabled={!canWrite || !clip.prompt}>
            重发…
          </button>
        ) : (
          <button onClick={() => onPayPoint("generate")} disabled={!canWrite || !clip.prompt}>
            生成…
          </button>
        )}
        {failure && <span className="mj-error">{failure.message}</span>}
      </td>
    </tr>
  );
}

function PayPoint({
  clip,
  mode,
  canWrite,
  onClose,
}: {
  clip: Clip;
  mode: "generate" | "reroll";
  canWrite: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [key] = useState(idemKey);
  const [budget, setBudget] = useState("12.000000");
  const [jobId, setJobId] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  // 失败和"已排队"以前共用一条绿色提示，用户看不出请求到底成没成
  const report = (text: string, isError = false) => {
    setMessage(text);
    setFailed(isError);
  };

  const submit = useMutation({
    mutationFn: () =>
      mode === "reroll" ? api.reroll(clip, budget, key) : api.generate(clip, budget, key),
    onSuccess: (data) => {
      setJobId(data.job.id);
      report(
        `${mode === "reroll" ? "已重发" : "已排队"}：预估 ¥${data.preview.cost_est} · ${data.preview.tokens_est} tokens`,
      );
    },
    onError: (error: ApiError) => report(error.message, true),
  });

  const confirm = useMutation({
    mutationFn: () => api.confirm(jobId!),
    onSuccess: () => {
      report("已确认，等待 worker 执行");
      queryClient.invalidateQueries({ queryKey: ["clips"] });
    },
    onError: (error: ApiError) => report(error.message, true),
  });

  const cancel = useMutation({
    mutationFn: () => api.cancel(jobId!),
    onSuccess: (data) => {
      report(`已取消（${data.state}）`);
      queryClient.invalidateQueries({ queryKey: ["clips"] });
    },
    onError: (error: ApiError) => report(error.message, true),
  });

  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.job(jobId!),
    enabled: jobId !== null,
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      return state && ["done", "failed", "cancelled", "paused_budget"].includes(state) ? false : 2000;
    },
  });

  useEffect(() => {
    if (job.data && TERMINAL.includes(job.data.state)) {
      queryClient.invalidateQueries({ queryKey: ["clips"] });
      queryClient.invalidateQueries({ queryKey: ["cost"] });
      queryClient.invalidateQueries({ queryKey: ["revisions"] });
    }
  }, [job.data?.state, queryClient]);

  // P0-8：回滚到某一版已有产物（不花钱、不写新 revision），回滚后片段回到 review
  const rollback = useMutation({
    mutationFn: (revisionId: number) => api.rollbackClip(clip, revisionId),
    onSuccess: (updated) => {
      report(`已回滚到 revision ${updated.current_revision_id}，片段回到 ${updated.status}`);
      queryClient.invalidateQueries({ queryKey: ["clips"] });
      queryClient.invalidateQueries({ queryKey: ["revisions", clip.id] });
    },
    onError: (error: ApiError) => report(error.message, true),
  });

  const diff = useMutation({
    mutationFn: ([left, right]: [number, number]) => api.revisionDiff(left, right),
    // 成功就清掉上一条提示：否则"服务内部错误"会一直挂在 diff 上方
    onSuccess: () => {
      setMessage(null);
      setFailed(false);
    },
    onError: (error: ApiError) => report(error.message, true),
  });

  // 出片后立刻能看到"哪条验收点没过、为什么"，否则质检结论只活在 DB 里。
  // 重发前也要能看：否则"先看版本链再决定重发"这件事在界面上做不到。
  const revisions = useQuery({
    queryKey: ["revisions", clip.id],
    queryFn: () => api.revisions(clip.id),
    enabled: mode === "reroll" || (job.data !== undefined && TERMINAL.includes(job.data.state)),
  });
  const take = revisions.data?.items[0];
  const current = rollback.data ?? clip;

  // P1-4：出片后能看到 claim → provider → asset → review → decision 的每一步耗时
  const trace = useQuery({
    queryKey: ["trace", jobId],
    queryFn: () => api.jobTrace(jobId!),
    enabled: jobId !== null && job.data !== undefined && TERMINAL.includes(job.data.state),
  });

  return (
    <div className="mj-modal" role="dialog" aria-modal="true">
      <div className="mj-modal-body">
        <h2>付费点 · 片段 {clip.idx}</h2>
        <p className="mj-hint">{clip.title}</p>
        <label>
          预算上限（元）
          <input value={budget} onChange={(event) => setBudget(event.target.value)} disabled={jobId !== null || submit.isPending} />
        </label>
        {message && <p className={failed ? "mj-error" : "mj-msg"}>{message}</p>}
        {job.data && (
          <dl className="mj-job">
            <dt>状态</dt>
            <dd>{job.data.state}</dd>
            <dt>已花</dt>
            <dd>¥{job.data.cost_used}</dd>
            <dt>尝试</dt>
            <dd>
              {job.data.attempts}/{job.data.max_attempts}
            </dd>
            {job.data.error_code && (
              <>
                <dt>错误</dt>
                <dd className="mj-error">{job.data.error_code}</dd>
              </>
            )}
          </dl>
        )}
        {take && <QcPanel take={take} />}
        {(trace.data?.length ?? 0) > 0 && <TraceList entries={trace.data!} />}
        {(revisions.data?.items.length ?? 0) > 1 && (
          <div className="mj-takes">
            <h3>版本链</h3>
            <ul>
              {(revisions.data?.items ?? []).map((item, index, all) => (
                <li key={item.id} className={item.id === current.current_revision_id ? "on" : ""}>
                  <span className="mj-take-seq">v{item.seq ?? "?"}</span>
                  <span className="mj-take-cost">¥{item.cost}</span>
                  <span className="mj-take-head">{item.prompt?.slice(0, 28) ?? "—"}…</span>
                  {index + 1 < all.length && (
                    <button onClick={() => diff.mutate([all[index + 1].id, item.id])}>对比上一版</button>
                  )}
                  <button
                    onClick={() => rollback.mutate(item.id)}
                    disabled={!canWrite || rollback.isPending || item.id === current.current_revision_id}
                  >
                    {item.id === current.current_revision_id ? "当前" : "回滚到此版"}
                  </button>
                </li>
              ))}
            </ul>
            {diff.data && <pre className="mj-diff">{diff.data.prompt_diff || "（提示词完全相同）"}</pre>}
          </div>
        )}
        <div className="mj-actions">
          {jobId === null ? (
            <button onClick={() => submit.mutate()} disabled={submit.isPending}>
              提交（不产生计费）
            </button>
          ) : (
            <button onClick={() => confirm.mutate()} disabled={confirm.isPending || job.data?.state !== "waiting_confirm"}>
              确认付费
            </button>
          )}
          {jobId !== null && !(job.data && TERMINAL.includes(job.data.state)) && (
            <button onClick={() => cancel.mutate()} disabled={cancel.isPending}>
              取消
            </button>
          )}
          <button onClick={onClose}>关闭</button>
        </div>
        <p className="mj-hint">确认前不产生任何计费；取消后 job 为 cancelled。</p>
      </div>
    </div>
  );
}

function detailText(detail: Record<string, unknown> | null): string {
  if (!detail) return "";
  return Object.entries(detail)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${key}=${Array.isArray(value) ? (value.length ? value.join(",") : "—") : String(value)}`)
    .join(" · ")
    .slice(0, 120);
}

/** P1-4 的执行时间线。它回答"这一步花了多久、判成什么"，不承担审计职责。 */
function TraceList({ entries }: { entries: TraceEntry[] }) {
  return (
    <div className="mj-trace">
      <h3>执行时间线</h3>
      <ol>
        {entries.map((entry, index) => (
          <li key={index}>
            <span className="mj-trace-step">{entry.step}</span>
            {entry.verdict && <b className={`mj-qc-${entry.verdict}`}>{entry.verdict}</b>}
            {entry.ms !== null && <em>{entry.ms}ms</em>}
            <span className="mj-trace-detail">{detailText(entry.detail)}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function QcPanel({ take }: { take: Revision }) {
  const results = take.acceptance_result ?? [];
  if (results.length === 0) return null;
  const pending = results.filter((item) => item.result === "unknown").length;
  return (
    <div className="mj-qc">
      <h3>质检结论 · 第 {take.seq ?? "?"} 次产物</h3>
      {take.motion && (
        <p className="mj-hint">
          动线 {take.motion.samples ?? "—"} 采样点 · avg {take.motion.avg ?? "—"} · max {take.motion.max ?? "—"} · 冻结{" "}
          {take.motion.freeze ?? 0}
        </p>
      )}
      <ul>
        {results.map((item) => (
          <li key={item.id} className={`mj-qc-item mj-qc-${item.result}`}>
            <b>{item.result}</b>
            <span>{item.evidence}</span>
          </li>
        ))}
      </ul>
      {pending > 0 && <p className="mj-hint">{pending} 条判不出来：判不了不等于通过，片段留在 review 等人看。</p>}
    </div>
  );
}

/** provider 当前接的是什么、哪条通道是真的。视频通道如实标非真（还没接真视频厂商）。 */
function ProviderPanel() {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ["providerStatus"], queryFn: api.providerStatus });
  const check = useMutation({ mutationFn: api.checkProvider });
  const toggle = useMutation({
    mutationFn: (input: { channel: ChannelName; enabled: boolean; reason?: string }) =>
      api.setProviderState(input.channel, input.enabled, input.reason),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["providerStatus"] }),
  });
  if (status.isLoading) return <div className="boot">载入 provider 状态…</div>;
  if (status.isError) return <div className="boot">{(status.error as ApiError).message}</div>;
  const data = status.data!;
  const channel = (name: ChannelName, label: string, state: ProviderChannel) => (
    <li>
      <b>{label}</b>
      <span className={state.real ? "mj-real" : "mj-fake"}>{state.real ? "真调用" : "mock"}</span>
      <em>{state.model}</em>
      <button
        className={state.enabled ? "" : "on"}
        disabled={toggle.isPending}
        onClick={() => {
          const reason = state.enabled ? window.prompt("停用原因（会记在状态表里，给下一个值班的人看）") : null;
          if (state.enabled && !reason) return;
          toggle.mutate({ channel: name, enabled: !state.enabled, reason: reason ?? undefined });
        }}
      >
        {state.enabled ? "停用" : "启用"}
      </button>
      {!state.enabled && (
        <span className="mj-error" title={`actor #${state.actor_id ?? "?"} · ${state.updated_at ?? ""}`}>
          已停用：{state.reason ?? "（未写原因）"}
        </span>
      )}
    </li>
  );
  return (
    <div className="mj-provider">
      <h3>
        provider · {data.provider}
        {data.base_url && <span className="mj-hint"> {data.base_url}</span>}
      </h3>
      <ul>
        {channel("text", "文本", data.text)}
        {channel("vision", "视觉", data.vision)}
        {channel("video", "视频", data.video)}
        {channel("image", "出图", data.image)}
      </ul>
      <div className="mj-actions">
        <button onClick={() => check.mutate()} disabled={check.isPending}>
          {check.isPending ? "检查中…" : "连通性检查"}
        </button>
      </div>
      {check.data && (
        <p className={check.data.ok ? "mj-msg" : "mj-error"}>
          {check.data.ok ? "✓ " : "✗ "}
          {check.data.detail}
          {check.data.latency_ms !== null && ` · ${check.data.latency_ms}ms`}
        </p>
      )}
      <p className="mj-hint">{data.note}</p>
    </div>
  );
}

/** P1-1：admin 维护 Skill 的唯一入口。发布是单向门 —— 没试跑成功的版本点不动"发布"。 */
function SkillEditor() {
  const queryClient = useQueryClient();
  const skills = useQuery({ queryKey: ["skills"], queryFn: api.skills });
  const [skillId, setSkillId] = useState<number | null>(null);
  const skill = skills.data?.find((item) => item.id === skillId) ?? skills.data?.[0] ?? null;

  const versions = useQuery({
    queryKey: ["skillVersions", skill?.id],
    queryFn: () => api.skillVersions(skill!.id),
    enabled: Boolean(skill),
  });

  const [body, setBody] = useState("");
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const report = (text: string, isError = false) => {
    setMessage(text);
    setFailed(isError);
  };
  const [preview, setPreview] = useState<{ version: number; data: DryRun } | null>(null);

  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const [projectId, setProjectId] = useState<number | null>(null);
  const activeProject = projects.data?.items.find((item) => item.id === projectId) ?? projects.data?.items[0] ?? null;
  const episodes = useQuery({
    queryKey: ["episodes", activeProject?.id],
    queryFn: () => api.episodes(activeProject!.id),
    enabled: Boolean(activeProject),
  });
  const [episodeId, setEpisodeId] = useState<number | null>(null);
  const activeEpisode = episodes.data?.find((item) => item.id === episodeId) ?? episodes.data?.[0] ?? null;
  const clips = useQuery({
    queryKey: ["clips", activeEpisode?.id],
    queryFn: () => api.clips(activeEpisode!.id),
    enabled: Boolean(activeEpisode),
  });
  const [clipId, setClipId] = useState<number | null>(null);
  const activeClip = clips.data?.find((item) => item.id === clipId) ?? clips.data?.[0] ?? null;

  // 一个 Skill 可以服务多个 stage（种子里 story-to-script 同时供 S1/S2/S3），
  // 所以试跑目标由**绑定关系**决定，不是由 skill.stage 猜
  const templates = useQuery({ queryKey: ["stepTemplates"], queryFn: api.stepTemplates });
  const bound = (templates.data ?? []).filter((item) => item.skill === skill?.name);
  const [stageCode, setStageCode] = useState<string | null>(null);
  const stage =
    bound.find((item) => item.stage === stageCode) ?? bound.find((item) => item.stage === skill?.stage) ?? bound[0] ?? null;
  const targetKind = stage?.target ?? null;
  const targetPayload =
    targetKind === "project"
      ? activeProject && { project_id: activeProject.id }
      : targetKind === "episode"
        ? activeEpisode && { episode_id: activeEpisode.id }
        : targetKind === "clip"
          ? activeClip && { clip_id: activeClip.id }
          : null;

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["skillVersions", skill?.id] });
    queryClient.invalidateQueries({ queryKey: ["skills"] });
  };

  const create = useMutation({
    mutationFn: () => api.createSkillVersion(skill!.id, body, note.trim() || null),
    onSuccess: (data) => {
      setBody("");
      setNote("");
      report(
        `已建 v${data.skill_version.version}：${data.skill_version.sections} 节 / ${data.skill_version.tokens_total} tokens（draft，未发布）`,
      );
      refresh();
    },
    onError: (error: ApiError) => report(error.message, true),
  });

  const dry = useMutation({
    mutationFn: (version: number) => api.dryRunSkill(skill!.id, version, targetPayload ?? {}),
    onSuccess: (data, version) => {
      setPreview({ version, data });
      report(`v${version} 试跑通过：可发布`);
      refresh();
    },
    onError: (error: ApiError) => report(error.message, true),
  });

  const publish = useMutation({
    mutationFn: (version: number) => api.publishSkillVersion(skill!.id, version),
    onSuccess: (data) => {
      report(`已发布 v${data.skill.active_version}${data.superseded ? `（v${data.superseded} 转为 superseded）` : ""}`);
      refresh();
    },
    onError: (error: ApiError) => report(error.message, true),
  });

  const rollback = useMutation({
    mutationFn: (version: number) => api.rollbackSkill(skill!.id, version),
    onSuccess: (data) => {
      report(`已回滚到 v${data.skill.active_version}`);
      refresh();
    },
    onError: (error: ApiError) => report(error.message, true),
  });

  const toggle = useMutation({
    mutationFn: () => api.patchSkill(skill!.id, skill!.status === "active" ? "disabled" : "active"),
    onSuccess: (updated) => {
      report(`技能已${updated.status === "active" ? "启用" : "停用"}`);
      refresh();
    },
    onError: (error: ApiError) => report(error.message, true),
  });

  if (skills.isLoading) return <div className="boot">载入 Skill…</div>;
  if (skills.isError) return <div className="boot">{(skills.error as ApiError).message}</div>;
  if (!skill) return <div className="boot">库里还没有 Skill。先跑一次 seed（scripts/seed_skills.py）。</div>;

  const rows = versions.data ?? [];

  return (
    <div className="mj-skill">
      <aside className="mj-skill-list">
        {(skills.data ?? []).map((item) => (          <button
            key={item.id}
            className={item.id === skill.id ? "on" : ""}
            onClick={() => {
              setSkillId(item.id);
              setPreview(null);
              setMessage(null);
            }}
          >
            <b>{item.name}</b>
            <span>
              {item.stage} · {item.status === "active" ? "启用" : "停用"}
            </span>
          </button>
        ))}
      </aside>

      <section className="mj-skill-main">
        <ProviderPanel />
        <header>
          <div>
            <strong>{skill.name}</strong>
            <span className="mj-hint">
              {" "}
              · stage {skill.stage} · {skill.status === "active" ? "启用中" : "已停用（不参与召回）"}
            </span>
          </div>
          <div className="mj-actions">
            <button onClick={() => toggle.mutate()} disabled={toggle.isPending}>
              {skill.status === "active" ? "停用" : "启用"}
            </button>
          </div>
        </header>

        {message && <p className={failed ? "mj-error" : "mj-msg"}>{message}</p>}

        <table className="mj-table">
          <thead>
            <tr>
              <th>版本</th>
              <th>状态</th>
              <th className="num">节</th>
              <th>试跑</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>v{row.version}</td>
                <td>
                  <span className={`mj-chip ${row.state === "active" ? "approved" : row.state === "draft" ? "review" : ""}`}>
                    {row.state}
                  </span>
                </td>
                <td className="num">{row.sections.length}</td>
                <td className="mj-hint">{row.dry_run_ok_at ? "已试跑" : "未试跑"}</td>
                <td className="mj-actions">
                  <button
                    onClick={() => dry.mutate(row.version)}
                    disabled={dry.isPending || targetKind === null || !targetPayload}
                    title={targetKind === null ? "质检技能不走 steps 组装，无法试跑" : ""}
                  >
                    试跑
                  </button>
                  <button
                    onClick={() => publish.mutate(row.version)}
                    disabled={publish.isPending || row.state !== "draft" || !row.dry_run_ok_at}
                  >
                    发布
                  </button>
                  <button
                    onClick={() => rollback.mutate(row.version)}
                    disabled={rollback.isPending || row.state === "active"}
                  >
                    回滚到此版
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="mj-dryrun-target">
          <label>
            注入到
            <select
              value={stage?.stage ?? ""}
              onChange={(event) => setStageCode(event.target.value)}
              disabled={bound.length === 0}
            >
              {bound.length === 0 && <option value="">该技能未被任何 step 绑定</option>}
              {bound.map((item) => (
                <option key={item.stage} value={item.stage}>
                  {item.stage} {item.label}（{item.target}）
                </option>
              ))}
            </select>
          </label>
          <label>
            项目
            <select value={activeProject?.id ?? ""} onChange={(event) => setProjectId(Number(event.target.value))}>
              {(projects.data?.items ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.title}
                </option>
              ))}
            </select>
          </label>
          <label>
            集
            <select
              value={activeEpisode?.id ?? ""}
              onChange={(event) => setEpisodeId(Number(event.target.value))}
              disabled={!episodes.data?.length || targetKind === "project"}
            >
              {(episodes.data ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  第 {item.idx} 集
                </option>
              ))}
            </select>
          </label>
          <label>
            片段
            <select
              value={activeClip?.id ?? ""}
              onChange={(event) => setClipId(Number(event.target.value))}
              disabled={!clips.data?.length || targetKind !== "clip"}
            >
              {(clips.data ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.idx}. {item.title ?? "未命名"}
                </option>
              ))}
            </select>
          </label>
        </div>

        {preview && (
          <div className="mj-dryrun">
            <h3>试跑结果 · v{preview.version}</h3>
            <p className="mj-hint">
              预算 {preview.data.budget.used_by_skill} / {preview.data.budget.input_limit} tokens（全上下文{" "}
              {preview.data.budget.used_total}）{preview.data.budget.trimmed && " · 有节因预算被裁"}
            </p>
            <ul>
              {preview.data.injected.map((section) => (
                <li key={section.rule_id} className={section.selected ? "on" : ""}>
                  <b>{section.rule_id}</b>
                  <span>{section.title}</span>
                  <em>{section.tokens_est}</em>
                  {!section.selected && <em className="mj-hint">未进</em>}
                </li>
              ))}
            </ul>
            <p className="mj-hint">
              相对当前 active：新增 {preview.data.diff_vs_active.added_rules.join("、") || "无"} · 移除{" "}
              {preview.data.diff_vs_active.removed_rules.join("、") || "无"} · tokens {preview.data.diff_vs_active.tokens_delta >= 0 ? "+" : ""}
              {preview.data.diff_vs_active.tokens_delta}
            </p>
            <pre className="mj-payload">{preview.data.prompt_preview.slice(0, 1200)}</pre>
          </div>
        )}

        <div className="mj-skill-new">
          <h3>新建版本（存为 draft，不生效）</h3>
          <textarea
            value={body}
            onChange={(event) => setBody(event.target.value)}
            placeholder={"## #110 巨物压迫感四件套\n远景先给高度差…\n\n## #112 机位位移＋背景相对位移\n…"}
            rows={8}
          />
          <div className="mj-actions">
            <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="变更说明（可选）" maxLength={200} />
            <button onClick={() => create.mutate()} disabled={create.isPending || !body.trim()}>
              建版本
            </button>
          </div>
          <p className="mj-hint">
            正文按 `##` 切节；出现脚本围栏会被拒（422 SKILL_TOOL_UNKNOWN）。发布前必须试跑 —— 这是协议层的单向门，不是界面约束。
          </p>
        </div>
      </section>
    </div>
  );
}

/** 槽位指派：从**项目级**资产库里挑图挂到 clip 的参考槽位上（跨集复用就发生在这里）。 */
function SlotPanel({
  clip,
  projectId,
  canWrite,
  onClose,
}: {
  clip: Clip;
  projectId: number;
  canWrite: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const assets = useQuery({
    queryKey: ["assets", projectId, "image"],
    queryFn: () => api.assets(projectId, "image"),
  });
  const [draft, setDraft] = useState<Slot[]>(clip.ref_slots ?? []);
  const [message, setMessage] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  const save = useMutation({
    mutationFn: () =>
      api.saveSlots(
        clip,
        draft.map(({ url: _url, thumb_url: _thumb, ...rest }) => rest),
      ),
    onSuccess: () => {
      setMessage("已保存");
      setFailed(false);
      queryClient.invalidateQueries({ queryKey: ["clips"] });
      queryClient.invalidateQueries({ queryKey: ["projectClips"] });
    },
    onError: (error: ApiError) => {
      setMessage(error.message);
      setFailed(true);
    },
  });

  const assign = (idx: number, assetId: number | null) =>
    setDraft((prev) =>
      prev.map((slot) => (slot.idx === idx && slot.kind === "image" ? { ...slot, asset_id: assetId } : slot)),
    );

  return (
    <div className="mj-modal" role="dialog" aria-modal="true">
      <div className="mj-modal-body mj-modal-wide">
        <h2>
          参考图槽位 · 片段 {clip.idx} {clip.title ?? ""}
        </h2>
        <p className="mj-hint">
          资产库是项目级的：这里能挑到别的集已经用过的图，同一个 asset_id 被多个片段引用，不复制文件。
        </p>
        {draft.length === 0 && <p className="mj-hint">这个片段没有参考槽位（分镜文档里未写「参考图」段）。</p>}
        <ul className="mj-slots">
          {draft.map((slot) => {
            const current = (assets.data?.items ?? []).find((item) => item.id === slot.asset_id);
            return (
              <li key={`${slot.kind}-${slot.idx}`}>
                <b>
                  {slot.kind} {slot.idx}
                </b>
                <span className="mj-slot-role">{slot.role}</span>
                <span className="mj-slot-path" title={slot.asset_ref ?? ""}>
                  {current?.path ?? slot.asset_ref ?? "未回填"}
                </span>
                {slot.kind === "image" ? (
                  <select
                    value={slot.asset_id ?? ""}
                    onChange={(event) => assign(slot.idx, event.target.value ? Number(event.target.value) : null)}
                    disabled={!canWrite}
                  >
                    <option value="">（不指派）</option>
                    {(assets.data?.items ?? []).map((item) => (
                      <option key={item.id} value={item.id}>
                        #{item.id} {item.path}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span className="mj-hint">视频/音频槽位暂不支持指派</span>
                )}
              </li>
            );
          })}
        </ul>
        {message && <p className={failed ? "mj-error" : "mj-msg"}>{message}</p>}
        <div className="mj-actions">
          <button onClick={() => save.mutate()} disabled={!canWrite || save.isPending || draft.length === 0}>
            保存
          </button>
          <button onClick={onClose}>关闭</button>
        </div>
      </div>
    </div>
  );
}

/** 资产库：项目级素材（跨集共用）。删除会被后端引用检查拦住 —— 这里如实展示"为什么删不掉"。 */
function AssetLibrary({ project, canWrite }: { project: Project; canWrite: boolean }) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<"all" | "image" | "video">("all");
  const [blocked, setBlocked] = useState<{ path: string; usages: Usage[] } | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  const assets = useQuery({
    queryKey: ["assets", project.id, kind],
    queryFn: () => api.assets(project.id, kind === "all" ? undefined : kind),
  });

  const remove = useMutation({
    mutationFn: ({ id, purge }: { id: number; purge: boolean }) => api.deleteAsset(id, purge),
    onSuccess: () => {
      setBlocked(null);
      setMessage("已删除");
      setFailed(false);
      queryClient.invalidateQueries({ queryKey: ["assets"] });
      queryClient.invalidateQueries({ queryKey: ["clips"] });
      queryClient.invalidateQueries({ queryKey: ["projectClips"] });
    },
    onError: (error: ApiError, variables) => {
      const usages = (error.body.detail?.usages as Usage[] | undefined) ?? [];
      const path = (assets.data?.items ?? []).find((item) => item.id === variables.id)?.path ?? `#${variables.id}`;
      setBlocked(usages.length ? { path, usages } : null);
      setMessage(error.message);
      setFailed(true);
    },
  });

  if (assets.isLoading) return <div className="boot">载入资产…</div>;
  if (assets.isError) return <div className="boot">{(assets.error as ApiError).message}</div>;
  const items = assets.data?.items ?? [];

  return (
    <div>
      <div className="mj-wall-head">
        {(
          [
            ["all", "全部"],
            ["image", "图片"],
            ["video", "视频"],
          ] as ["all" | "image" | "video", string][]
        ).map(([key, label]) => (
          <button key={key} className={kind === key ? "on" : ""} onClick={() => setKind(key)}>
            {label}
          </button>
        ))}
        <span className="mj-hint">{items.length} 个资产 · 项目级，所有集共用</span>
      </div>

      {message && (
        <p className={failed ? "mj-error" : "mj-msg"} style={{ padding: "8px 24px 0" }}>
          {message}
        </p>
      )}
      {blocked && (
        <div className="mj-usages">
          <b>「{blocked.path}」正被引用，删不掉：</b>
          <ul>
            {blocked.usages.slice(0, 6).map((usage, index) => (
              <li key={index}>
                {usage.clip_id !== undefined
                  ? `片段 #${usage.clip_id}（第 ${usage.clip_idx} 段 · 集 ${usage.episode_id}）的槽位 ${usage.slot_idx}`
                  : `take revision #${usage.revision_id} 的产物`}
              </li>
            ))}
            {blocked.usages.length > 6 && <li>…共 {blocked.usages.length} 处</li>}
          </ul>
          <span className="mj-hint">先把这些槽位改掉再删；物理文件默认保留。</span>
        </div>
      )}

      <div className="mj-wall">
        {items.map((item) => (
          <figure key={item.id} className="mj-card">
            {/* 视频有海报就贴海报；没海报再退回"视频 · 宽×高"占位 */}
            {item.kind === "video" && !item.thumb_path ? (
              <div className="mj-noimg">视频 · {item.w ?? "?"}×{item.h ?? "?"}</div>
            ) : (
              <img src={item.thumb_url} alt={item.path} loading="lazy" />
            )}
            <figcaption>
              <b title={item.path}>{item.path.split(/[\\/]/).pop()}</b>
              <span>
                #{item.id} · {item.kind} · {(item.bytes / 1024).toFixed(0)} KB · {item.thumb_path ? "有海报" : "无海报"}
              </span>
              {canWrite && (
                <div className="mj-actions">
                  <button onClick={() => remove.mutate({ id: item.id, purge: false })} disabled={remove.isPending}>
                    删除
                  </button>
                  {item.path.startsWith(".gen") && (
                    <button onClick={() => remove.mutate({ id: item.id, purge: true })} disabled={remove.isPending}>
                      连文件删
                    </button>
                  )}
                </div>
              )}
            </figcaption>
          </figure>
        ))}
      </div>
    </div>
  );
}

/** 镜头墙：默认看**全项目**（多集时这是"跨集一起看"的入口），可切回本集。 */
function Wall({
  projectId,
  episodes,
  episode,
  clips,
  canWrite,
  onPayPoint,
}: {
  projectId: number;
  episodes: Episode[];
  episode: Episode | null;
  clips: Clip[];
  canWrite: boolean;
  onPayPoint: (clip: Clip, mode: "generate" | "reroll") => void;
}) {
  const [scope, setScope] = useState<"project" | "episode">("project");
  const all = useQuery({
    queryKey: ["projectClips", projectId],
    queryFn: () => api.projectClips(projectId),
    enabled: scope === "project",
  });
  // 全项目按接口给的是 id 倒序；镜头墙要按"第几集第几段"读，才和本集视图一个口径
  const episodeOrder = new Map(episodes.map((item, index) => [item.id, index]));
  const rows =
    scope === "project"
      ? [...(all.data?.items ?? [])].sort(
          (a, b) =>
            (episodeOrder.get(a.episode_id) ?? 0) - (episodeOrder.get(b.episode_id) ?? 0) || a.idx - b.idx,
        )
      : clips;
  const episodeIdx = (id: number) => episodes.find((item) => item.id === id)?.idx ?? "?";

  return (
    <div>
      <div className="mj-wall-head">
        <button className={scope === "project" ? "on" : ""} onClick={() => setScope("project")}>
          全项目（{episodes.length} 集）
        </button>
        <button className={scope === "episode" ? "on" : ""} onClick={() => setScope("episode")}>
          本集（第 {episode?.idx ?? "?"} 集）
        </button>
        <span className="mj-hint">{rows.length} 个片段</span>
      </div>
      <div className="mj-wall">
        {rows.map((clip) => (
          <figure key={clip.id} className="mj-card">
            {clip.output_asset_id ? (
              <WallThumb assetId={clip.output_asset_id} title={clip.title ?? `片段 ${clip.idx}`} />
            ) : (
              <div className="mj-noimg">无产物</div>
            )}
            <figcaption>
              <b>
                {scope === "project" && <span className="mj-src">第{episodeIdx(clip.episode_id)}集</span>}
                {clip.idx}. {clip.title ?? "未命名"}
              </b>
              <span>
                {clip.status} · {clip.duration_s}s · ¥{clip.cost_total}
              </span>
              {canWrite && (
                <button
                  onClick={() => onPayPoint(clip, clip.status === "rejected" ? "reroll" : "generate")}
                  disabled={!clip.prompt}
                >
                  {clip.status === "rejected" ? "重发…" : "生成…"}
                </button>
              )}
            </figcaption>
          </figure>
        ))}
      </div>
    </div>
  );
}

/** 出片产物多是视频；没有海报时 `?thumb=1` 会原样回一段 mp4，`<img>` 接不住。 */
function WallThumb({ assetId, title }: { assetId: number; title: string }) {
  const [broken, setBroken] = useState(false);
  if (broken) return <div className="mj-noimg">视频产物</div>;
  return <img src={thumbUrl(assetId)!} alt={title} loading="lazy" onError={() => setBroken(true)} />;
}

function CostTable({ projectId }: { projectId: number }) {
  const [groupBy, setGroupBy] = useState<"model" | "day" | "clip" | "episode">("model");
  const cost = useQuery({
    queryKey: ["cost", projectId, groupBy],
    queryFn: () => api.cost(projectId, groupBy),
  });
  if (cost.isLoading) return <div className="boot">载入成本…</div>;
  if (cost.isError) return <div className="boot">{(cost.error as ApiError).message}</div>;
  return (
    <>
      <div className="mj-wall-head">
        {(
          [
            ["model", "按模型"],
            ["episode", "按集"],
            ["clip", "按片段"],
            ["day", "按天"],
          ] as [typeof groupBy, string][]
        ).map(([key, label]) => (
          <button key={key} className={groupBy === key ? "on" : ""} onClick={() => setGroupBy(key)}>
            {label}
          </button>
        ))}
      </div>
      <table className="mj-table">
        <thead>
          <tr>
            <th>{groupBy === "model" ? "模型" : groupBy === "episode" ? "集" : groupBy === "clip" ? "片段" : "日期"}</th>
            <th className="num">调用</th>
            <th className="num">tokens</th>
            <th className="num">秒数</th>
            <th className="num">金额</th>
          </tr>
        </thead>
        <tbody>
          {(cost.data?.items ?? []).map((item) => (
            <tr key={item.key}>
              <td>{item.key}</td>
              <td className="num">{item.calls}</td>
              <td className="num">{item.tokens.toLocaleString()}</td>
              <td className="num">{item.seconds}s</td>
              <td className="num">¥{item.cost}</td>
            </tr>
          ))}
          <tr className="mj-sum">
            <td>合计</td>
            <td className="num">{cost.data?.total.calls}</td>
            <td />
            <td />
            <td className="num">¥{cost.data?.total.cost}</td>
          </tr>
        </tbody>
      </table>
    </>
  );
}
