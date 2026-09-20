import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Card, Empty, Table, Tag } from 'antd'

import { apiGet } from '../api/client'
import type { ProjectDetail, Submission } from '../api/types'

const STATUS_COLOR: Record<string, string> = {
  succeeded: 'green',
  failed: 'red',
  running: 'blue',
  pending: 'gold',
  cancelled: 'default',
}

export default function TasksPage() {
  const { projectId } = useParams()

  const project = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => apiGet<ProjectDetail>(`/projects/${projectId}`),
    enabled: Boolean(projectId),
  })
  const episodeId = project.data?.episodes[0]?.id

  const submissions = useQuery({
    queryKey: ['episode-submissions', episodeId],
    queryFn: () =>
      apiGet<{ items: Submission[]; total: number }>(
        `/episodes/${episodeId}/submissions?pageSize=100`,
      ),
    enabled: Boolean(episodeId),
    refetchInterval: 3000,
  })

  return (
    <div className="page-pane">
      <Card className="section-card">
        <div style={{ fontSize: 16, fontWeight: 700 }}>任务记录</div>
        <div style={{ color: '#8491a0', fontSize: 12, marginTop: 4 }}>
          每次实际模型调用独立记一条 attempt；轮询同一供应商任务不新增 attempt。
          logical（用户目标）/ physical（执行进程）/ delivery（交付核验）三层状态互不覆盖。
        </div>
      </Card>

      <Card>
        <Table
          rowKey="id"
          dataSource={submissions.data?.items ?? []}
          pagination={{ pageSize: 20 }}
          locale={{ emptyText: <Empty description="还没有任务" /> }}
          columns={[
            {
              title: '操作',
              dataIndex: 'operation',
              width: 200,
            },
            {
              title: '状态',
              dataIndex: 'logicalStatus',
              width: 110,
              render: (value: string) => <Tag color={STATUS_COLOR[value] ?? 'default'}>{value}</Tag>,
            },
            { title: '数量', dataIndex: 'requestedCount', width: 80 },
            {
              title: '错误',
              render: (_value, record: Submission) =>
                record.errorCode ? (
                  <span style={{ color: '#b91c1c', fontSize: 12 }}>
                    {record.errorCode} · {record.errorMessage}
                  </span>
                ) : (
                  <span style={{ color: '#94a3b8' }}>—</span>
                ),
            },
            {
              title: '创建时间',
              dataIndex: 'createdAt',
              width: 200,
              render: (value: string) => value.replace('T', ' ').replace('Z', ''),
            },
          ]}
        />
      </Card>
    </div>
  )
}
