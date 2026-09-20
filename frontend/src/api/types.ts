/** 与 docs/openapi.yaml 对应的最小类型集。字段名与契约一致（camelCase）。 */

export type Role = 'user' | 'manager' | 'admin'

export interface User {
  id: string
  username: string
  displayName: string
  role: Role
  status: 'active' | 'disabled'
  lastLoginAt: string | null
  createdAt: string
}

export interface Project {
  id: string
  name: string
  description: string
  status: 'active' | 'archived'
  coverMediaId: string | null
  createdAt: string
  updatedAt: string
}

export interface Episode {
  id: string
  projectId: string
  number: number
  title: string
  status: string
  createdAt: string
}

export interface ProjectDetail {
  project: Project
  episodes: Episode[]
  outlineNodeId: string | null
}

export interface Layout {
  x: number
  y: number
  width: number
  height: number
  zIndex: number
}

export type NodeType = 'text' | 'shot' | 'image' | 'video'

export interface CanvasNode {
  id: string
  episodeId: string
  nodeType: NodeType
  documentId: string | null
  clipId: string | null
  mediaId: string | null
  parentNodeId: string | null
  label: string
  hidden: boolean
  contentFingerprint: string
  createdAt: string
  updatedAt: string
}

export interface CanvasSnapshot {
  episodeId: string
  nodes: { node: CanvasNode; layout: Layout }[]
  edges: { id: string; fromNodeId: string; toNodeId: string; edgeType: string; note: string }[]
}

/** GET /canvas/nodes/{nodeId}：按节点类型展开 document / clip / mediaNode 三者之一。 */
export interface CanvasNodeDetail {
  node: CanvasNode
  layout: Layout | null
  document: ContentDocumentDetail | null
  clip: ClipDetail | null
  mediaNode: MediaNodeDetail | null
}

export interface MediaItem {
  id: string
  mediaType: 'image' | 'video' | 'audio' | 'file'
  fileKey: string
  sizeBytes: number
  sha256: string
  width: number | null
  height: number | null
  durationMs: number | null
  decodeStatus: string
  fileUrl: string
  thumbnailUrl: string | null
}

export interface PromptVersion {
  id: string
  nodeId: string
  revisionNo: number
  visibleText: string
  finalInputHash: string
  source: string
  createdAt: string
}

export interface MediaNode {
  id: string
  episodeId: string
  mediaKind: 'image_plan' | 'image_result' | 'video_plan' | 'video_result'
  mediaItemId: string | null
  sourceMediaId: string | null
  promptVersionId: string | null
  clipId: string | null
  assetId: string | null
  targetName: string
  title: string
  blocked: boolean
  seed: number
  params: Record<string, unknown>
}

export interface MediaNodeDetail {
  mediaNode: MediaNode
  visiblePrompt: string | null
  promptVersions: PromptVersion[]
  media: MediaItem | null
  sourceMedia: MediaItem | null
  referenceImages: MediaItem[]
  frozenClip: { id: string; clipNo: number; title: string; durationMs: number } | null
  sourceSummary: Record<string, unknown>
}

export interface ContentDocument {
  id: string
  episodeId: string
  kind: 'outline' | 'setting' | 'script' | 'clip'
  title: string
  body: string
  status: string
  version: number
  currentRevisionId: string | null
  boundOutlineRevisionId: string | null
  boundSettingRevisionId: string | null
  nodeId: string | null
}

export interface ContentRevision {
  id: string
  documentId: string
  revisionNo: number
  source: string
  title: string
  body: string
  createdAt: string
}

export interface ContentDocumentDetail {
  document: ContentDocument
  currentRevision: ContentRevision | null
  revisions: ContentRevision[]
  hasUnsavedDraft: boolean
}

export interface ClipShot {
  id: string
  clipId: string
  shotNo: number
  startMs: number
  endMs: number
  visualText: string
  cameraText: string
  dialogueText: string
  soundText: string
}

export interface Clip {
  id: string
  episodeId: string
  clipNo: number
  title: string
  durationMs: number
  characters: string[]
  locationName: string
  status: string
  body: string
  version: number
}

export interface ClipDetail {
  clip: Clip
  shots: ClipShot[]
  references: AssetReference[]
}

export interface Asset {
  id: string
  projectId: string
  assetType: 'character' | 'scene' | 'prop'
  canonicalName: string
  variantName: string
  imageKind: string
  status: string
  mediaNodeId: string | null
  createdAt: string
}

export interface AssetReference {
  id: string
  clipId: string
  assetId: string
  slotNo: number
  required: boolean
  source: string
  asset: Asset | null
}

export interface AssetDetail {
  asset: Asset
  candidates: MediaNode[]
  usableResultCount: number
}

export interface Submission {
  id: string
  operation: string
  logicalStatus: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  requestedCount: number
  episodeId: string | null
  nodeId: string | null
  errorCode: string
  errorMessage: string
  createdAt: string
  finishedAt: string | null
}

export interface Attempt {
  id: string
  submissionId: string
  attemptNo: number
  operation: string
  physicalStatus: string
  deliveryStatus: string
  isMock: boolean
  errorCode: string
  errorMessage: string
  createdAt: string
  delivery: { decodeStatus: string; fileKey: string; sha256: string } | null
  usage: {
    inputUnits: number | null
    outputUnits: number | null
    actualAmount: string | null
    billingStatus: string
    isMock: boolean
  } | null
}

export interface SubmissionDetail {
  submission: Submission
  attempts: Attempt[]
  producedNodeIds: string[]
  result: Record<string, unknown> | null
}

export interface BillingSummary {
  currency: string
  attemptCount: number
  actualTotal: string
  estimatedTotal: string
  mockTotal: string
  byOperation: { operation: string; attemptCount: number; amount: string }[]
  unknownUsageCount: number
}

export interface ModelConfig {
  id: string
  provider: string
  capability: string
  productLabel: string
  endpointId: string
  actualModel: string
  enabled: boolean
  isDefault: boolean
  verifiedAt: string | null
  spec: Record<string, unknown>
  priceAmount: string
  priceCurrency: string
  priceUnit: string
  concurrencyLimit: number
  note: string
}

export interface Meta {
  mode: 'mock' | 'real'
  realAvailable: boolean
  imageProducts: string[]
  videoProducts: string[]
}
