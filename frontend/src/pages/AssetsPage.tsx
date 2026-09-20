import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Card, Empty, Segmented, Space, Table, Tag, message } from 'antd'

import { apiGet, apiPost } from '../api/client'
import type { Asset, ProjectDetail } from '../api/types'
import { canEdit, useAuth } from '../auth/AuthContext'

const TYPE_LABEL: Record<string, string> = {
  character: '角色',
  scene: '场景',
  prop: '道具',
}

export default function AssetsPage() {
  const { projectId } = useParams()
  const { user } = useAuth()
  const editable = canEdit(user?.role)
  const queryClient = useQueryClient()
  const [assetType, setAssetType] = useState<string>('all')

  const project = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => apiGet<ProjectDetail>(`/projects/${projectId}`),
    enabled: Boolean(projectId),
  })
  const episodeId = project.data?.episodes[0]?.id

  const assets = useQuery({
    queryKey: ['assets', projectId, assetType],
    queryFn: () =>
      apiGet<{ items: Asset[]; total: number }>(
        `/projects/${projectId}/assets?pageSize=100${assetType === 'all' ? '' : `&assetType=${assetType}`}`,
      ),
    enabled: Boolean(projectId),
  })

  const assetIds = (assets.data?.items ?? []).map((asset) => asset.id)
  const detailQueries = useQuery({
    queryKey: ['asset-details', assetIds.join(',')],
    queryFn: async () => {
      const entries = await Promise.all(
        assetIds.map(async (assetId) => [assetId, await apiGet<{ usableResultCount: number }>(`/assets/${assetId}`)] as const),
      )
      return Object.fromEntries(entries)
    },
    enabled: assetIds.length > 0,
  })

  const createPlan = async (assetId: string) => {
    try {
      await apiPost(`/assets/${assetId}/image-plans`, {})
      message.success('已创建图片方案节点（不直接生图）')
      void queryClient.invalidateQueries({ queryKey: ['assets'] })
      void queryClient.invalidateQueries({ queryKey: ['canvas', episodeId] })
    } catch (error) {
      message.error((error as Error).message)
    }
  }

  return (
    <div className="page-pane">
      <Card className="section-card">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Segmented
            value={assetType}
            onChange={(value) => setAssetType(String(value))}
            options={[
              { label: '全部', value: 'all' },
              { label: '角色', value: 'character' },
              { label: '场景', value: 'scene' },
              { label: '道具', value: 'prop' },
            ]}
          />
          <div style={{ color: '#8491a0', fontSize: 12 }}>
            资产身份在项目内共享；同名候选各有独立节点与物理文件。
            <b> 视频方案要求每项资产恰好有一个未屏蔽且可读取的图片结果。</b>
          </div>
        </Space>
      </Card>

      <Card>
        <Table
          rowKey="id"
          dataSource={assets.data?.items ?? []}
          locale={{ emptyText: <Empty description="还没有资产，先在剧本节点点「人物立绘」规划" /> }}
          pagination={false}
          columns={[
            {
              title: '类型',
              dataIndex: 'assetType',
              width: 90,
              render: (value: string) => <Tag color="orange">{TYPE_LABEL[value] ?? value}</Tag>,
            },
            {
              title: '规范名',
              render: (_value, record: Asset) =>
                `${record.canonicalName}${record.variantName ? `-${record.variantName}` : ''}`,
            },
            { title: '图种', dataIndex: 'imageKind', width: 120 },
            { title: '状态', dataIndex: 'status', width: 100 },
            {
              title: '可用结果',
              width: 110,
              render: (_value, record: Asset) => {
                const count = detailQueries.data?.[record.id]?.usableResultCount
                if (count === undefined) return '—'
                return count === 1 ? (
                  <Tag color="green">1（可出视频）</Tag>
                ) : (
                  <Tag color="red">{count}（需屏蔽到 1）</Tag>
                )
              },
            },
            {
              title: '操作',
              width: 180,
              render: (_value, record: Asset) => (
                <Space>
                  <Button size="small" disabled={!editable} onClick={() => void createPlan(record.id)}>
                    图片方案
                  </Button>
                  <Button
                    size="small"
                    type="link"
                    onClick={() => navigator.clipboard.writeText(record.id)}
                  >
                    复制 ID
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>
    </div>
  )
}
