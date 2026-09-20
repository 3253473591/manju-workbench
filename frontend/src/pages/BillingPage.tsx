import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Card, Empty, Statistic, Table, Tag } from 'antd'

import { apiGet } from '../api/client'
import type { Attempt, BillingSummary, Submission } from '../api/types'
import { canBill, useAuth } from '../auth/AuthContext'

interface RecordRow {
  attemptId: string
  submissionId: string
  operation: string
  inputUnits: number | null
  outputUnits: number | null
  amount: string | null
  currency: string
  billingStatus: string
  isMock: boolean
  createdAt: string
}

export default function BillingPage() {
  const { projectId } = useParams()
  const { user } = useAuth()

  const summary = useQuery({
    queryKey: ['billing-summary', projectId],
    queryFn: () => apiGet<BillingSummary>(`/projects/${projectId}/billing/summary`),
    enabled: Boolean(projectId) && canBill(user?.role),
  })

  const records = useQuery({
    queryKey: ['billing-records', projectId],
    queryFn: () =>
      apiGet<{ items: RecordRow[]; total: number }>(
        `/projects/${projectId}/billing/records?pageSize=100`,
      ),
    enabled: Boolean(projectId) && canBill(user?.role),
  })

  if (!canBill(user?.role)) {
    return (
      <div className="page-pane">
        <Card>
          <Empty description="user 角色不返回任何计费字段（服务端裁剪，不是前端隐藏）" />
        </Card>
      </div>
    )
  }

  return (
    <div className="page-pane">
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
          gap: 16,
          marginBottom: 16,
        }}
      >
        <Card>
          <Statistic title="实际支出合计" value={summary.data?.actualTotal ?? '0'} suffix="CNY" precision={4} />
        </Card>
        <Card>
          <Statistic title="提交前预估合计" value={summary.data?.estimatedTotal ?? '0'} suffix="CNY" precision={4} />
        </Card>
        <Card>
          <Statistic title="mock 模拟（不计入支出）" value={summary.data?.mockTotal ?? '0'} suffix="CNY" precision={4} />
        </Card>
        <Card>
          <Statistic title="计费 attempt 数" value={summary.data?.attemptCount ?? 0} />
        </Card>
        <Card>
          <Statistic
            title="用量/价格待核对"
            value={summary.data?.unknownUsageCount ?? 0}
            valueStyle={{ color: (summary.data?.unknownUsageCount ?? 0) > 0 ? '#c2410c' : undefined }}
          />
        </Card>
      </div>

      <Card className="section-card" title="按操作聚合">
        <Table
          rowKey="operation"
          size="small"
          pagination={false}
          dataSource={summary.data?.byOperation ?? []}
          columns={[
            { title: '操作', dataIndex: 'operation' },
            { title: 'attempt 数', dataIndex: 'attemptCount', width: 140 },
            { title: '金额', dataIndex: 'amount', width: 160 },
          ]}
        />
      </Card>

      <Card title="费用明细">
        <Table
          rowKey="attemptId"
          dataSource={records.data?.items ?? []}
          pagination={{ pageSize: 20 }}
          locale={{ emptyText: <Empty description="还没有计费记录" /> }}
          columns={[
            { title: '操作', dataIndex: 'operation', width: 180 },
            { title: '输入', dataIndex: 'inputUnits', width: 90 },
            { title: '输出', dataIndex: 'outputUnits', width: 90 },
            {
              title: '金额',
              width: 140,
              render: (_value, record: RecordRow) =>
                record.amount === null ? <Tag color="orange">待核对</Tag> : `${record.amount} ${record.currency}`,
            },
            {
              title: '计费状态',
              dataIndex: 'billingStatus',
              width: 120,
              render: (value: string, record: RecordRow) => (
                <Tag color={record.isMock ? 'blue' : value === 'unknown' ? 'orange' : 'green'}>{value}</Tag>
              ),
            },
            {
              title: '时间',
              dataIndex: 'createdAt',
              render: (value: string) => value.replace('T', ' ').replace('Z', ''),
            },
          ]}
        />
      </Card>
    </div>
  )
}

export type { Attempt, Submission }
