import { useQuery } from '@tanstack/react-query'
import { Card, Empty, Tag } from 'antd'
import { useNavigate } from 'react-router-dom'

import { apiGet } from '../api/client'
import type { Project } from '../api/types'
import { canEdit, useAuth } from '../auth/AuthContext'

export default function ProjectsPage() {
  const navigate = useNavigate()
  const { user } = useAuth()
  const projects = useQuery({
    queryKey: ['projects'],
    queryFn: () => apiGet<{ items: Project[]; total: number }>('/projects?pageSize=50'),
  })

  return (
    <div className="page-pane">
      {!canEdit(user?.role) && (
        <div className="role-note">
          当前角色为 <b>manager</b>：可以查看项目、生产内容和费用，但不能编辑或提交生成。
        </div>
      )}
      <div className="section-card" style={{ marginTop: 16 }}>
        <h2 style={{ margin: 0, fontSize: 18 }}>项目</h2>
        <div style={{ color: '#8491a0', fontSize: 12, marginTop: 4 }}>
          所有已登录用户共享同一组项目；新建项目会自动创建「第 1 集」与居中的大纲节点。
        </div>
      </div>

      {projects.data?.items.length ? (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))',
            gap: 16,
          }}
        >
          {projects.data.items.map((project) => (
            <Card
              key={project.id}
              hoverable
              title={project.name}
              onClick={() => navigate(`/projects/${project.id}`)}
              extra={<Tag color={project.status === 'active' ? 'orange' : 'default'}>{project.status}</Tag>}
            >
              <div style={{ color: '#64748b', fontSize: 13, minHeight: 40 }}>
                {project.description || '（暂无简介）'}
              </div>
              <div style={{ color: '#94a3b8', fontSize: 11, marginTop: 12 }}>
                第 1 集 · 创建于 {project.createdAt.slice(0, 10)}
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Card>
          <Empty description="还没有项目，点击左侧「新建项目」开始" />
        </Card>
      )}
    </div>
  )
}
