import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Card, Empty, Select, Space, Table, Tag, message } from 'antd'

import { apiGet, apiPatch } from '../api/client'
import type { User } from '../api/types'
import { useAuth } from '../auth/AuthContext'

export default function AdminUsersPage() {
  const { user } = useAuth()
  const queryClient = useQueryClient()

  const users = useQuery({
    queryKey: ['admin-users'],
    queryFn: () => apiGet<{ items: User[]; total: number }>('/admin/users?pageSize=100'),
    enabled: user?.role === 'admin',
  })

  const patch = async (userId: string, body: Record<string, unknown>) => {
    try {
      await apiPatch(`/admin/users/${userId}`, body)
      message.success('已更新')
      void queryClient.invalidateQueries({ queryKey: ['admin-users'] })
    } catch (error) {
      message.error((error as Error).message)
    }
  }

  if (user?.role !== 'admin') {
    return (
      <div className="page-pane">
        <Card>
          <Empty description="仅 admin 可以访问用户管理（后端返回 403）" />
        </Card>
      </div>
    )
  }

  return (
    <div className="page-pane">
      <Card className="section-card">
        <div style={{ fontSize: 16, fontWeight: 700 }}>用户管理</div>
        <div style={{ color: '#8491a0', fontSize: 12, marginTop: 4 }}>
          注册一律创建 user；只有 admin 能调整角色与启停。角色变更或停用会立刻吊销该账号已有会话；
          禁止禁用或降级最后一个启用的管理员。
        </div>
      </Card>

      <Card>
        <Table
          rowKey="id"
          dataSource={users.data?.items ?? []}
          pagination={false}
          columns={[
            { title: '账号', dataIndex: 'username' },
            { title: '显示名', dataIndex: 'displayName' },
            {
              title: '角色',
              width: 160,
              render: (_value, record: User) => (
                <Select
                  size="small"
                  value={record.role}
                  style={{ width: 120 }}
                  onChange={(role) => void patch(record.id, { role })}
                  options={[
                    { value: 'user', label: 'user' },
                    { value: 'manager', label: 'manager' },
                    { value: 'admin', label: 'admin' },
                  ]}
                />
              ),
            },
            {
              title: '状态',
              width: 180,
              render: (_value, record: User) => (
                <Space>
                  <Tag color={record.status === 'active' ? 'green' : 'red'}>{record.status}</Tag>
                  <a
                    onClick={() =>
                      void patch(record.id, {
                        status: record.status === 'active' ? 'disabled' : 'active',
                      })
                    }
                  >
                    {record.status === 'active' ? '停用' : '启用'}
                  </a>
                </Space>
              ),
            },
            {
              title: '最近登录',
              dataIndex: 'lastLoginAt',
              render: (value: string | null) => (value ? value.replace('T', ' ').replace('Z', '') : '—'),
            },
          ]}
        />
      </Card>
    </div>
  )
}
