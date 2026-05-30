# iCloud HME Generator

基于 iCloud Hide My Email 协议，自动批量创建 `@icloud.com` 隐私邮箱的工具。

- ⏱ 定时调度 — 每小时随机触发，触达上限自动停止，等下一轮
- 🌐 Web UI — 白色简洁面板，仪表盘 + 日志 + 邮箱列表
- 📡 联网校时 — HTTP 对时，不怕服务器时钟漂移

## 前提条件

- **iCloud+ 订阅**（Hide My Email 需要 iCloud+）
- Python 3.10+
- Windows / macOS / Linux

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 获取 cookie
#    用 Chrome 打开 https://www.icloud.com/settings/ 并登录
#    安装 Cookie Editor 扩展 → Export → Header String

# 3. 启动 Web UI + 调度器
python web_ui.py --scheduler

# 4. 打开 http://127.0.0.1:5050
#    点击左侧「导入 Cookie」粘贴 Header String
```

## 使用方式

### Web UI（推荐）

```bash
python web_ui.py --scheduler          # 启动 Web + 自动调度
python web_ui.py --port 8080          # 指定端口
python web_ui.py --cookies cookies.json  # 从文件加载 cookie
```

打开浏览器访问 `http://127.0.0.1:5050`，界面提供：

- 仪表盘：累计/今日/本轮统计 + 下次触发倒计时
- 手动创建：一次一个或批量
- 调度器：启停控制，状态实时显示
- 邮箱列表：一键复制 / 全部复制 / CSV 导出

### CLI 独立调度器

```bash
python scheduler.py --cookies cookies.json
```

纯命令行，无 Web 界面，适合服务器挂机。

### CLI 手动操作

```bash
# 创建 1 个
python icloud_hme.py create

# 批量创建 5 个
python icloud_hme.py create -n 5 -o results.json

# 列出所有别名
python icloud_hme.py list

# 删除指定别名
python icloud_hme.py delete --email xxx@icloud.com

# 导出 Chrome cookie 到文件
python icloud_hme.py export-cookies -o cookies.json
```

## Cookie 获取

三种方式任选：

| 方式 | 说明 |
|------|------|
| Chrome 自动提取 | Windows 下自动从 Chrome 读 cookie（需登录过 icloud.com） |
| Cookie Editor Header String | 粘贴到 Web UI 导入框，自动保存 |
| 命令行 `--cookies` | 指定 JSON 或 Header String 文件 |

导入一次后自动存盘 `cookies.json`，重启不再需要重新粘贴。

## 调度逻辑

```
每个整点的 前15分钟或后15分钟 随机选一刻触发
  → 创建到 iCloud 返回上限为止
  → 等待下一个随机触发时刻
  → 连续两轮都因上限失败 → 延迟 5 分 23 秒重试
  → 任意两轮间隔 ≥ 45 分钟
```

时间基于联网校准（HTTP Date 头），不依赖本地时钟。

## 文件结构

```
├── icloud_hme.py       # 核心库：cookie 提取 / HME API / 别名操作
├── web_ui.py           # Flask Web 面板 + 内置调度器
├── scheduler.py        # 独立命令行调度器
└── requirements.txt    # pip 依赖
```

运行时生成的文件（已 gitignore）：

```
cookies.json           # 导入的 cookie（自动持久化）
scheduler_state.json   # 调度器状态
logs/                  # 运行日志
results/               # 创建的邮箱列表
```

## 依赖

```
requests>=2.25          # HTTP
pycryptodome>=3.15     # Chrome cookie 解密 (Windows)
pywin32>=305           # Windows DPAPI (仅 Windows)
flask>=3.0             # Web UI
```

## 交流

灌水 QQ 群：**1060714372**

## License

MIT
