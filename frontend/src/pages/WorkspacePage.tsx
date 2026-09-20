import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Background,
  BackgroundVariant,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeChange,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Button, Dropdown, Form, Input, Modal, Select, Upload, message } from 'antd'
import { PlusOutlined, UploadOutlined } from '@ant-design/icons'

import { apiGet, apiPatch, apiPost } from '../api/client'
import type { CanvasNodeDetail, CanvasSnapshot, ProjectDetail } from '../api/types'
import { canEdit, useAuth } from '../auth/AuthContext'
import NodeDrawer from '../components/NodeDrawer'
import { nodeTypes, type CanvasNodeData } from '../canvas/nodes'

const NODE_DEFAULT_SIZE: Record<string, { width: number; height: number }> = {
  text: { width: 420, height: 320 },
  shot: { width: 320, height: 240 },
  image: { width: 240, height: 300 },
  video: { width: 280, height: 220 },
}

function WorkspaceInner() {
  const { projectId } = useParams()
  const { user } = useAuth()
  const editable = canEdit(user?.role)
  const queryClient = useQueryClient()
  const reactFlow = useReactFlow()
  const wrapperRef = useRef<HTMLDivElement>(null)
  const paneRef = useRef<HTMLDivElement>(null)

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [drawerWidth, setDrawerWidth] = useState(30)
  const [importOpen, setImportOpen] = useState(false)
  const [importForm] = Form.useForm()

  const project = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => apiGet<ProjectDetail>(`/projects/${projectId}`),
    enabled: Boolean(projectId),
  })
  const episodeId = project.data?.episodes[0]?.id

  const canvas = useQuery({
    queryKey: ['canvas', episodeId],
    queryFn: () => apiGet<CanvasSnapshot>(`/episodes/${episodeId}/canvas`),
    enabled: Boolean(episodeId),
    refetchInterval: 4000,
  })

  const nodeIds = useMemo(
    () => (canvas.data?.nodes ?? []).map((item) => item.node.id),
    [canvas.data],
  )

  // 画布快照只给节点+布局；每个节点的内容按需批量取详情。
  const details = useQueries({
    queries: nodeIds.map((nodeId) => ({
      queryKey: ['node', nodeId],
      queryFn: () => apiGet<CanvasNodeDetail>(`/canvas/nodes/${nodeId}`),
      enabled: Boolean(nodeId),
    })),
  })

  const detailMap = useMemo(() => {
    const map = new Map<string, CanvasNodeDetail>()
    details.forEach((query, index) => {
      if (query.data) map.set(nodeIds[index], query.data)
    })
    return map
  }, [details, nodeIds])

  const submissions = useQuery({
    queryKey: ['submissions', episodeId],
    queryFn: () => apiGet<{ items: { id: string; nodeId: string | null; logicalStatus: string }[] }>(
      `/episodes/${episodeId}/submissions?pageSize=50`,
    ),
    enabled: Boolean(episodeId),
    refetchInterval: 2000,
  })

  const runningNodeIds = useMemo(() => {
    const set = new Set<string>()
    submissions.data?.items.forEach((item) => {
      if (item.nodeId && (item.logicalStatus === 'pending' || item.logicalStatus === 'running')) {
        set.add(item.nodeId)
      }
    })
    return set
  }, [submissions.data])

  const refreshCanvas = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['canvas', episodeId] })
    void queryClient.invalidateQueries({ queryKey: ['node'] })
  }, [queryClient, episodeId])

  const onResize = useCallback(
    (nodeId: string, width: number, height: number, x: number, y: number) => {
      if (!editable) return
      void apiPatch(`/canvas/nodes/${nodeId}/layout`, { x, y, width, height, zIndex: 0 }).catch(
        (error: Error) => message.error(error.message),
      )
    },
    [editable],
  )

  const flowNodes = useMemo<Node<CanvasNodeData>[]>(() => {
    return (canvas.data?.nodes ?? []).map((item) => {
      const detail = detailMap.get(item.node.id)
      return {
        id: item.node.id,
        type: item.node.nodeType,
        position: { x: item.layout.x, y: item.layout.y },
        style: { width: item.layout.width, height: item.layout.height },
        selected: item.node.id === selectedNodeId,
        data: {
          node: item.node,
          document: detail?.document as never,
          clip: detail?.clip as never,
          mediaNode: detail?.mediaNode as never,
          running: runningNodeIds.has(item.node.id),
          onResize,
        },
      }
    })
  }, [canvas.data, detailMap, selectedNodeId, runningNodeIds, onResize])

  const flowEdges = useMemo<Edge[]>(
    () =>
      (canvas.data?.edges ?? []).map((edge) => ({
        id: edge.id,
        source: edge.fromNodeId,
        target: edge.toNodeId,
        animated: edge.edgeType === 'derives',
        label: edge.note || undefined,
        style: { stroke: '#c2ccd7', strokeDasharray: '5 5' },
      })),
    [canvas.data],
  )

  const onNodesChange = useCallback((_changes: NodeChange<Node<CanvasNodeData>>[]) => {
    // 位置与尺寸由拖拽结束/缩放结束事件显式落库，这里不写库，避免每帧一次请求。
  }, [])

  const onNodeDragStop = useCallback(
    (_event: unknown, node: Node<CanvasNodeData>) => {
      const size = NODE_DEFAULT_SIZE[node.data.node.nodeType] ?? { width: 320, height: 220 }
      onResize(
        node.id,
        Number(node.style?.width ?? size.width),
        Number(node.style?.height ?? size.height),
        node.position.x,
        node.position.y,
      )
    },
    [onResize],
  )

  const onConnect = useCallback(
    async (connection: Connection) => {
      if (!editable || !episodeId || !connection.source || !connection.target) return
      try {
        await apiPost(`/episodes/${episodeId}/edges`, {
          fromNodeId: connection.source,
          toNodeId: connection.target,
          edgeType: 'derives',
        })
        refreshCanvas()
      } catch (error) {
        message.error((error as Error).message)
      }
    },
    [editable, episodeId, refreshCanvas],
  )

  const createClip = useCallback(
    async (title: string) => {
      if (!episodeId) return
      try {
        await apiPost(`/episodes/${episodeId}/clips`, { title })
        message.success('已新建分镜节点')
        refreshCanvas()
      } catch (error) {
        message.error((error as Error).message)
      }
    },
    [episodeId, refreshCanvas],
  )

  const submitImport = useCallback(
    async (values: {
      target: string
      title?: string
      imageKind?: string
      canonicalName?: string
      file: { file: File }
    }) => {
      if (!episodeId) return
      const formData = new FormData()
      formData.append('file', values.file.file)
      if (values.target === 'image') {
        if (values.imageKind) formData.append('imageKind', values.imageKind)
        if (values.canonicalName) formData.append('canonicalName', values.canonicalName)
      }
      try {
        const path =
          values.target === 'image'
            ? `/episodes/${episodeId}/imports/image`
            : `/episodes/${episodeId}/imports/text`
        if (values.target !== 'image') {
          formData.append('target', values.target)
          if (values.title) formData.append('title', values.title)
        }
        const response = await fetch(`/api/v1${path}`, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${localStorage.getItem('manju.token') ?? ''}`,
          },
          body: formData,
        })
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}))
          throw new Error(payload?.error?.message ?? '导入失败')
        }
        message.success('导入完成，已创建节点')
        setImportOpen(false)
        importForm.resetFields()
        refreshCanvas()
      } catch (error) {
        message.error((error as Error).message)
      }
    },
    [episodeId, importForm, refreshCanvas],
  )

  // 抽屉宽度拖拽（30%–70%）
  const startResize = useCallback(    (event: React.MouseEvent) => {
      event.preventDefault()
      const container = wrapperRef.current
      if (!container) return
      const total = container.clientWidth
      const onMove = (moveEvent: MouseEvent) => {
        const right = container.getBoundingClientRect().right
        const ratio = ((right - moveEvent.clientX) / total) * 100
        setDrawerWidth(Math.min(70, Math.max(30, ratio)))
      }
      const onUp = () => {
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
      }
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
    },
    [],
  )

  const addMenu = {
    items: [
      { key: 'clip', label: '新建分镜（Clip）' },
      { key: 'text', label: '导入文本（txt / md / docx）' },
      { key: 'image', label: '导入图片（png / jpg / webp / bmp）' },
    ],
    onClick: ({ key }: { key: string }) => {
      if (!editable) {
        message.info('manager 角色只读，不能创建节点')
        return
      }
      if (key === 'clip') {
        void createClip('新建分镜')
        return
      }
      importForm.setFieldsValue({ target: key === 'image' ? 'image' : 'script' })
      setImportOpen(true)
    },
  }

  const nodeCount = canvas.data?.nodes.length ?? 0

  // 视图策略：**不追求「一屏装下整条链路」**。
  // 一集的流水线天然是几千像素宽，硬 fit 会把节点缩到 0.2 倍、文字完全不可读；
  // 这里改为可读缩放 + 定位到链路起点，其余靠平移浏览——这也是节点画布的常规用法。
  const MIN_READABLE_ZOOM = 0.55

  const resetView = useCallback(() => {
    const items = canvas.data?.nodes ?? []
    const pane = paneRef.current
    if (!items.length || !pane) return
    const minX = Math.min(...items.map((item) => item.layout.x))
    const minY = Math.min(...items.map((item) => item.layout.y))
    const maxX = Math.max(...items.map((item) => item.layout.x + item.layout.width))
    const maxY = Math.max(...items.map((item) => item.layout.y + item.layout.height))
    const width = Math.max(maxX - minX, 1)
    const height = Math.max(maxY - minY, 1)
    const fitZoom = Math.min((pane.clientWidth - 80) / width, (pane.clientHeight - 80) / height)
    const zoom = Math.max(Math.min(fitZoom, 1), MIN_READABLE_ZOOM)

    if (fitZoom >= MIN_READABLE_ZOOM) {
      // 装得下就居中整张图
      reactFlow.setViewport({
        x: (pane.clientWidth - width * zoom) / 2 - minX * zoom,
        y: (pane.clientHeight - height * zoom) / 2 - minY * zoom,
        zoom,
      })
      return
    }
    // 装不下就定位到起点（最靠左上的节点），保证第一屏是可读的
    const origin = items.reduce((best, item) =>
      item.layout.x + item.layout.y < best.layout.x + best.layout.y ? item : best,
    )
    reactFlow.setCenter(
      origin.layout.x + origin.layout.width / 2,
      origin.layout.y + origin.layout.height / 2,
      { zoom, duration: 0 },
    )
  }, [canvas.data, reactFlow])

  useEffect(() => {
    if (!nodeCount) return
    const timer = window.setTimeout(resetView, 150)
    return () => window.clearTimeout(timer)
  }, [nodeCount, resetView])

  // 首次进入时默认选中大纲节点：右侧抽屉不该是一块空白（视觉稿里抽屉始终有内容）
  const autoSelected = useRef(false)
  useEffect(() => {
    if (autoSelected.current || !canvas.data?.nodes.length) return
    const first = canvas.data.nodes.find((item) => item.node.nodeType === 'text')
    if (first) {
      setSelectedNodeId(first.node.id)
      autoSelected.current = true
    }
  }, [canvas.data])

  if (project.isLoading || canvas.isLoading) {
    return <div className="page-pane">正在加载画布…</div>
  }

  return (
    <div className="workspace" ref={wrapperRef}>
      <div className="canvas-pane" ref={paneRef}>
        <div className="canvas-legend">画布 · 双击空白处或右上角添加节点</div>
        <div className="canvas-toolbar">
          <Button onClick={() => reactFlow.fitView({ padding: 0.12, duration: 200 })}>
            适应画布
          </Button>
          <Button onClick={resetView}>回到起点</Button>
          <Dropdown menu={addMenu} trigger={['click']}>
            <Button icon={<PlusOutlined />}>添加节点</Button>
          </Dropdown>
          <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>
            导入文件
          </Button>
        </div>
        <ReactFlow
          nodes={flowNodes}
          edges={flowEdges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onNodeDragStop={onNodeDragStop}
          onConnect={onConnect}
          onNodeClick={(_event, node) => setSelectedNodeId(node.id)}
          onPaneClick={() => setSelectedNodeId(null)}
          onDoubleClick={() => editable && setImportOpen(true)}
          minZoom={0.2}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#dbe4ee" />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>

      <div className="drawer-resizer" onMouseDown={startResize} />

      <div className="drawer-pane" style={{ flexBasis: `${drawerWidth}%`, width: `${drawerWidth}%` }}>
        <NodeDrawer
          episodeId={episodeId ?? ''}
          detail={selectedNodeId ? detailMap.get(selectedNodeId) : undefined}
          onChanged={() => {
            refreshCanvas()
            resetView()
          }}
        />
      </div>

      <Modal
        title="导入文件"
        open={importOpen}
        onCancel={() => setImportOpen(false)}
        onOk={() => importForm.submit()}
        okText="开始导入"
      >
        <Form form={importForm} layout="vertical" onFinish={(values) => void submitImport(values)}>
          <Form.Item name="target" label="导入为" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'script', label: '文本 · 剧本' },
                { value: 'outline', label: '文本 · 大纲' },
                { value: 'clip', label: '文本 · 分镜（新建 Clip）' },
                { value: 'image', label: '图片' },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="file"
            label="文件"
            valuePropName="fileList"
            getValueFromEvent={(event) =>
              event?.fileList?.[0] ? { file: event.fileList[0].originFileObj } : undefined
            }
            rules={[{ required: true, message: '请选择文件' }]}
          >
            <Upload beforeUpload={() => false} maxCount={1}>
              <Button icon={<UploadOutlined />}>选择文件</Button>
            </Upload>
          </Form.Item>
          <Form.Item
            noStyle
            shouldUpdate={(prev, next) => prev.target !== next.target}
          >
            {({ getFieldValue }) =>
              getFieldValue('target') === 'image' ? (
                <>
                  <Form.Item name="imageKind" label="图种">
                    <Select
                      allowClear
                      placeholder="留空则按文件名解析"
                      options={[
                        { value: 'portrait', label: '立绘' },
                        { value: 'face_closeup', label: '面部特写' },
                        { value: 'three_view', label: '三视图' },
                        { value: 'scene', label: '场景' },
                        { value: 'prop', label: '道具' },
                      ]}
                    />
                  </Form.Item>
                  <Form.Item name="canonicalName" label="资产名（可选）">
                    <Input placeholder="留空则按「00人物-张三-冬装-三视图」解析" />
                  </Form.Item>
                </>
              ) : (
                <Form.Item name="title" label="标题（可选）">
                  <Input placeholder="留空则用文件名" />
                </Form.Item>
              )
            }
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

export default function WorkspacePage() {
  return (
    <ReactFlowProvider>
      <WorkspaceInner />
    </ReactFlowProvider>
  )
}
