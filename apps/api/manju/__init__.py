"""漫剧工作台后端：数据层（模型 / 迁移 / 种子）与 FastAPI 骨架。

后端包名是 `manju` 而不是常见的 `app`：工作台落在 ComfyUI 树内，
ComfyUI 自己的 `app` 包会抢先命中 `sys.path`（见 `docs/开发文档/01-数据结构.md` §7.1）。
"""
