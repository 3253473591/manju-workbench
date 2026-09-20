/**
 * 后端 API 客户端。
 *
 * 约定（docs/openapi.yaml）：Base path `/api/v1`；鉴权用 `Authorization: Bearer <token>`；
 * 错误响应统一为 { success:false, error:{ code, message, details }, requestId, timestamp }。
 * 前端只依赖 `code` 做分支，不依赖中文 message。
 */

const BASE = '/api/v1'
const TOKEN_KEY = 'manju.token'

export class ApiError extends Error {
  code: string
  status: number
  details: Record<string, unknown>

  constructor(code: string, message: string, status: number, details: Record<string, unknown> = {}) {
    super(message)
    this.code = code
    this.status = status
    this.details = details
  }
}

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

type RequestOptions = {
  method?: string
  body?: unknown
  idempotencyKey?: string
  formData?: FormData
  signal?: AbortSignal
}

export async function api<T = unknown>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  headers['X-Request-Id'] = `req_web_${Math.random().toString(36).slice(2, 12)}`
  if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey

  let body: BodyInit | undefined
  if (options.formData) {
    body = options.formData
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  const response = await fetch(`${BASE}${path}`, {
    method: options.method ?? 'GET',
    headers,
    body,
    signal: options.signal,
  })

  if (response.status === 204) return undefined as T

  const text = await response.text()
  const payload = text ? JSON.parse(text) : {}

  if (!response.ok) {
    const error = payload?.error ?? {}
    throw new ApiError(
      error.code ?? 'INTERNAL_ERROR',
      error.message ?? `请求失败（HTTP ${response.status}）`,
      response.status,
      error.details ?? {},
    )
  }
  return payload as T
}

export function newIdempotencyKey(prefix = 'web'): string {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`
}

export const apiGet = <T,>(path: string) => api<T>(path)
export const apiPost = <T,>(path: string, body?: unknown, idempotencyKey?: string) =>
  api<T>(path, { method: 'POST', body, idempotencyKey })
export const apiPatch = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: 'PATCH', body })
export const apiDelete = (path: string) => api<void>(path, { method: 'DELETE' })
