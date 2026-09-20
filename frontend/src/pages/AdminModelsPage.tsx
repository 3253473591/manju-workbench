import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Card, Empty, Switch, Table, Tag, Tooltip, message } from 'antd'

import { apiGet, apiPatch } from '../api/client'
import type { ModelConfig } from '../api/types'
import { useAuth } from '../auth/AuthContext'

const CAPABILITY_LABEL: Record<string, string> = {
  text: '文本与提示词',
  image: '图片',
  video: '视频',
}

export default function AdminModelsPage() {
  const { user } = useAuth()
  const queryClient = useQueryClient()

  const configs = useQuery({
    queryKey: ['model-configs'],
    queryFn: () => apiGet<{ items: ModelConfig[]; total: number }>('/admin/model-configs'),
    enabled: user?.role === 'admin',
  })

  const toggle = async (config: ModelConfig, enabled: boolean) => {
    try {
      await apiPatch(`/admin/model-configs/${config.id}`, {
        capability: config.capability,
        productLabel: config.productLabel,
        endpointId: config.endpointId,
        actualModel: config.actualModel,
        enabled,
        isDefault: config.isDefault,
        spec: config.spec,
        priceAmount: config.priceAmount,
        priceCurrency: config.priceCurrency,
        priceUnit: config.priceUnit,
        concurrencyLimit: config.concurrencyLimit,
        note: config.note,
      })
      message.success(enabled ? '已启用' : '已停用')
      void queryClient.invalidateQueries({ queryKey: ['model-configs'] })
      void queryClient.invalidateQueries({ queryKey: ['meta'] })
    } catch (error) {
      message.error((error as Error).message)
    }
  }

  if (user?.role !== 'admin') {
    return (
      <div className="page-pane">
        <Card>
          <Empty description="仅 admin 可以管理模型配置" />
        </Card>
      </div>
    )
  }

  return (
    <div className="page-pane">
      <Card className="section-card">
        <div style={{ fontSize: 16, fontWeight: 700 }}>模型配置</div>
        <div style={{ color: '#8491a0', fontSize: 12, marginTop: 4 }}>
          只接火山方舟官方接口。密钥只按 <code>credential_ref</code> 指向的环境变量名读取，
          接口不返回密钥值。<b>未通过真实调用核验的档位不会被标记为可用。</b>
        </div>
      </Card>

      <Card>
        <Table
          rowKey="id"
          dataSource={configs.data?.items ?? []}
          pagination={false}
          columns={[
            {
              title: '能力',
              dataIndex: 'capability',
              width: 130,
              render: (value: string) => CAPABILITY_LABEL[value] ?? value,
            },
            {
              title: '档位',
              dataIndex: 'productLabel',
              width: 170,
              render: (value: string, record: ModelConfig) => (
                <span>
                  {value} {record.isDefault && <Tag color="orange">默认</Tag>}
                </span>
              ),
            },
            { title: '接入点', dataIndex: 'endpointId', width: 230 },
            {
              title: '实际模型',
              dataIndex: 'actualModel',
              width: 200,
              render: (value: string) => value || '—',
            },
            {
              title: '核验',
              width: 140,
              render: (_value, record: ModelConfig) =>
                record.verifiedAt ? (
                  <Tag color="green">已核验</Tag>
                ) : (
                  <Tooltip title="没有真实调用成功记录，real 模式不会当作可用">
                    <Tag color="orange">未核验</Tag>
                  </Tooltip>
                ),
            },
            {
              title: '计价',
              width: 170,
              render: (_value, record: ModelConfig) =>
                `${record.priceAmount} ${record.priceCurrency} / ${record.priceUnit || '未定义'}`,
            },
            { title: '并发', dataIndex: 'concurrencyLimit', width: 80 },
            {
              title: '启用',
              width: 90,
              render: (_value, record: ModelConfig) => (
                <Switch
                  checked={record.enabled}
                  onChange={(checked) => void toggle(record, checked)}
                />
              ),
            },
          ]}
        />
      </Card>
    </div>
  )
}
