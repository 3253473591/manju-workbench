import { useState } from 'react'
import { Button, Card, Form, Input, Tabs, Typography, message } from 'antd'

import { apiPost } from '../api/client'
import { useAuth } from '../auth/AuthContext'

export default function LoginPage() {
  const { login } = useAuth()
  const [tab, setTab] = useState('login')
  const [submitting, setSubmitting] = useState(false)
  const [loginForm] = Form.useForm()
  const [registerForm] = Form.useForm()

  const doLogin = async (values: { username: string; password: string }) => {
    setSubmitting(true)
    try {
      const user = await login(values.username, values.password)
      message.success(`欢迎回来，${user.displayName}`)
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  const doRegister = async (values: {
    username: string
    password: string
    displayName: string
  }) => {
    setSubmitting(true)
    try {
      await apiPost('/auth/register', values)
      message.success('注册成功，已使用新账号登录')
      await doLogin({ username: values.username, password: values.password })
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="login-page">
      <Card className="login-card" variant="borderless">
        <h1 className="login-title">漫剧工作台</h1>
        <div className="login-subtitle">MANJU DIRECTOR · LOCAL</div>

        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
            {
              key: 'login',
              label: '登录',
              children: (
                <Form form={loginForm} layout="vertical" onFinish={doLogin} size="large">
                  <Form.Item name="username" label="账号" rules={[{ required: true }]}>
                    <Input placeholder="admin" autoComplete="username" />
                  </Form.Item>
                  <Form.Item name="password" label="密码" rules={[{ required: true }]}>
                    <Input.Password placeholder="admin123" autoComplete="current-password" />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" block loading={submitting}>
                    登录
                  </Button>
                </Form>
              ),
            },
            {
              key: 'register',
              label: '注册',
              children: (
                <Form form={registerForm} layout="vertical" onFinish={doRegister} size="large">
                  <Form.Item
                    name="username"
                    label="账号"
                    rules={[
                      { required: true, message: '请输入账号' },
                      { min: 3, message: '账号至少 3 位' },
                      { pattern: /^[A-Za-z0-9_.-]+$/, message: '只能包含字母、数字、_ . -' },
                    ]}
                  >
                    <Input placeholder="用于登录的用户名" autoComplete="username" />
                  </Form.Item>
                  <Form.Item name="displayName" label="显示名" rules={[{ required: true }]}>
                    <Input placeholder="导演用户" />
                  </Form.Item>
                  <Form.Item
                    name="password"
                    label="密码"
                    rules={[{ required: true, message: '请输入密码' }]}
                  >
                    <Input.Password placeholder="至少 6 位" autoComplete="new-password" />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" block loading={submitting}>
                    注册并登录
                  </Button>
                </Form>
              ),
            },
          ]}
        />

        <div className="login-hint">
          <Typography.Text strong>开放注册，新账号固定为 user 角色。</Typography.Text>
          <br />
          本地 Demo 管理员：<code>admin / admin123</code>（首次启动自动 seed，可通过环境变量覆盖）。
          角色由管理员在后台调整；权限拦截在后端。
        </div>
      </Card>
    </div>
  )
}
