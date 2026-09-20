/**
 * 节点右侧抽屉：按节点类型显示专用按钮、需求框、模型与发送/抽卡。
 *
 * 「发送按钮锁死」由服务端判据驱动：这里只是读 `GET /episodes/{id}/submissions` 里
 * 该节点是否还有未结束的提交，然后禁用按钮。真正的拦截在后端。
 */
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Divider,
  Input,
  InputNumber,
  List,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Tag,
  Typography,
  message,
} from 'antd'

import { apiDelete, apiGet, apiPatch, apiPost, newIdempotencyKey } from '../api/client'
import type {
  CanvasNodeDetail,
  ContentDocumentDetail,
  Submission,
  SubmissionDetail,
} from '../api/types'
import { canEdit, useAuth } from '../auth/AuthContext'

interface Props {
  episodeId: string
  detail?: CanvasNodeDetail
  onChanged: () => void
}

const ACTIVE = new Set(['pending', 'running'])

export default function NodeDrawer({ episodeId, detail, onChanged }: Props) {
  const { user } = useAuth()
  const editable = canEdit(user?.role)
  const queryClient = useQueryClient()

  const [requirement, setRequirement] = useState('')
  const [count, setCount] = useState(1)
  const [seed, setSeed] = useState(-1)
  const [promptDraft, setPromptDraft] = useState('')
  const [documentDraft, setDocumentDraft] = useState('')
  const [trackedSubmission, setTrackedSubmission] = useState<string | null>(null)

  const node = detail?.node
  const documentDetail: ContentDocumentDetail | undefined = detail?.document as
    | ContentDocumentDetail
    | undefined
  const clipDetail = detail?.clip as
    | { clip: { id: string; title: string; body: string; characters: string[]; locationName: string; durationMs: number }; shots: unknown[]; references: unknown[] }
    | undefined
  const mediaDetail = detail?.mediaNode as
    | import('../api/types').MediaNodeDetail
    | undefined

  // 产线规范 lint 结果：由后端按 AI漫剧制作工作流执行手册 的硬规则检查后挂在方案节点上
  const promptWarnings = ((mediaDetail?.sourceSummary ?? {}) as {
    promptWarnings?: { code: string; message: string }[]
  }).promptWarnings ?? []

  const submissions = useQuery({
    queryKey: ['submissions', episodeId],
    queryFn: () =>
      apiGet<{ items: Submission[] }>(`/episodes/${episodeId}/submissions?pageSize=50`),
    refetchInterval: 2000,
  })

  const activeForNode = useMemo(
    () =>
      submissions.data?.items.find(
        (item) => item.nodeId === node?.id && ACTIVE.has(item.logicalStatus),
      ),
    [submissions.data, node?.id],
  )

  const latestForNode = useMemo(
    () => submissions.data?.items.find((item) => item.nodeId === node?.id),
    [submissions.data, node?.id],
  )

  const watched = trackedSubmission ?? activeForNode?.id ?? latestForNode?.id ?? null

  const submissionDetail = useQuery({
    queryKey: ['submission', watched],
    queryFn: () => apiGet<SubmissionDetail>(`/submissions/${watched}`),
    enabled: Boolean(watched),
    refetchInterval: (query) => {
      const status = query.state.data?.submission.logicalStatus
      return status && ACTIVE.has(status) ? 1500 : false
    },
  })

  useEffect(() => {
    setRequirement('')
    setPromptDraft(mediaDetail?.visiblePrompt ?? '')
    setDocumentDraft(documentDetail?.document.body ?? '')
    setTrackedSubmission(null)
  }, [node?.id, mediaDetail?.visiblePrompt, documentDetail?.document.body])

  useEffect(() => {
    const status = submissionDetail.data?.submission.logicalStatus
    if (status && !ACTIVE.has(status)) {
      void queryClient.invalidateQueries({ queryKey: ['canvas', episodeId] })
      void queryClient.invalidateQueries({ queryKey: ['submissions', episodeId] })
      onChanged()
    }
  }, [submissionDetail.data?.submission.logicalStatus, episodeId, queryClient, onChanged])

  const runAction = useMutation({
    mutationFn: async ({ path, body, key }: { path: string; body?: unknown; key: string }) =>
      apiPost<{ submissionId: string }>(path, body ?? {}, newIdempotencyKey(key)),
    onSuccess: (data) => {
      setTrackedSubmission(data.submissionId)
      message.success('任务已受理，正在执行')
      void queryClient.invalidateQueries({ queryKey: ['submissions', episodeId] })
    },
    onError: (error: Error) => message.error(error.message),
  })

  const saveDraft = useMutation({
    mutationFn: () =>
      apiPatch(`/documents/${documentDetail?.document.id}/draft`, {
        body: documentDraft,
        expectedVersion: documentDetail?.document.version,
      }),
    onSuccess: () => {
      message.success('草稿已保存')
      onChanged()
    },
    onError: (error: Error) => message.error(error.message),
  })

  const saveRevision = useMutation({
    mutationFn: () =>
      apiPost(`/documents/${documentDetail?.document.id}/revisions`, {
        body: documentDraft,
        source: 'user',
      }),
    onSuccess: () => {
      message.success('已保存为不可变版本')
      onChanged()
    },
    onError: (error: Error) => message.error(error.message),
  })

  const savePrompt = useMutation({
    mutationFn: () =>
      apiPost(`/media-nodes/${mediaDetail?.mediaNode.id}/prompt-versions`, {
        visibleText: promptDraft,
      }),
    onSuccess: () => {
      message.success('已追加方案版本（未出图）')
      onChanged()
    },
    onError: (error: Error) => message.error(error.message),
  })

  const toggleBlocked = useMutation({
    mutationFn: (blocked: boolean) =>
      apiPatch(`/media-nodes/${mediaDetail?.mediaNode.id}`, { blocked }),
    onSuccess: () => onChanged(),
    onError: (error: Error) => message.error(error.message),
  })

  const toggleHidden = useMutation({
    mutationFn: (hidden: boolean) => apiPatch(`/canvas/nodes/${node?.id}/visibility`, { hidden }),
    onSuccess: () => onChanged(),
    onError: (error: Error) => message.error(error.message),
  })

  const deleteNode = useMutation({
    mutationFn: () => apiDelete(`/canvas/nodes/${node?.id}`),
    onSuccess: () => {
      message.success('节点已移除（内容与媒体保留）')
      onChanged()
    },
    onError: (error: Error) => message.error(error.message),
  })

  if (!node) {
    return (
      <div className="drawer-body">
        <div className="role-note">点击画布上的节点查看内容、专用按钮与任务反馈。</div>
      </div>
    )
  }

  const busy = Boolean(activeForNode) || runAction.isPending
  const kindLabel =
    node.nodeType === 'text'
      ? `TEXT · ${documentDetail?.document.kind ?? ''}`
      : node.nodeType === 'shot'
        ? 'SHOT · CLIP'
        : mediaDetail?.mediaNode.mediaKind === 'image_plan'
          ? 'IMAGE · 方案'
          : mediaDetail?.mediaNode.mediaKind === 'image_result'
            ? 'IMAGE · 结果'
            : mediaDetail?.mediaNode.mediaKind === 'video_plan'
              ? 'VIDEO · 方案'
              : 'VIDEO · 结果'

  const docKind = documentDetail?.document.kind
  const mediaKind = mediaDetail?.mediaNode.mediaKind

  return (
    <>
      <div className="drawer-header">
        <div className="drawer-title">{node.label || '未命名节点'}</div>
        <div className="drawer-subtitle">
          {kindLabel} · {busy ? '执行中' : editable ? '可编辑' : '只读'}
        </div>
      </div>

      <div className="drawer-body">
        {!editable && <div className="role-note">manager 角色只读：所有写操作会被后端拒绝。</div>}

        {/* ---------------- 文本节点 ---------------- */}
        {node.nodeType === 'text' && documentDetail && (
          <>
            <div className="drawer-section-label">当前内容 · v{documentDetail.document.version}</div>
            <Input.TextArea
              rows={8}
              value={documentDraft}
              onChange={(event) => setDocumentDraft(event.target.value)}
              disabled={!editable}
              placeholder="在这里写大纲 / 剧本 / 设定正文"
            />
            <Space style={{ marginTop: 10 }} wrap>
              <Button onClick={() => saveDraft.mutate()} disabled={!editable} loading={saveDraft.isPending}>
                保存草稿
              </Button>
              <Button
                type="primary"
                onClick={() => saveRevision.mutate()}
                disabled={!editable}
                loading={saveRevision.isPending}
              >
                保存为版本
              </Button>
              <Select
                style={{ width: 180 }}
                placeholder="历史版本"
                disabled={!documentDetail.revisions.length}
                value={undefined}
                options={documentDetail.revisions.map((revision) => ({
                  value: revision.id,
                  label: `v${revision.revisionNo} · ${revision.source}`,
                }))}
                onChange={(revisionId) =>
                  void apiPost(`/documents/${documentDetail.document.id}/revisions`, {
                    fromRevisionId: revisionId,
                  })
                    .then(() => {
                      message.success('已载入历史版本（未新增版本）')
                      onChanged()
                    })
                    .catch((error: Error) => message.error(error.message))
                }
              />
            </Space>
            {documentDetail.hasUnsavedDraft && (
              <Alert
                style={{ marginTop: 10 }}
                type="warning"
                showIcon
                message="草稿与当前版本不一致，保存或生成时会新增一个「用户修改」版本"
              />
            )}

            <Divider />
            <div className="drawer-section-label">本次需求</div>
            <Input.TextArea
              rows={2}
              value={requirement}
              onChange={(event) => setRequirement(event.target.value)}
              placeholder="例如：加强雨夜氛围，对白更克制"
              disabled={!editable}
            />
            <Space style={{ marginTop: 10 }} align="center">
              <span style={{ color: '#64748b', fontSize: 12 }}>数量</span>
              <InputNumber min={1} max={8} value={count} onChange={(value) => setCount(value ?? 1)} />
            </Space>
            <div style={{ marginTop: 12 }}>
              <Button
                type="primary"
                block
                disabled={!editable || busy}
                loading={runAction.isPending}
                onClick={() =>
                  runAction.mutate({
                    path: `/documents/${documentDetail.document.id}/actions/optimize`,
                    body: { requirement, count },
                    key: 'doc-optimize',
                  })
                }
              >
                {busy ? '任务进行中…' : '发送'}
              </Button>
            </div>

            <div className="drawer-section-label">专用按钮</div>
            <Space direction="vertical" style={{ width: '100%' }}>
              {docKind === 'outline' && (
                <>
                  <Button
                    block
                    disabled={!editable || busy}
                    onClick={() =>
                      runAction.mutate({
                        path: `/documents/${documentDetail.document.id}/actions/generate-setting`,
                        body: { requirement },
                        key: 'gen-setting',
                      })
                    }
                  >
                    人物设定
                  </Button>
                  <Button
                    block
                    disabled={!editable || busy}
                    onClick={() =>
                      runAction.mutate({
                        path: `/documents/${documentDetail.document.id}/actions/generate-script`,
                        body: { requirement },
                        key: 'gen-script',
                      })
                    }
                  >
                    生成剧本
                  </Button>
                </>
              )}
              {docKind === 'script' && (
                <>
                  <Button
                    block
                    disabled={!editable || busy}
                    onClick={() =>
                      runAction.mutate({
                        path: `/documents/${documentDetail.document.id}/actions/split-clips`,
                        body: { requirement },
                        key: 'split-clips',
                      })
                    }
                  >
                    分镜
                  </Button>
                  <Button
                    block
                    disabled={!editable || busy}
                    onClick={() =>
                      runAction.mutate({
                        path: `/documents/${documentDetail.document.id}/actions/plan-characters`,
                        body: { requirement },
                        key: 'plan-characters',
                      })
                    }
                  >
                    人物立绘
                  </Button>
                </>
              )}
            </Space>
          </>
        )}

        {/* ---------------- 分镜节点 ---------------- */}
        {node.nodeType === 'shot' && clipDetail && (
          <>
            <div className="drawer-section-label">当前内容</div>
            <div className="reference-card">
              <div className="name">{clipDetail.clip.title}</div>
              <div style={{ marginTop: 6, whiteSpace: 'pre-wrap', color: '#334155' }}>
                {clipDetail.clip.body || '（暂无正文）'}
              </div>
              <div className="hint">
                来源：{(clipDetail.clip.characters ?? []).join('、') || '未标注角色'} ·{' '}
                {clipDetail.clip.locationName || '未标注场景'} ·{' '}
                {(clipDetail.clip.durationMs / 1000).toFixed(1)} 秒
              </div>
            </div>

            <div className="drawer-section-label">
              参考资产 · {clipDetail.references.length}
            </div>
            <div className="reference-grid">
              {clipDetail.references.map((reference) => {
                const ref = reference as import('../api/types').AssetReference
                return (
                  <div key={ref.id} className="reference-card ok">
                    <div className="name">
                      {ref.asset?.canonicalName}
                      {ref.asset?.variantName ? `-${ref.asset.variantName}` : ''}
                    </div>
                    <div className="hint">槽位 {ref.slotNo} · {ref.source}</div>
                  </div>
                )
              })}
              {!clipDetail.references.length && (
                <div className="reference-card">
                  <div className="name">还没有参考资产</div>
                  <div className="hint">视频方案需要每项资产恰有一个可用图片结果</div>
                </div>
              )}
            </div>

            <Divider />
            <div className="drawer-section-label">本次需求</div>
            <Input.TextArea
              rows={2}
              value={requirement}
              onChange={(event) => setRequirement(event.target.value)}
              disabled={!editable}
            />
            <Space style={{ marginTop: 10 }}>
              <span style={{ color: '#64748b', fontSize: 12 }}>数量</span>
              <InputNumber min={1} max={8} value={count} onChange={(value) => setCount(value ?? 1)} />
            </Space>
            <div style={{ marginTop: 12 }}>
              <Button
                type="primary"
                block
                disabled={!editable || busy}
                onClick={() =>
                  runAction.mutate({
                    path: `/clips/${clipDetail.clip.id}/actions/optimize`,
                    body: { requirement, count },
                    key: 'clip-optimize',
                  })
                }
              >
                {busy ? '任务进行中…' : '发送'}
              </Button>
            </div>
            <div className="drawer-section-label">下一步</div>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Button
                block
                disabled={!editable || busy}
                onClick={() =>
                  runAction.mutate({
                    path: `/clips/${clipDetail.clip.id}/actions/plan-scenes`,
                    body: { requirement },
                    key: 'plan-scenes',
                  })
                }
              >
                生成场景
              </Button>
              <Button
                block
                disabled={!editable || busy}
                onClick={() =>
                  runAction.mutate({
                    path: `/clips/${clipDetail.clip.id}/actions/plan-props`,
                    body: { requirement },
                    key: 'plan-props',
                  })
                }
              >
                生成道具
              </Button>
              <Button
                type="primary"
                block
                disabled={!editable || busy}
                onClick={() =>
                  runAction.mutate({
                    path: `/clips/${clipDetail.clip.id}/video-plans`,
                    body: { requirement },
                    key: 'video-plan',
                  })
                }
              >
                生成视频方案
              </Button>
            </Space>
          </>
        )}

        {/* ---------------- 媒体节点 ---------------- */}
        {mediaDetail && (
          <>
            {mediaDetail.media?.mediaType === 'video' && (
              <>
                <div className="drawer-section-label">视频产物</div>
                <video className="media-preview" controls src={mediaDetail.media.fileUrl} />
              </>
            )}
            {mediaDetail.media?.mediaType === 'image' && (
              <>
                <div className="drawer-section-label">图片产物</div>
                <img className="media-preview" src={mediaDetail.media.fileUrl} alt="产物" />
              </>
            )}

            <div className="drawer-section-label">
              方案描述（可见 B） · {mediaDetail.promptVersions.length} 个版本
            </div>
            <Input.TextArea
              rows={5}
              value={promptDraft}
              onChange={(event) => setPromptDraft(event.target.value)}
              disabled={!editable || mediaKind?.endsWith('result')}
              placeholder="用户可见、可编辑的描述"
            />
            <Space style={{ marginTop: 10 }} wrap>
              <Button
                onClick={() => savePrompt.mutate()}
                disabled={!editable || mediaKind?.endsWith('result')}
                loading={savePrompt.isPending}
              >
                保存方案版本
              </Button>
              <Select
                style={{ width: 160 }}
                placeholder="选版"
                options={mediaDetail.promptVersions.map((version) => ({
                  value: version.id,
                  label: `v${version.revisionNo} · ${version.source}`,
                }))}
                onChange={(versionId) =>
                  void apiPost(
                    `/media-nodes/${mediaDetail.mediaNode.id}/prompt-versions/${versionId}/select`,
                  )
                    .then(() => onChanged())
                    .catch((error: Error) => message.error(error.message))
                }
              />
            </Space>
            <div className="hint" style={{ marginTop: 6, color: '#94a3b8', fontSize: 11 }}>
              服务端隐藏预设 A 与完整内部组装文本不会通过任何接口返回。
            </div>

            {promptWarnings.length > 0 && (
              <Alert
                style={{ marginTop: 10 }}
                type="warning"
                showIcon
                message={`提示词规范问题 ${promptWarnings.length} 条（产线手册硬规则）`}
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {promptWarnings.map((warning) => (
                      <li key={warning.code} style={{ fontSize: 12 }}>
                        {warning.message}
                      </li>
                    ))}
                  </ul>
                }
              />
            )}

            {mediaDetail.referenceImages.length > 0 && (
              <>
                <div className="drawer-section-label">
                  冻结的参考图 · {mediaDetail.referenceImages.length}（顺序不可变）
                </div>
                <div className="reference-grid">
                  {mediaDetail.referenceImages.map((item) => (
                    <div key={item.id} className="reference-card">
                      <img
                        src={item.thumbnailUrl ?? item.fileUrl}
                        alt="参考图"
                        style={{ width: '100%', borderRadius: 6 }}
                      />
                    </div>
                  ))}
                </div>
              </>
            )}
            {mediaDetail.frozenClip && (
              <div className="hint" style={{ marginTop: 8, color: '#64748b', fontSize: 12 }}>
                冻结 Clip：{mediaDetail.frozenClip.title}（
                {(mediaDetail.frozenClip.durationMs / 1000).toFixed(1)} 秒）
              </div>
            )}

            {(mediaKind === 'image_plan' || mediaKind === 'video_plan') && (
              <>
                <Divider />
                <div className="drawer-section-label">抽卡</div>
                <Space align="center" wrap>
                  <span style={{ color: '#64748b', fontSize: 12 }}>数量</span>
                  <InputNumber
                    min={1}
                    max={8}
                    value={count}
                    onChange={(value) => setCount(value ?? 1)}
                    disabled={seed >= 0}
                  />
                  <span style={{ color: '#64748b', fontSize: 12 }}>seed</span>
                  <InputNumber
                    min={-1}
                    value={seed}
                    onChange={(value) => setSeed(value ?? -1)}
                    style={{ width: 120 }}
                  />
                </Space>
                <div className="hint" style={{ marginTop: 4, fontSize: 11, color: '#94a3b8' }}>
                  seed 为 -1 时随机，固定 seed 时数量强制为 1。
                </div>
                <Button
                  type="primary"
                  block
                  style={{ marginTop: 12 }}
                  disabled={!editable || busy}
                  onClick={() =>
                    runAction.mutate({
                      path: `/media/${mediaDetail.mediaNode.id}/draw`,
                      body: { count, seed },
                      key: 'draw',
                    })
                  }
                >
                  {busy ? '任务进行中…' : '抽卡出片'}
                </Button>
              </>
            )}

            {mediaKind === 'image_result' && mediaDetail.mediaNode.params.preset === 'portrait' && (
              <>
                <Divider />
                <div className="drawer-section-label">立绘联合派生</div>
                <Button
                  block
                  disabled={!editable || busy}
                  onClick={() =>
                    runAction.mutate({
                      path: `/media-nodes/${mediaDetail.mediaNode.id}/derive`,
                      key: 'derive',
                    })
                  }
                >
                  生成面部特写与三视图
                </Button>
                <div className="hint" style={{ marginTop: 6, fontSize: 11, color: '#94a3b8' }}>
                  每次点击以当前立绘为源图新建两个结果节点、固定各 1 张，不覆盖旧结果。
                </div>
              </>
            )}

            <Divider />
            <Space wrap>
              <Button
                disabled={!editable}
                danger={!mediaDetail.mediaNode.blocked}
                onClick={() => toggleBlocked.mutate(!mediaDetail.mediaNode.blocked)}
              >
                {mediaDetail.mediaNode.blocked ? '解除屏蔽' : '屏蔽此节点'}
              </Button>
              <Tag>{mediaDetail.mediaNode.blocked ? '已屏蔽' : '未屏蔽'}</Tag>
            </Space>
          </>
        )}

        {/* ---------------- 任务反馈 ---------------- */}
        {submissionDetail.data && (
          <>
            <Divider />
            <div className="drawer-section-label">任务反馈</div>
            <div
              className={`task-feedback${
                submissionDetail.data.submission.logicalStatus === 'failed' ? ' failed' : ''
              }`}
            >
              <div>
                <div>
                  {submissionDetail.data.submission.operation} ·{' '}
                  {submissionDetail.data.submission.logicalStatus}
                  {submissionDetail.data.submission.errorMessage
                    ? ` · ${submissionDetail.data.submission.errorCode}`
                    : ''}
                </div>
                <div style={{ fontSize: 11, opacity: 0.8, marginTop: 4 }}>
                  {submissionDetail.data.submission.errorMessage ||
                    `${submissionDetail.data.attempts.length} 次实际调用`}
                </div>
              </div>
            </div>
            <List
              size="small"
              style={{ marginTop: 8 }}
              dataSource={submissionDetail.data.attempts}
              renderItem={(attempt) => (
                <List.Item>
                  <Space direction="vertical" size={0}>
                    <span style={{ fontSize: 12 }}>
                      #{attempt.attemptNo} {attempt.physicalStatus} / {attempt.deliveryStatus}
                      {attempt.isMock ? ' · mock' : ''}
                    </span>
                    {attempt.usage && (
                      <span style={{ fontSize: 11, color: '#94a3b8' }}>
                        用量 in {attempt.usage.inputUnits ?? '未知'} / out{' '}
                        {attempt.usage.outputUnits ?? '未知'} · 费用{' '}
                        {attempt.usage.actualAmount ?? '未知'}（{attempt.usage.billingStatus}）
                      </span>
                    )}
                  </Space>
                </List.Item>
              )}
            />
          </>
        )}

        {submissionDetail.data?.result &&
          Array.isArray((submissionDetail.data.result as { assets?: unknown }).assets) && (
            <>
              <Divider />
              <div className="drawer-section-label">资产规划清单（确认后才创建方案节点）</div>
              <AssetPlanConfirm
                submissionId={submissionDetail.data.submission.id}
                items={
                  (submissionDetail.data.result as { assets: Record<string, unknown>[] }).assets
                }
                onDone={onChanged}
              />
            </>
          )}

        {editable && (
          <>
            <Divider />
            <Space wrap>
              <Button onClick={() => toggleHidden.mutate(!node.hidden)}>
                {node.hidden ? '解除屏蔽节点' : '屏蔽节点'}
              </Button>
              <Popconfirm title="移除该画布节点？内容与媒体会保留" onConfirm={() => deleteNode.mutate()}>
                <Button danger>移除节点</Button>
              </Popconfirm>
            </Space>
          </>
        )}

        <Typography.Paragraph style={{ marginTop: 16, fontSize: 11, color: '#94a3b8' }}>
          节点 ID {node.id}
        </Typography.Paragraph>
      </div>
    </>
  )
}

function AssetPlanConfirm({
  submissionId,
  items,
  onDone,
}: {
  submissionId: string
  items: Record<string, unknown>[]
  onDone: () => void
}) {
  const [submitting, setSubmitting] = useState(false)

  const confirm = async () => {
    setSubmitting(true)
    try {
      await apiPost('/asset-plans/confirm', { submissionId, items })
      message.success('清单已确认：复用已有资产，未命中的创建图片方案节点')
      onDone()
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <Segmented
        block
        options={['查重', '规范命名', '回填引用']}
        value="查重"
        style={{ marginBottom: 8 }}
      />
      <List
        size="small"
        dataSource={items}
        rowKey={(item) => String(item.canonicalName)}
        renderItem={(item) => (
          <List.Item>
            <Space>
              <Tag color="orange">{String(item.assetType)}</Tag>
              <span>{String(item.canonicalName)}</span>
              {item.imageKind ? <Tag>{String(item.imageKind)}</Tag> : null}
            </Space>
          </List.Item>
        )}
      />
      <Button type="primary" block loading={submitting} onClick={() => void confirm()}>
        确认清单（{items.length} 项，不直接生图）
      </Button>
    </>
  )
}
