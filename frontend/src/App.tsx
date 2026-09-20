import { Navigate, Route, Routes } from 'react-router-dom'
import { Spin } from 'antd'

import { useAuth } from './auth/AuthContext'
import AppLayout from './layout/AppLayout'
import LoginPage from './pages/LoginPage'
import ProjectsPage from './pages/ProjectsPage'
import WorkspacePage from './pages/WorkspacePage'
import AssetsPage from './pages/AssetsPage'
import TasksPage from './pages/TasksPage'
import BillingPage from './pages/BillingPage'
import AdminUsersPage from './pages/AdminUsersPage'
import AdminModelsPage from './pages/AdminModelsPage'

export default function App() {
  const { user, loading } = useAuth()

  if (loading) {
    return (
      <div className="boot-screen">
        <Spin size="large" tip="正在加载工作台…" />
      </div>
    )
  }

  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    )
  }

  return (
    <Routes>
      <Route path="/login" element={<Navigate to="/projects" replace />} />
      <Route element={<AppLayout />}>
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/projects/:projectId" element={<WorkspacePage />} />
        <Route path="/projects/:projectId/assets" element={<AssetsPage />} />
        <Route path="/projects/:projectId/tasks" element={<TasksPage />} />
        <Route path="/projects/:projectId/billing" element={<BillingPage />} />
        <Route path="/admin/users" element={<AdminUsersPage />} />
        <Route path="/admin/models" element={<AdminModelsPage />} />
        <Route path="*" element={<Navigate to="/projects" replace />} />
      </Route>
    </Routes>
  )
}
