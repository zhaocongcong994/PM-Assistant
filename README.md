# Nexus —— Agent 产品需求引擎

> 给它一个 PRD、一个需求点子、或一个可落地的 Agent 方向，它推导出架构方案、组件选型与代码骨架，直到一个能在真实环境运行的产品。
>
> 推导入口：**[BLUEPRINT.md](./BLUEPRINT.md)** —— 从 PRD 到产品的五步 SOP、架构决策映射表、生产级组件库、"可落地"验收标准。
>
> 🌱 **纯小白？只有一个想法、还没 PRD？** 先看 **[《纯小白入门指南》](./docs/beginner-guide.md)**。
>
> 本仓库同时保留一套**组件参考实现**（`s01`~`s17`），每个组件一个可运行的 `code.py`，供落地时对照与复用。

---

## 核心模型

```
Agent = 模型（智能，已训练好） + Harness（载具，你构建的代码）
```

```
Harness = 工具 + 知识 + 观察 + 行动接口 + 权限
```

模型做决策，Harness 执行。你构建的是 Harness，不是智能。

---

## 从 PRD 到产品：五步推导

这是本引擎的核心工作流。完整方法见 **[BLUEPRINT.md](./BLUEPRINT.md)**。

```
PRD / 点子 / 方向
      │
      ▼
① 需求拆解   从 PRD 提取硬事实（目标/用户/闭环/边界/约束），缺失标注"待确认"
      │
      ▼
② 领域建模   把事实映射到 Harness 五要素（工具/知识/观察/行动/权限）
      │
      ▼
③ 组件选型   查「架构决策映射表」，只选需要的组件
      │
      ▼
④ 架构设计   产出方案：分层架构图（7层）/工具清单/系统提示词/安全边界/部署形态
      │
      ▼  （确认后再生成代码）
⑤ 代码生成   骨架 → 硬化 → 产品化
```

**原则：先方案，确认后再生成代码。**

---

## 架构决策映射表

| PRD 特征 | 选用组件 | 参考实现 |
|---|---|---|
| 需要调用外部系统/API/数据源 | 工具系统 | s02 |
| 涉及破坏性/敏感操作 | 权限系统 | s03 |
| 需要审计、拦截、埋点 | 钩子系统 | s04 |
| 多步骤、需跟踪进度 | 任务规划 | s05 |
| 上下文会爆、任务可并行 | 子 Agent | s06 |
| 领域知识库庞大 | 技能加载 | s07 |
| 长会话、日志量大 | 上下文压缩 | s08 |
| 需跨会话记住偏好/决策 | 记忆系统 | s09 |
| 目标需持久化、断点续跑 | 任务系统 | s10 |
| 有慢操作需不阻塞 | 后台任务 | s11 |
| 需定时自治触发 | 定时调度 | s12 |
| 多任务并行、需隔离工作区 | Agent 团队 | s13 |
| 需接入外部工具生态 | MCP 插件 | s14 |
| 上述机制需协同 | 集成 Harness | s15 |
| 编排形态固定 | 工作流运行时 | s16 |
| 需自动判断"何时算完成" | 目标闭环 | s17 |

---

## 生产级组件库（17 个）

每个组件一个可运行的 `code.py`，供落地时对照与复用。按功能域分组：

| 域 | 组件 | 一句话 | 参考 |
|---|---|---|---|
| 行动 | Agent Loop | 一切的基础：循环 + 工具 | s01 |
| 行动 | 工具系统 | 循环不变，工具可增 | s02 |
| 行动 | MCP 插件 | 外部工具接入同一工具池 | s14 |
| 约束 | 权限系统 | 先划边界，再给自由 | s03 |
| 约束 | 钩子系统 | 挂循环上，不写循环里 | s04 |
| 认知 | 任务规划 | 先计划后执行 | s05 |
| 认知 | 技能加载 | 用到时再加载 | s07 |
| 认知 | 记忆系统 | 记住该记的，忘掉该忘的 | s09 |
| 认知 | 目标闭环 | 目标决定循环何时停止 | s17 |
| 上下文 | 子 Agent | 全新消息列表，隔离噪声 | s06 |
| 上下文 | 上下文压缩 | 长上下文腾空间 | s08 |
| 协作 | 任务系统 | 大目标拆小任务，持久化 | s10 |
| 协作 | 后台任务 | 慢操作丢后台 | s11 |
| 协作 | 定时调度 | 到点自动触发 | s12 |
| 协作 | Agent 团队 | 队友分工协作 | s13 |
| 编排 | 集成 Harness | 多机制归一循环 | s15 |
| 编排 | 工作流运行时 | 编排形状固定就写进代码 | s16 |

完整说明（含生产级要点）见 **[BLUEPRINT.md](./BLUEPRINT.md) 第五节**。

---

## 核心模式

```python
def agent_loop(messages):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM,
            messages=messages, tools=TOOLS,
        )
        messages.append({"role": "assistant",
                         "content": response.content})

        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            return

        results = []
        for block in tool_calls:
            output = TOOL_HANDLERS[block.name](**block.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": results})
```

循环属于 Agent，机制属于 Harness。这个循环是常量；工具、知识、权限随领域而变。

---

## 快速开始

> 要求 Python 3.10+，仅支持 macOS / Linux（部分组件依赖 `fcntl` 等 Unix 特性）。

### 使用本地网页工作台

1. 运行 `./setup.sh` 初始化 Python 环境（已初始化则可跳过）。
2. 启动 `./run-local.command`，在浏览器打开 <http://127.0.0.1:8765>。
3. 点击左下角「模型设置」，绑定 Anthropic 兼容服务的 API Key、可选 API 地址和一个或多个具体模型 ID（每行一个）。可添加多个连接并测试已保存的型号。
4. 在右上角选择具体型号后创建项目；之后可在同一位置切换，切换对后续请求生效。每个项目独立保存选择。

已有 PRD 时可选择「我有一份 PRD」，粘贴文本、上传本地文件，或输入飞书 `docx` / `wiki` 文档链接及云空间中的 Markdown 文件链接（`/file/`），点击「读取飞书文档」。飞书导入使用本机的 `lark-cli` 和当前飞书用户的阅读权限；需要登录或补充授权时，页面会显示「连接飞书账号」，随后展示授权链接和二维码。如果工作台找不到命令，可设置 `LARK_CLI_PATH` 为其可执行文件路径后重启工作台。若提示无法访问 macOS 钥匙串，请从普通终端运行 `./run-local.command` 启动工作台。

会话和生成文件保存在项目本机的 `.webapp/`；模型连接与 API Key 保存在本机 `.webapp/models.json`，密钥不会返回到浏览器页面；旧 `.env` 配置会在首次使用时迁移。网页仅监听本机地址。

本地页面服务也可直接运行：`python3 app.py`。

### 学生上手：3 步跑起来（macOS + VS Code）

1. **用 VS Code 打开本项目**：右下角弹出「是否安装推荐扩展」→ 点「安装」（会装 Python 扩展）。
2. **一键初始化环境**：菜单 `终端 → 运行任务 → 一键初始化环境`（或命令行执行 `./setup.sh`）。脚本会自动创建 `.venv`、安装依赖、生成 `.env`。
3. **填密钥并运行**：打开 `.env` 填入 `ANTHROPIC_API_KEY` 与 `MODEL_ID`，然后：

```sh
.venv/bin/python s01_agent_loop/code.py         # 最小 agent loop
.venv/bin/python s15_integrated_harness/code.py # 集成 harness 参考
.venv/bin/python s17_goal_loop/code.py          # 目标闭环
```

> 没装 Python？去 <https://www.python.org/downloads/> 装 3.10+（macOS 选 macOS 安装包），或 `brew install python`。
> 手动方式（不用脚本）：`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cp .env.example .env`

**从 PRD 推导产品**：把 [BLUEPRINT.md](./BLUEPRINT.md)（或相关章节）连同你的 PRD 一起交给 AI，要求按五步 SOP 推导，并遵守"先方案后代码"。

---

## 项目结构

```
agent-blueprint/
  BLUEPRINT.md                     # 核心蓝图 + PRD 推导 SOP
  s01_agent_loop/                  # 组件参考实现，每个文件夹：
    README.md                      #   组件说明
    code.py                        #   独立可运行代码
    images/                        #   架构图
  s02_tool_use/
  ...
  s17_goal_loop/
  scaffold/                        # 生产级项目模板（见 scaffold/）
  skills/                          # 领域技能（agent-builder 等）
  tests/                           # 测试
```

---

## 许可证

MIT
