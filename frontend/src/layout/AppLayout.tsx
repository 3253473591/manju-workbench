import { useMemo, useState } from 'react'
import { Link, Outlet, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Avatar, Button, Form, Input, Modal, Tag, Tooltip, message } from 'antd'
import {
  AppstoreOutlined,
  DatabaseOutlined,
  DeploymentUnitOutlined,
  FileTextOutlined,
  SettingOutlined,
  TeamOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'

import { apiGet, apiPost } from '../api/client'
import type { Meta, Project, ProjectDetail } from '../api/types'
import { canBill, canEdit, isAdmin, useAuth } from '../auth/AuthContext'

interface ProjectPage {
  items: Project[]
  total: number
}

export default function AppLayout() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const params = useParams()
  const queryClient = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [form] = Form.useForm<{ name: string; description?: string }>()

  const projects = useQuery({
    queryKey: ['projects'],
    queryFn: () => apiGet<ProjectPage>('/projects?pageSize=50'),
  })

  const meta = useQuery({
    queryKey: ['meta'],
    queryFn: () => apiGet<Meta>('/meta'),
  })

  const activeProjectId = params.projectId

  const activeProject = useMemo(
    () => projects.data?.items.find((item) => item.id === activeProjectId),
    [projects.data, activeProjectId],
  )

  const detail = useQuery({
    queryKey: ['project', activeProjectId],
    queryFn: () => apiGet<ProjectDetail>(`/projects/${activeProjectId}`),
    enabled: Boolean(activeProjectId),
  })

  const createProject = useMutation({
    mutationFn: (values: { name: string; description?: string }) =>
      apiPost<ProjectDetail>('/projects', {
        name: values.name,
        description: values.description ?? '',
      }),
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
      setCreateOpen(false)
      form.resetFields()
      message.success('项目已创建，已自动生成第 1 集与大纲节点')
      navigate(`/projects/${data.project.id}`)
    },
    onError: (error: Error) => message.error(error.message),
  })

  const isWorkspace = location.pathname.includes('/projects/') && !location.pathname.endsWith('/assets')

  const breadcrumb = activeProject
    ? `项目 / ${activeProject.name} / ${detail.data?.episodes[0]?.title ?? '第 1 集'}`
    : '所有项目'

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="sidebar-title">漫剧工作台</div>
          <div className="sidebar-subtitle">MANJU DIRECTOR · LOCAL</div>
        </div>

        {canEdit(user?.role) && (
          <Button
            className="new-project-btn"
            type="primary"
            icon={<ThunderboltOutlined />}
            onClick={() => setCreateOpen(true)}
          >
            ＋ 新建项目
          </Button>
        )}

        <div className="sidebar-section-label">项目</div>
        <div className="project-list">
          {projects.data?.items.map((project) => (
            <button
              key={project.id}
              type="button"
              className={`project-chip${project.id === activeProjectId ? ' active' : ''}`}
              onClick={() => navigate(`/projects/${project.id}`)}
            >
              <span className="project-chip-name">{project.name}</span>
              <span className="project-chip-meta">第 1 集</span>
            </button>
          ))}
          {!projects.data?.items.length && <div className="sidebar-empty">还没有项目</div>}
        </div>

        <div className="sidebar-section-label">工作区</div>
        <nav className="sidebar-nav">
          {activeProjectId && (
            <>
              <Link className={isWorkspace ? 'active' : ''} to={`/projects/${activeProjectId}`}>
                <DeploymentUnitOutlined /> 生产画布
              </Link>
              <Link
                className={location.pathname.endsWith('/assets') ? 'active' : ''}
                to={`/projects/${activeProjectId}/assets`}
              >
                <DatabaseOutlined /> 资产库
              </Link>
              <Link
                className={location.pathname.endsWith('/tasks') ? 'active' : ''}
                to={`/projects/${activeProjectId}/tasks`}
              >
                <AppstoreOutlined /> 任务记录
              </Link>
              {canBill(user?.role) && (
                <Link
                  className={location.pathname.endsWith('/billing') ? 'active' : ''}
                  to={`/projects/${activeProjectId}/billing`}
                >
                  <FileTextOutlined /> 费用账单
                </Link>
              )}
            </>
          )}
          {isAdmin(user?.role) && (
            <>
              <div className="sidebar-section-label inline">管理</div>
              <Link className={location.pathname === '/admin/users' ? 'active' : ''} to="/admin/users">
                <TeamOutlined /> 用户管理
              </Link>
              <Link className={location.pathname === '/admin/models' ? 'active' : ''} to="/admin/models">
                <SettingOutlined /> 模型配置
              </Link>
            </>
          )}
        </nav>

        <div className="sidebar-footer">
          <Avatar style={{ backgroundColor: '#f59e0b', color: '#241407' }}>
            {user?.displayName?.slice(0, 1).toUpperCase()}
          </Avatar>
          <div className="sidebar-user">
            <div className="sidebar-user-name">{user?.displayName}</div>
            <div className="sidebar-user-role">
              {user?.role} · 已登录
              <Button type="link" size="small" onClick={() => void logout()}>
                退出
              </Button>
            </div>
          </div>
        </div>
      </aside>

      <div className="main-column">
        <header className="topbar">
          <div>
            <div className="topbar-title">{isWorkspace ? '生产画布' : '工作区'}</div>
            <div className="topbar-subtitle">{breadcrumb}</div>
          </div>
          <div className="topbar-actions">
            <Tag color={meta.data?.mode === 'real' ? 'red' : 'orange'}>
              {meta.data?.mode === 'real' ? 'real 模式' : 'mock 模式'}
            </Tag>
            {meta.data && !meta.data.realAvailable && (
              <Tooltip title="没有任何模型档位通过真实调用核验，real 档位不会显示为可用">
                <Tag color="default">real 未核验</Tag>
              </Tooltip>
            )}
          </div>
        </header>
        <div className="content">
          <Outlet />
        </div>
      </div>

      <Modal
        title="新建项目"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={createProject.isPending}
        okText="创建"
      >
        <Form form={form} layout="vertical" onFinish={(values) => createProject.mutate(values)}>
          <Form.Item name="name" label="项目名" rules={[{ required: true, message: '请输入项目名' }]}>
            <Input placeholder="例如：死党落水后，我竟拥有两个校花女友" />
          </Form.Item>
          <Form.Item name="description" label="一句话简介">
            <Input.TextArea rows={3} placeholder="可留空" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
