# 演示录屏（OBS）

成品：`manju-demo.mp4`（2560×1440 / 30fps / 66 秒 / 2.4 MB，字幕已烧入）。
字幕源：`demo.srt`（文本）与 `demo.ass`（精确排版：微软雅黑 46px、底部居中、拉满 1440p）。

## 这条视频是怎么来的

1. **服务**：后端 `8010`（mock provider、worker 开）、前端 Vite `5177`、MySQL `manju_dev`；
2. **浏览器**：前台 Chrome（`--remote-debugging-port=9222` + 临时 profile），CDP 驱动点击/输入；
3. **OBS**：专用场景集合 `demo`（只含一个「窗口采集」，锁定 Chrome 窗口，静音所有音频设备），
   录制参数 1440p30 / x264 / mkv；通过 obs-websocket 发 `StartRecord` / `StopRecord`；
4. **驱动脚本**：`scripts/record_demo.py`（先干跑、再 `--record`）；
5. **后期**：mkv → `ass=demo.ass` 烧字幕 → h264 CRF20 无音轨 mp4。

## 十拍内容（与字幕一一对应）

| # | 画面 | 说什么 |
|---|---|---|
| ① | 流水线 | S1–S5 共用一条代码路径 |
| ② | 分镜表 | 8 个片段：状态 / 镜头 / 槽位 / 成本一张表 |
| ③ | 镜头墙 | 真实成片缩略图，跨集可见 |
| ④ | 打回 | 审批绑定具体 take，状态进入 rejected |
| ⑤ | 重发 + 回滚 | 历史 take 都在；回滚不写新 revision、不花钱 |
| ⑥ | 付费点（两步） | 提交前零计费；先看预估再确认；出片后时间线可追 |
| ⑦ | 资产库 | 被引用即禁删：列出引用它的片段与槽位 |
| ⑧ | 成本台账 | 按模型 / 按集 / 按片段 / 按天 |
| ⑨ | 第二集槽位 | 资产项目级：第二集直接复用第一集的图 |
| ⑩ | admin | Skill 发布必须试跑；provider 四通道可一键停用 |

## 重录

```powershell
# 1) 起服务（8010 / 5177）与带调试端口的前台 Chrome
# 2) 临时开 OBS websocket（配置：%APPDATA%\obs-studio\plugin_config\obs-websocket\config.json
#    server_enabled=true / auth_required=false），用场景集合 demo + profile demo 启动 OBS
python scripts/record_demo.py            # 干跑：只点不录，截图落在 docs/demo/dry
python scripts/record_demo.py --record   # 正式录：docs/demo/*.mkv + demo.srt
# 3) 烧字幕（见上面的 ffmpeg 命令）
```

> 脚本会自己把演示状态摆好：缺 review 片段就用一次「重发」把它跑回来；
> 资产那拍会先用 `DELETE` 探测找一张**确实被引用**的资产（409 不产生副作用）。
