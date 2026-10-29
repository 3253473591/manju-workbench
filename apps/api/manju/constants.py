"""`03` §5 参数总表的代码侧唯一来源。

协议（`00` §5 第 4 条）：文档只引用这些名字，代码只读这些常量，不再各自写魔法数字。
`tests/test_constants.py` 会解析 `03` §5 的表格逐行比对 —— 改一边不改另一边测试就红。

三类值**刻意不在这里**：

- **provider 的单价**（`PRICE_VIDEO_MINI` 等）→ 由 provider 配置下发，见 `PROVIDER_CONFIGURED`。
  调价不该要求改代码重新部署；缺失时启动即失败（`03` §12.1）。
- **参考槽位容量**（`01` §3.8，seedance-2.0 是 9/3/3、2.5 是 30/10/10）→ 同样是 provider 配置。
  写死在这里，换模型就等于改代码。
- **字段的字符串长度上限**（`03` §6.1）→ 跟着 pydantic 请求模型走，不在这儿留第二份。

数值型边界（时长、镜头数、序号、资产体积）在这里：它们要被 domain 层在入库前后反复用到，
放在 schema 里等于每加一个入口就抄一遍。
"""

from decimal import Decimal

# ---------------------------------------------------------------- 重试与超时（03 §3 / §5）

MAX_ATTEMPTS = 3
RETRY_BACKOFF = (1, 2, 4)  # 秒；429 / 5xx 用同一组退避
JITTER = Decimal("0.20")  # ±20%，防同步重试打爆上游
RETRY_AFTER_MAX = 300  # 上游要求的等待超过这个值就不排队干等，直接失败

TIMEOUT_TEXT = 120
TIMEOUT_IMAGE = 300
TIMEOUT_VIDEO = 1800  # 真实 12 s 片段实测约 3 分钟，留 10× 余量
TIMEOUT_REVIEW = 300
JOB_TOTAL_TIMEOUT = 3600  # 墙钟上限；没有它，3 次 TIMEOUT_VIDEO 叠加能挂 1.5 小时

# ---------------------------------------------------------------- 产物下载（03 §3.1）

MEDIA_DOWNLOAD_TIMEOUT = 120
ASSET_DOWNLOAD_RETRY = 2  # 签名过期（403/410）不计入重试

# ---------------------------------------------------------------- 心跳与协作（03 §5 / §6.4）

JOB_HEARTBEAT = 30  # worker 上报间隔
JOB_HEARTBEAT_DEAD = 120  # 超过即判僵死、重新入队
LOCK_TTL = 10  # 编辑锁，心跳续期；只是提示，不是权限
PRESENCE_TTL = 30

# ---------------------------------------------------------------- 上下文与 Skill（03 §4.3 / §5）

CTX_INPUT_LIMIT = 12_000
SKILL_SECTION_MAX = 2_000  # 单节字数
SKILL_BODY_MAX = 200_000  # 字符；真实一篇终剪提示词 168 KB ≈ 5.6 万字

# ---------------------------------------------------------------- 预算（元）

BUDGET_JOB_DEFAULT = Decimal("12.000000")
BUDGET_JOB_MAX = Decimal("100.000000")
BUDGET_PROJECT_DAILY = Decimal("100.000000")
BUDGET_LIMIT_MIN = Decimal("0.01")
TOKENS_PER_VIDEO_SECOND = 21_675  # 实测常数：用量只跟时长 × 分辨率挂钩

# ---------------------------------------------------------------- 分页（00 §3.6）

PAGE_LIMIT_MIN = 1
PAGE_LIMIT_MAX = 200
PAGE_LIMIT_DEFAULT = 50

# ---------------------------------------------------------------- 传输与协议（03 §6.7 / §8.1）

BODY_MAX_BYTES = 2_097_152  # 2 MB，反代层先拒
IDEM_KEY_MAX = 128
IDEM_KEY_PATTERN = rf"^[A-Za-z0-9._:-]{{1,{IDEM_KEY_MAX}}}$"

# ---------------------------------------------------------------- 限流（03 §6.5 / §12.4）

LOGIN_FAIL_LIMIT = 5
LOGIN_FAIL_WINDOW = 60  # 秒；同 (IP, email) 组合
SSE_BUFFER = 100
SSE_POLL = 0.5  # 跨副本兜底轮询周期（秒）：单副本走进程内直推，不受它影响
EVENT_KEEP_DAYS = 1  # event_log 保留天数：只用于补发/续传，不是审计日志
EVENT_PRUNE_EVERY = 500  # 每发布 N 条顺手清一次旧行
SSE_PING = 15
SSE_MAX_CONNECTIONS_PER_USER = 5
PROJECT_VIDEO_CONCURRENCY = 1  # 同项目同时只跑一个 video job
PROJECT_TEXT_CONCURRENCY = 3

# ---------------------------------------------------------------- 观测（03 §5 / §11）

TRACE_KEEP = 20  # job trace 持久化上限

# ---------------------------------------------------------------- 熔断（03 §7.2）

CIRCUIT_FAIL_THRESHOLD = 5
CIRCUIT_COOLDOWN = 300

# ---------------------------------------------------------------- 数值边界（03 §6.2 / §6.4）

DURATION_MIN = Decimal("1.00")
DURATION_MAX = Decimal("15.00")  # Seedance 单片段硬上限
SHOT_SUM_TOLERANCE = Decimal("0.05")  # 末镜 end 与标称时长的容差
MAX_SHOTS = 8
EPISODE_IDX_MAX = 50
CLIP_IDX_MAX = 30
ASSET_BYTES_MAX = 52_428_800  # 50 MB，对应 03 §6.2 的 52,428,800
ASSET_DIM_MAX = 8192
TIME_RANGE_MAX_DAYS = 366

# ---------------------------------------------------------------- 记忆治理（01 §4.8）

LESSON_ARCHIVE_AFTER = 200  # 单 scope 超过这个条数才归档
LESSON_ARCHIVE_IDLE_DAYS = 90

# ---------------------------------------------------------------- 不在本文件里的参数

# 03 §5 里靠 provider 配置下发、因此没有同名常量的条目。
# 启动段逐项校验它们存在（03 §12.1），缺失即 exit 1 —— 成本恒为 0 的静默错账最难查。
PROVIDER_CONFIGURED = ("PRICE_VIDEO_MINI",)
