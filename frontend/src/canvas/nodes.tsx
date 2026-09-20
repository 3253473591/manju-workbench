/**
 * 画布节点组件。
 *
 * ⚠️ `nodeTypes` 必须在**模块作用域**定义并保持引用稳定：放进组件里会让 React Flow
 * 每次渲染都认为 props 变了，触发 store 更新 → 再渲染的死循环（交接文档 §7.1）。
 */
import { Handle, NodeResizer, Position, type NodeProps } from '@xyflow/react'
import { PlayCircleFilled } from '@ant-design/icons'

import type { CanvasNode, ClipDetail, ContentDocumentDetail, MediaNodeDetail } from '../api/types'

export interface CanvasNodeData extends Record<string, unknown> {
  node: CanvasNode
  document?: ContentDocumentDetail
  clip?: ClipDetail
  mediaNode?: MediaNodeDetail
  running?: boolean
  [key: string]: unknown
}

const DOC_LABEL: Record<string, string> = {
  outline: '大纲',
  setting: '人物设定',
  script: '剧本',
  clip: '分镜',
}

const MEDIA_LABEL: Record<string, string> = {
  image_plan: '图片方案',
  image_result: '图片结果',
  video_plan: '视频方案',
  video_result: '视频结果',
}

function Shell({
  selected,
  hidden,
  running,
  accent,
  children,
}: {
  selected?: boolean
  hidden?: boolean
  running?: boolean
  accent: string
  children: React.ReactNode
}) {
  const classes = ['node-card']
  if (selected) classes.push('selected')
  if (hidden) classes.push('hidden-node')
  if (running) classes.push('running')
  return (
    <div className={classes.join(' ')}>
      <div className="node-accent" style={{ background: accent }} />
      <Handle type="target" position={Position.Left} />
      {children}
      <Handle type="source" position={Position.Right} />
    </div>
  )
}

function Resizer({
  visible,
  nodeId,
  onResize,
}: {
  visible: boolean
  nodeId: string
  onResize?: (nodeId: string, width: number, height: number, x: number, y: number) => void
}) {
  return (
    <NodeResizer
      isVisible={visible}
      minWidth={140}
      minHeight={100}
      lineStyle={{ borderColor: '#f59e0b' }}
      handleStyle={{ borderColor: '#f59e0b' }}
      onResizeEnd={(_event, params) => {
        onResize?.(nodeId, params.width, params.height, params.x, params.y)
      }}
    />
  )
}

function resizeProps(data: CanvasNodeData, nodeId: string) {
  return {
    nodeId,
    onResize: data.onResize as
      | ((nodeId: string, width: number, height: number, x: number, y: number) => void)
      | undefined,
  }
}

export function TextNode({ id, data, selected }: NodeProps) {
  const { node, document: doc, running } = data as CanvasNodeData
  const kind = doc?.document.kind ?? 'outline'
  const body = doc?.document.body ?? ''
  return (
    <>
      <Resizer visible={Boolean(selected)} {...resizeProps(data as CanvasNodeData, id)} />
      <Shell selected={selected} hidden={node.hidden} running={running} accent="#f59e0b">
        <div className="node-kind">
          TEXT · {DOC_LABEL[kind] ?? kind} · v{doc?.document.version ?? 0}
        </div>
        <div className="node-title">{node.label || doc?.document.title || '未命名文本'}</div>
        <div className="node-meta">{body.slice(0, 60) || '（空）'}</div>
        {doc?.hasUnsavedDraft && <div className="node-badge warn">草稿未保存</div>}
        {!doc?.hasUnsavedDraft && doc?.currentRevision && (
          <div className="node-badge">已保存 v{doc.currentRevision.revisionNo}</div>
        )}
      </Shell>
    </>
  )
}

export function ShotNode({ id, data, selected }: NodeProps) {
  const { node, clip, running } = data as CanvasNodeData
  const detail = clip?.clip
  return (
    <>
      <Resizer visible={Boolean(selected)} {...resizeProps(data as CanvasNodeData, id)} />
      <Shell selected={selected} hidden={node.hidden} running={running} accent="#f97316">
        <div className="node-kind">SHOT · CLIP {String(detail?.clipNo ?? 0).padStart(2, '0')}</div>
        <div className="node-title">{detail?.title ?? node.label}</div>
        <div className="node-meta">
          {((detail?.durationMs ?? 0) / 1000).toFixed(1)} 秒 ·{' '}
          {detail?.characters?.length ?? 0} 个角色 · {detail?.locationName || '未指定场景'}
        </div>
        <div className="node-badge">{detail?.status ?? 'draft'}</div>
      </Shell>
    </>
  )
}

export function ImageNode({ id, data, selected }: NodeProps) {
  const { node, mediaNode, running } = data as CanvasNodeData
  const info = mediaNode?.mediaNode
  const item = mediaNode?.media
  const label = info ? MEDIA_LABEL[info.mediaKind] : '图片'
  return (
    <>
      <Resizer visible={Boolean(selected)} {...resizeProps(data as CanvasNodeData, id)} />
      <Shell selected={selected} hidden={node.hidden} running={running} accent="#38bdf8">
        {item?.thumbnailUrl ? (
          <img className="node-media" src={item.thumbnailUrl} alt={node.label} style={{ height: '70%' }} />
        ) : (
          <div className="node-meta" style={{ marginTop: 8 }}>
            {mediaNode?.visiblePrompt?.slice(0, 80) || '方案：尚无产物'}
          </div>
        )}
        <div className="node-kind" style={{ marginTop: 6 }}>
          {label} · {item ? `${item.width ?? '?'}×${item.height ?? '?'}` : '未出图'}
        </div>
        <div className="node-title" style={{ fontSize: 13 }}>
          {node.label}
        </div>
        {info?.blocked && <div className="node-badge warn">已屏蔽</div>}
      </Shell>
    </>
  )
}

export function VideoNode({ id, data, selected }: NodeProps) {
  const { node, mediaNode, running } = data as CanvasNodeData
  const info = mediaNode?.mediaNode
  const item = mediaNode?.media
  const label = info ? MEDIA_LABEL[info.mediaKind] : '视频'
  return (
    <>
      <Resizer visible={Boolean(selected)} {...resizeProps(data as CanvasNodeData, id)} />
      <Shell selected={selected} hidden={node.hidden} running={running} accent="#a78bfa">
        {item?.thumbnailUrl ? (
          <div style={{ position: 'relative', height: '70%' }}>
            <img className="node-media" src={item.thumbnailUrl} alt={node.label} style={{ height: '100%' }} />
            <PlayCircleFilled
              style={{
                position: 'absolute',
                inset: 0,
                margin: 'auto',
                fontSize: 34,
                color: 'rgba(255,255,255,.86)',
                width: '100%',
                height: '100%',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                pointerEvents: 'none',
              }}
            />
          </div>
        ) : (
          <div className="node-meta" style={{ marginTop: 8 }}>
            {mediaNode?.visiblePrompt?.slice(0, 80) || '方案：尚无产物'}
          </div>
        )}
        <div className="node-kind" style={{ marginTop: 6 }}>
          {label} · 冻结 {mediaNode?.referenceImages.length ?? 0} 张参考图
        </div>
        <div className="node-title" style={{ fontSize: 13 }}>
          {node.label}
        </div>
        {info?.blocked && <div className="node-badge warn">已屏蔽</div>}
      </Shell>
    </>
  )
}

export const nodeTypes = {
  text: TextNode,
  shot: ShotNode,
  image: ImageNode,
  video: VideoNode,
}

export const CIRCLED_TYPES = Object.keys(nodeTypes)
