#!/usr/bin/env python3
"""Local web workbench for Nexus."""

from __future__ import annotations

import json
import io
import base64
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from anthropic import Anthropic
from dotenv import load_dotenv, set_key

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
STATE_DIR = ROOT / ".webapp"
OUTPUT_DIR = STATE_DIR / "generated"
STATE_PATH = STATE_DIR / "sessions.json"
ENV_PATH = ROOT / ".env"
HOST = "127.0.0.1"
PORT = int(os.getenv("WEB_PORT", "8765"))
load_dotenv(ENV_PATH, override=True)

PHASES = ["brief", "components", "architecture", "development", "ui_design", "frontend", "backend", "verification"]
PHASE_LABELS = {
    "brief": "需求访谈", "components": "组件确认", "architecture": "架构方案",
    "development": "开发任务", "ui_design": "页面设计确认", "frontend": "前端开发", "backend": "后端开发",
    "verification": "联调验收",
}
COMPONENTS = [
    ("tools", "工具系统", "需要接入外部系统、文件、API 或数据源"),
    ("permissions", "权限系统", "执行写入、命令或其他敏感操作前需要审批"),
    ("hooks", "钩子系统", "需要在工具调用前后拦截、记录或审计"),
    ("planning", "任务规划", "工作包含多个需要跟踪的步骤"),
    ("subagents", "子 Agent", "存在可并行任务或需要隔离上下文的工作"),
    ("skills", "技能加载", "领域知识较多，需要按需加载"),
    ("compaction", "上下文压缩", "对话或工具结果可能超过模型上下文"),
    ("memory", "记忆系统", "需要跨会话保留用户偏好或产品决策"),
    ("tasks", "持久化任务", "需要断点续跑、任务依赖或持久状态"),
    ("background", "后台任务", "存在较慢的操作，不能阻塞主交互"),
    ("scheduler", "定时调度", "需要按计划自动触发任务"),
    ("teams", "Agent 团队", "需要多个 Agent 并行开发或隔离工作区"),
    ("mcp", "MCP 插件", "需要接入 MCP 工具生态"),
    ("workflow", "工作流运行时", "编排步骤固定、需要稳定恢复和阶段进度"),
]

DISCOVERY_FIELDS = {
    "target_users": "目标用户",
    "problem": "要解决的问题",
    "workflow": "核心使用流程",
    "mvp_scope": "首版范围",
    "success_criteria": "成功标准",
}
DISCOVERY_STAGES = ["direction", "value", "workflow", "scope", "constraints"]

PROMPT = """你是 Nexus 的产品与开发助手。根据用户的产品想法，按需协助完成需求梳理、Agent 组件选型、架构方案与代码开发。
严格遵循：未知信息明确标注待确认；先交付方案，用户确认后才进入开发；组件只选解决实际需求的最小集合；区分产品所需的 Agent 能力组件和工作台界面的 UI 组件。
回复使用中文，直接清晰。不要声称已执行实际没有执行的操作。当前工作阶段由上下文给出，只处理该阶段目标。
对于代码生成任务，将可运行文件保存在要求的输出目录，以文件列表形式返回，并同时报告文件用途、运行命令和验证建议。"""
DISCOVERY_SYSTEM = """你是耐心的产品访谈助手。当前目标是帮助用户逐步澄清自己的产品需求，而不是替用户设计产品或一次性写出 PRD。
每轮先简短复述，再问一个主要问题；可以提供通俗的示例或选项，但必须把建议与用户已确认的信息分开。用户说“不知道”时降低问题难度、给出启发，不擅自代答。
只收集用户真实表达；不要推荐组件、架构、技术栈或开发计划。达到需求条件后，告知用户可以整理 PRD 草稿，等待用户主动点击整理。"""

_lock = threading.RLock()
_operations: dict[str, dict] = {}
_feishu_auth_flows: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_state() -> dict:
    if not STATE_PATH.exists():
        return {"sessions": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data.get("sessions"), list) else {"sessions": []}
    except (OSError, json.JSONDecodeError):
        return {"sessions": []}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_PATH)


def _recover_interrupted_sessions() -> None:
    with _lock:
        state = _read_state()
        changed = False
        for session in state["sessions"]:
            if session.get("status") == "running":
                session["status"] = "ready"
                session["error"] = "工作台曾重启，当前任务已中断。请重新运行该阶段。"
                session["updated_at"] = _now()
                changed = True
        if changed:
            _save_state(state)


def _public_session(session: dict) -> dict:
    return {k: v for k, v in session.items() if k != "messages"}


def _find_session(state: dict, session_id: str) -> dict | None:
    return next((s for s in state["sessions"] if s["id"] == session_id), None)


class FeishuCliError(Exception):
    def __init__(self, message: str, status: int = 502, auth_required: bool = False):
        super().__init__(message)
        self.status = status
        self.auth_required = auth_required


def _run_feishu_cli(args: list[str], timeout: int = 60) -> dict:
    cli = os.getenv("LARK_CLI_PATH") or shutil.which("lark-cli")
    if not cli:
        raise FeishuCliError("本机未找到 lark-cli。请先安装，或设置 LARK_CLI_PATH 后重启工作台。", 503)
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    try:
        result = subprocess.run([cli, *args], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise FeishuCliError("无法运行 lark-cli，请检查安装路径。", 503)
    except subprocess.TimeoutExpired:
        raise FeishuCliError("飞书操作超时，请稍后重试。", 504)
    except OSError:
        raise FeishuCliError("无法运行 lark-cli，请检查安装路径。", 503)
    try:
        payload = json.loads(result.stdout if result.returncode == 0 else result.stderr)
    except (ValueError, TypeError):
        raise FeishuCliError("飞书返回了无法识别的响应。")
    if not isinstance(payload, dict):
        raise FeishuCliError("飞书返回了无法识别的响应。")
    if result.returncode != 0 or not payload.get("ok"):
        error = payload.get("error") or {}
        message = str(error.get("message", "")) if isinstance(error, dict) else ""
        subtype = str(error.get("subtype", "")) if isinstance(error, dict) else ""
        error_type = str(error.get("type", "")) if isinstance(error, dict) else ""
        if "keychain" in message.lower():
            raise FeishuCliError("当前服务无法访问 macOS 钥匙串。请从普通终端启动 ./run-local.command 后重试。", 503)
        if error_type in ("authorization", "authentication") or subtype in (
                "missing_scope", "unauthorized", "not_authenticated", "not_logged_in") or any(
                term in message.lower() for term in ("not logged", "login required", "token expired", "unauthorized")):
            raise FeishuCliError("飞书账号需要登录或补充授权。", 401, True)
        if "permission" in message.lower() or "forbidden" in message.lower():
            raise FeishuCliError("当前飞书账号没有权限读取该文件。", 403)
        raise FeishuCliError("读取飞书资源失败，请检查链接和当前账号的访问权限。")
    return payload


def _valid_feishu_doc_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > 2048 or any(ord(char) < 32 for char in url):
        return None
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        allowed_hosts = ("feishu.cn", "larksuite.com", "larkoffice.com", "doubao.com")
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port
                or not any(host == base or host.endswith("." + base) for base in allowed_hosts)
                or not re.fullmatch(r"/(?:docx|wiki|file|drive/file)/[A-Za-z0-9_-]+/?", parsed.path)):
            return None
    except ValueError:
        return None
    return url


def _new_discovery(entry_mode: str) -> dict:
    return {"entry_mode": entry_mode, "status": "interviewing", "stage": "direction",
            "facts": {}, "open_questions": [], "can_draft": False, "draft_version": 0, "started": False}


def _discovery_context(session: dict) -> str:
    discovery = session.get("discovery") or _new_discovery(session.get("entry_mode", "idea"))
    facts = discovery.get("facts", {})
    fact_lines = [f"- {DISCOVERY_FIELDS.get(key, key)}：{value.get('value', '')}（用户原话依据：{value.get('evidence', '')}）"
                  for key, value in facts.items()]
    missing = [label for key, label in DISCOVERY_FIELDS.items() if key not in facts]
    mode = discovery.get("entry_mode", "idea")
    mode_instruction = ("用户选择了已有 PRD 场景：先提取文档已明确的信息，只针对缺失或矛盾处追问。"
                        if mode == "prd" else "用户只有模糊想法：通过对话逐步明确需求，绝不能把场景选择文字当成产品需求。")
    transcript = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}"
                            for m in session.get("messages", [])[-30:])
    return (f"当前阶段：需求访谈；入口模式：{mode}（{mode_instruction}）\n"
            f"当前访谈环节：{discovery.get('stage', 'direction')}\n"
            f"已从用户表达中整理的信息：\n" + ("\n".join(fact_lines) or "（尚无已确认信息）") + "\n"
            f"仍缺少的核心信息：{'、'.join(missing) or '无'}\n"
            f"当前待确认问题：{'；'.join(discovery.get('open_questions', [])) or '无'}\n"
            "你正在做产品访谈，不是在设计方案或编写 PRD。每轮先简短复述理解，再问 1 个主要问题，最多附 1 个相关追问。按方向、价值、流程、范围、约束逐步推进；用户答不上来时给通俗例子或选择建议，并保留待确认。不得假装用户已决定，不得生成完整需求文档，不得建议 Agent 组件、架构或技术方案。\n"
            "必须调用 discovery_update 工具返回本轮内容。facts 只填写从用户消息逐字引用得到的事实，每项提供可在用户原消息中找到的 evidence 原文；不能把你的建议写成事实。核心字段为 target_users、problem、workflow、mvp_scope、success_criteria。信息不足时 ready_for_draft 必须为 false。\n"
            f"完整访谈记录：\n{transcript}")


def _discovery_tool() -> dict:
    return {"name": "discovery_update", "description": "返回需求访谈的下一步问题和有用户原话依据的事实。",
            "input_schema": {"type": "object", "properties": {
                "assistant_message": {"type": "string"},
                "stage": {"type": "string", "enum": DISCOVERY_STAGES},
                "facts": {"type": "array", "items": {"type": "object", "properties": {
                    "key": {"type": "string", "enum": list(DISCOVERY_FIELDS)},
                    "value": {"type": "string"}, "evidence": {"type": "string"}},
                    "required": ["key", "value", "evidence"], "additionalProperties": False}},
                "open_questions": {"type": "array", "items": {"type": "string"}},
                "ready_for_draft": {"type": "boolean"}},
                "required": ["assistant_message", "stage", "facts", "open_questions", "ready_for_draft"],
                "additionalProperties": False}}


def _apply_discovery_update(session: dict, update: dict) -> dict:
    discovery = session.setdefault("discovery", _new_discovery(session.get("entry_mode", "idea")))
    facts = dict(discovery.get("facts", {}))
    user_messages = [m.get("content", "") for m in session.get("messages", []) if m.get("role") == "user"]
    for item in update.get("facts", []):
        if not isinstance(item, dict):
            continue
        key, value, evidence = item.get("key"), item.get("value"), item.get("evidence")
        if key not in DISCOVERY_FIELDS or not isinstance(value, str) or not value.strip():
            continue
        if not isinstance(evidence, str) or not evidence.strip() or not any(evidence in message for message in user_messages):
            continue
        facts[key] = {"value": value.strip(), "evidence": evidence.strip()}
    questions = update.get("open_questions", [])
    if not isinstance(questions, list):
        questions = []
    questions = list(dict.fromkeys(q.strip() for q in questions if isinstance(q, str) and q.strip()))[:12]
    stage = update.get("stage")
    if stage not in DISCOVERY_STAGES:
        stage = discovery.get("stage", "direction")
    can_draft = all(key in facts for key in DISCOVERY_FIELDS)
    discovery.update({"facts": facts, "open_questions": questions, "stage": stage,
                      "can_draft": can_draft, "status": "interviewing", "started": True})
    return discovery


def _build_prd(discovery: dict) -> str:
    facts = discovery.get("facts", {})
    lines = ["# 产品需求文档（PRD 草稿）", "", "| 维度 | 已明确内容 |", "|---|---|"]
    for key, label in DISCOVERY_FIELDS.items():
        lines.append(f"| {label} | {facts.get(key, {}).get('value', '待确认')} |")
    lines.extend(["", "## 其他待确认事项"])
    lines.extend(f"- {question}" for question in discovery.get("open_questions", []))
    if not discovery.get("open_questions"):
        lines.append("- 无")
    lines.extend(["", "> 本文由用户访谈整理。确认前仍是草稿；没有依据的信息不会自动补全。"])
    return "\n".join(lines)


def _invalidate_downstream(session: dict, phase: str) -> None:
    descendants = {
        "brief": ["brief", "components", "architecture", "development", "ui_design", "frontend", "backend", "verification"],
        "components": ["components", "architecture", "development", "ui_design", "frontend", "backend", "verification"],
        "architecture": ["architecture", "development", "ui_design", "frontend", "backend", "verification"],
        "development": ["development", "ui_design", "frontend", "backend", "verification"],
        "ui_design": ["ui_design", "frontend", "verification"],
        "frontend": ["frontend", "verification"],
        "backend": ["backend", "verification"],
        "verification": ["verification"],
    }.get(phase, [])
    for descendant in descendants:
        session["phase_results"].pop(descendant, None)
        session["approvals"].pop(descendant, None)


def _client() -> Anthropic:
    load_dotenv(ENV_PATH, override=True)
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key or "xxx" in key.lower():
        raise ValueError("请先在「模型设置」中填写有效的 API Key。")
    kwargs = {"api_key": key}
    if os.getenv("ANTHROPIC_BASE_URL"):
        kwargs["base_url"] = os.environ["ANTHROPIC_BASE_URL"]
    return Anthropic(**kwargs)


def _model() -> str:
    load_dotenv(ENV_PATH, override=True)
    return os.getenv("MODEL_ID", "claude-sonnet-4-6")


def _write_files(session_id: str, files: list) -> list[str]:
    destination = (OUTPUT_DIR / session_id).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    created = []
    for item in files:
        if not isinstance(item, dict):
            continue
        relative = item.get("path", "")
        content = item.get("content", "")
        if not isinstance(relative, str) or not isinstance(content, str):
            continue
        relative_path = Path(relative)
        if relative_path.is_absolute() or not relative_path.parts or any(part in ("..", ".") for part in relative_path.parts):
            continue
        target = (destination / relative_path).resolve()
        if not target.is_relative_to(destination):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(relative_path.as_posix())
    return created


def _phase_context(session: dict, phase: str) -> str:
    scope = session.get("generation_scope", "fullstack")
    detail = {
        "components": "审核产品需求，给出最小 Agent 组件建议。说明选择理由、需求依据、引入影响、放弃项。明确 UI 组件不是 Agent 组件。",
        "architecture": "基于需求和已确认的 Agent 组件输出架构方案，包括 7 层、数据流、安全边界、模型与部署建议，并标注待确认项。",
        "development": f"根据需求和已确认方案拆解开发任务。生成范围={scope}。任务分为前端、后端、联调，标出依赖、验收条件和推荐顺序。",
        "ui_design": "基于已确认需求和架构，先提出前端页面的具体设计供用户评审。详细描述三栏布局、各阶段卡片内容、组件层级、关键状态、窄屏布局和交互。列出页面清单与验收标准。当前只产出页面设计说明，严禁生成代码。",
        "frontend": "为已确认的需求与方案生成前端代码。实现三栏中文工作台，含项目、对话、阶段导航、成果区域、需求/组件/架构确认卡、范围选择、前后端任务进度及状态。生成真实文件。",
        "backend": "为已确认的需求与方案生成后端代码。实现会话持久化、Agent API、前端/后端任务状态、审批事件、配置接口和前端静态资源服务。生成真实文件。",
        "verification": "基于下方提供的实际文件内容作静态审阅。列出文件、检查点、通过项和问题，给出用户可运行的命令和验收步骤。严禁声称运行过未实际执行的命令。明确区分静态检查与运行验收。",
    }.get(phase, "梳理用户需求，列出目标、用户、核心闭环、交付物、边界、约束与待确认项。")
    generated = ""
    if phase == "verification":
        folder = OUTPUT_DIR / session["id"]
        parts, used = [], 0
        code_files = sorted({name for part in ("frontend", "backend")
                             for name in session.get("phase_results", {}).get(part, {}).get("artifacts", [])
                             if Path(name).suffix != ".md"})
        for name in code_files:
            path = (folder / name).resolve()
            if path.is_relative_to(folder.resolve()) and path.is_file():
                remaining = 30000 - used
                if remaining <= 0:
                    parts.append("（其余文件省略，达到静态审阅上下文上限）")
                    break
                body = path.read_text(encoding="utf-8", errors="replace")[:min(10000, remaining)]
                parts.append(f"\n--- {name} ---\n{body}")
                used += len(body)
        generated = "\n\n待静态审阅的实际生成文件：\n" + ("".join(parts) if parts else "（暂无代码文件）")
    names = {key: label for key, label, _description in COMPONENTS}
    selected = "、".join(names.get(key, key) for key in session.get("selected_components", [])) or "尚未确认"
    selection_state = "已确认" if session.get("approvals", {}).get("components") else "当前选择（待确认）"
    return f"工作阶段：{PHASE_LABELS.get(phase, phase)}\n项目需求：{session.get('brief', '')}\n{selection_state} Agent 组件：{selected}\n生成范围：{scope}\n阶段指引：{detail}{generated}"


def _complete_text_response(client: Anthropic, request_args: dict, response) -> str:
    """Continue a plain-text answer until the model finishes its turn."""
    chunks = []
    messages = list(request_args["messages"])
    for continuation in range(6):
        chunk = "\n".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        if chunk:
            chunks.append(chunk)
        if response.stop_reason != "max_tokens":
            return "".join(chunks)
        if not chunk or continuation == 5:
            raise ValueError("模型输出达到长度上限，内容尚未完整。请缩小本轮范围后重试。")
        messages.extend([
            {"role": "assistant", "content": chunk},
            {"role": "user", "content": "上一段因长度限制中断。请从断点继续，只输出尚未输出的内容，不重复前文，直到本轮回答完整结束。"},
        ])
        response = client.messages.create(**{**request_args, "messages": messages})
    raise ValueError("模型输出未能完成，请重试。")


def _run_job(operation_id: str, session_id: str, phase: str, prompt: str,
             save_result: bool = True, task_mode: str = "phase") -> None:
    try:
        client = _client()
        messages = [{"role": "user", "content": prompt}]
        artifacts = []
        request_args = dict(
            model=_model(), max_tokens=12000,
            system=DISCOVERY_SYSTEM if task_mode == "discovery" else PROMPT,
            messages=messages,
        )
        if task_mode == "discovery":
            request_args.update({"tools": [_discovery_tool()],
                                 "tool_choice": {"type": "tool", "name": "discovery_update"}})
        elif save_result and phase in ("frontend", "backend"):
            request_args.update({
                "tools": [{"name": "save_files", "description": "将生成的源代码保存为项目文件。",
                           "input_schema": {"type": "object", "properties": {
                               "files": {"type": "array", "items": {"type": "object", "properties": {
                                   "path": {"type": "string"}, "content": {"type": "string"}},
                                   "required": ["path", "content"], "additionalProperties": False}}},
                               "required": ["files"], "additionalProperties": False}}],
                "tool_choice": {"type": "tool", "name": "save_files"},
            })
        response = client.messages.create(**request_args)
        if response.stop_reason == "max_tokens" and (task_mode == "discovery" or phase in ("frontend", "backend")):
            raise ValueError("模型输出达到长度上限，结构化结果尚未完整。请缩小本轮范围后重试。")
        text = _complete_text_response(client, request_args, response)
        discovery_update = None
        for block in response.content:
            if task_mode == "discovery" and getattr(block, "type", "") == "tool_use" and block.name == "discovery_update":
                discovery_update = block.input
            if save_result and getattr(block, "type", "") == "tool_use" and block.name == "save_files":
                artifacts.extend(_write_files(session_id, block.input.get("files", [])))
        if task_mode == "discovery":
            if not isinstance(discovery_update, dict):
                raise ValueError("访谈结果格式无效，请重试；本轮回答已保留。")
            text = str(discovery_update.get("assistant_message", "")).strip()
            if not text:
                raise ValueError("访谈没有生成下一步问题，请重试；本轮回答已保留。")
        if not text:
            text = ("已生成以下文件：\n" + "\n".join(f"- {name}" for name in artifacts)
                    if artifacts else "已完成本阶段输出。")
        all_artifacts = []
        if save_result:
            filename = f"{_now()[:10]}-{phase}-{uuid.uuid4().hex[:6]}.md"
            artifact_dir = OUTPUT_DIR / session_id
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / filename).write_text(text, encoding="utf-8")
            all_artifacts = [filename] + artifacts
        result = {"text": text, "artifacts": all_artifacts, "phase": phase}
        with _lock:
            state = _read_state()
            session = _find_session(state, session_id)
            if session:
                if task_mode == "discovery":
                    _apply_discovery_update(session, discovery_update)
                session.setdefault("messages", []).append({"role": "assistant", "content": text, "created_at": _now()})
                session["updated_at"] = _now()
                if save_result:
                    session["phase_results"][phase] = result
                session["status"] = "ready"
                _save_state(state)
            _operations[operation_id].update({"status": "completed", "result": result})
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        with _lock:
            if operation_id in _operations:
                _operations[operation_id].update({"status": "failed", "error": error})
            state = _read_state()
            session = _find_session(state, session_id)
            if session:
                session["status"] = "ready"
                session["error"] = error
                _save_state(state)


def _continue_job(operation_id: str, session_id: str, phase: str, partial: str, last_user: str) -> None:
    try:
        client = _client()
        request_args = {
            "model": _model(), "max_tokens": 12000, "system": PROMPT,
            "messages": [
                {"role": "user", "content": _phase_context_for_id(session_id, phase) + "\n用户补充：" + last_user},
                {"role": "assistant", "content": partial},
                {"role": "user", "content": "上一条回答意外中断。请从最后一个字继续，只输出剩余内容，不重复前文，直到完整结束。"},
            ],
        }
        response = client.messages.create(**request_args)
        addition = _complete_text_response(client, request_args, response)
        if not addition.strip():
            raise ValueError("模型没有返回续写内容，请重试。")
        with _lock:
            state = _read_state()
            session = _find_session(state, session_id)
            if not session or not session.get("messages") or session["messages"][-1].get("content") != partial:
                raise ValueError("会话内容已变化，请刷新后重试。")
            completed = partial + addition
            session["messages"][-1]["content"] = completed
            for result in session.get("phase_results", {}).values():
                if result.get("text") == partial:
                    result["text"] = completed
                    for filename in result.get("artifacts", []):
                        path = (OUTPUT_DIR / session_id / filename).resolve()
                        if path.suffix == ".md" and path.is_relative_to((OUTPUT_DIR / session_id).resolve()) and path.is_file() and path.read_text(encoding="utf-8") == partial:
                            path.write_text(completed, encoding="utf-8")
            session["updated_at"] = _now()
            session["status"] = "ready"
            _save_state(state)
            _operations[operation_id].update({"status": "completed"})
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        with _lock:
            _operations[operation_id].update({"status": "failed", "error": error})
            state = _read_state()
            session = _find_session(state, session_id)
            if session:
                session["status"] = "ready"
                session["error"] = error
                _save_state(state)


def _phase_context_for_id(session_id: str, phase: str) -> str:
    with _lock:
        session = _find_session(_read_state(), session_id)
        if not session:
            raise ValueError("项目不存在。")
        return _phase_context(session, phase)


class Handler(BaseHTTPRequestHandler):
    server_version = "Nexus/1.0"

    def log_message(self, fmt, *args):
        print(f"[web] {self.address_string()} {fmt % args}")

    def _json(self, status: int, value: dict) -> None:
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("请求内容超过 2 MB。")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象。")
        return data

    def _file(self, path: Path) -> None:
        if not path.is_file() or not path.resolve().is_relative_to(WEB.resolve()):
            self._json(404, {"error": "页面不存在。"})
            return
        data = path.read_bytes()
        mime = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            return self._file(WEB / "index.html")
        if parsed.path.startswith("/assets/"):
            return self._file(WEB / Path(unquote(parsed.path)).name)
        match = re.fullmatch(r"/api/sessions/([a-f0-9-]+)/download\.zip", parsed.path)
        if match:
            folder = (OUTPUT_DIR / match.group(1)).resolve()
            archive = io.BytesIO()
            if folder.is_dir():
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                    for path in sorted(folder.rglob("*")):
                        if path.is_file() and path.resolve().is_relative_to(folder):
                            bundle.write(path, path.relative_to(folder).as_posix())
            if archive.tell() == 0:
                return self._json(404, {"error": "还没有可下载的项目成果。"})
            data = archive.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="nexus-{match.group(1)[:8]}.zip"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return self.wfile.write(data)
        if parsed.path == "/api/health":
            load_dotenv(ENV_PATH, override=True)
            key = os.getenv("ANTHROPIC_API_KEY", "")
            return self._json(200, {"configured": bool(key and "xxx" not in key.lower()), "model": _model(), "base_url": os.getenv("ANTHROPIC_BASE_URL", "")})
        if parsed.path == "/api/sessions":
            with _lock:
                return self._json(200, {"sessions": [_public_session(s) for s in _read_state()["sessions"]]})
        match = re.fullmatch(r"/api/sessions/([a-f0-9-]+)", parsed.path)
        if match:
            with _lock:
                session = _find_session(_read_state(), match.group(1))
                return self._json(200, {"session": session}) if session else self._json(404, {"error": "项目不存在。"})
        match = re.fullmatch(r"/api/jobs/([a-f0-9-]+)", parsed.path)
        if match:
            with _lock:
                job = _operations.get(match.group(1))
                return self._json(200, job) if job else self._json(404, {"error": "任务不存在。"})
        match = re.fullmatch(r"/api/sessions/([a-f0-9-]+)/files", parsed.path)
        if match:
            folder = OUTPUT_DIR / match.group(1)
            files = []
            if folder.is_dir():
                files = [{"path": p.relative_to(folder).as_posix(), "size": p.stat().st_size} for p in folder.rglob("*") if p.is_file()]
            return self._json(200, {"files": files})
        match = re.fullmatch(r"/api/sessions/([a-f0-9-]+)/file", parsed.path)
        if match:
            relative = parse_qs(parsed.query).get("path", [""])[0]
            folder = (OUTPUT_DIR / match.group(1)).resolve()
            candidate = (folder / relative).resolve()
            if not candidate.is_relative_to(folder) or not candidate.is_file():
                return self._json(404, {"error": "成果文件不存在。"})
            data = candidate.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{candidate.name}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return self.wfile.write(data)
        self._json(404, {"error": "接口不存在。"})

    def do_POST(self):
        try:
            origin = self.headers.get("Origin")
            if origin:
                origin_url = urlparse(origin)
                if origin_url.scheme != "http" or origin_url.hostname not in ("127.0.0.1", "localhost") or origin_url.port != PORT:
                    return self._json(403, {"error": "仅接受本地工作台的请求。"})
            body = self._body()
            if self.path == "/api/config":
                raw_key = body.get("api_key", "")
                if not isinstance(raw_key, str) or not isinstance(body.get("model", ""), str):
                    return self._json(400, {"error": "模型配置格式无效，请检查输入。"})
                key = raw_key.strip()
                model = body["model"].strip()
                if key and (len(key) < 8 or any(ord(char) < 32 for char in key)):
                    return self._json(400, {"error": "API Key 格式无效。"})
                if any(ord(char) < 32 for char in key) or not model or len(model) > 128 or any(ord(char) < 32 for char in model):
                    return self._json(400, {"error": "模型配置格式无效，请检查输入。"})
                load_dotenv(ENV_PATH, override=True)
                if not key:
                    key = os.getenv("ANTHROPIC_API_KEY", "")
                if not key or "xxx" in key.lower():
                    return self._json(400, {"error": "请填写有效的 API Key。"})
                ENV_PATH.touch(exist_ok=True)
                if raw_key.strip():
                    set_key(str(ENV_PATH), "ANTHROPIC_API_KEY", key)
                set_key(str(ENV_PATH), "MODEL_ID", model)
                base_url = str(body.get("base_url", "")).strip()
                if base_url:
                    parsed_url = urlparse(base_url)
                    if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname or parsed_url.username or parsed_url.password or any(ord(char) < 32 for char in base_url):
                        return self._json(400, {"error": "兼容 API 地址格式无效。"})
                    set_key(str(ENV_PATH), "ANTHROPIC_BASE_URL", base_url)
                else:
                    from dotenv import unset_key
                    unset_key(str(ENV_PATH), "ANTHROPIC_BASE_URL")
                os.environ["ANTHROPIC_API_KEY"] = key
                os.environ["MODEL_ID"] = model
                if base_url:
                    os.environ["ANTHROPIC_BASE_URL"] = base_url
                else:
                    os.environ.pop("ANTHROPIC_BASE_URL", None)
                return self._json(200, {"configured": True, "model": model})
            if self.path == "/api/import/feishu":
                url = _valid_feishu_doc_url(body.get("url"))
                if not url:
                    return self._json(400, {"error": "请粘贴有效的飞书 docx、wiki 或 Markdown 文件链接。"})
                path_parts = urlparse(url).path.strip("/").split("/")
                file_link = "file" in path_parts[:-1]
                try:
                    if file_link:
                        fetched = _run_feishu_cli(["markdown", "+fetch", "--file-token", path_parts[-1], "--as", "user"])
                        document = fetched.get("data", {})
                        content = document.get("content", "")
                        title = document.get("file_name", "飞书 Markdown 文件")
                    else:
                        fetched = _run_feishu_cli(["docs", "+fetch", "--as", "user", "--doc", url,
                                                   "--doc-format", "markdown"])
                        document = fetched.get("data", {}).get("document", {})
                        content = document.get("content", "")
                        title = document.get("title", "飞书文档")
                except FeishuCliError as exc:
                    return self._json(exc.status, {"error": str(exc), "auth_required": exc.auth_required})
                except (AttributeError, TypeError):
                    return self._json(502, {"error": "飞书文档返回格式无法识别。"})
                if not isinstance(content, str) or not content.strip():
                    return self._json(422, {"error": "文档没有可读取的正文，请检查内容或改用粘贴 PRD。"})
                if len(content) > 200_000:
                    return self._json(413, {"error": "文档内容超过 20 万字符，请先精简 PRD 再导入。"})
                return self._json(200, {"content": content.strip(), "title": title})
            if self.path == "/api/feishu/auth/start":
                try:
                    started = _run_feishu_cli(["auth", "login", "--domain", "docs", "--domain", "drive",
                                               "--domain", "markdown", "--no-wait", "--json"], timeout=30)
                    data = started.get("data", {})
                    verification_url = data.get("verification_uri_complete") or data.get("verification_url")
                    device_code = data.get("device_code")
                    user_code = data.get("user_code")
                    parsed_auth = urlparse(verification_url or "")
                    if (not device_code or parsed_auth.scheme != "https" or not parsed_auth.hostname
                            or parsed_auth.username or parsed_auth.password):
                        return self._json(502, {"error": "飞书未返回有效的登录地址。"})
                    cli = os.getenv("LARK_CLI_PATH") or shutil.which("lark-cli")
                    with tempfile.TemporaryDirectory(prefix="feishu-auth-") as temp_dir:
                        qr_result = subprocess.run([cli, "auth", "qrcode", verification_url,
                                                    "--output", "./auth.png"],
                                                   cwd=temp_dir, capture_output=True, text=True, timeout=15)
                        if qr_result.returncode != 0:
                            return self._json(502, {"error": "无法生成飞书登录二维码，请重试。"})
                        qr_image = base64.b64encode((Path(temp_dir) / "auth.png").read_bytes()).decode("ascii")
                    flow_id = str(uuid.uuid4())
                    with _lock:
                        _feishu_auth_flows[flow_id] = {"device_code": device_code, "created_at": time.monotonic()}
                    return self._json(200, {"flow_id": flow_id, "verification_url": verification_url,
                                            "qr_data_url": "data:image/png;base64," + qr_image,
                                            "user_code": user_code if isinstance(user_code, str) else ""})
                except FeishuCliError as exc:
                    return self._json(exc.status, {"error": str(exc)})
                except (AttributeError, TypeError, OSError, subprocess.TimeoutExpired):
                    return self._json(502, {"error": "飞书登录流程启动失败，请重试。"})
            if self.path == "/api/feishu/auth/complete":
                flow_id = body.get("flow_id")
                with _lock:
                    flow = _feishu_auth_flows.get(flow_id) if isinstance(flow_id, str) else None
                    if flow and time.monotonic() - flow["created_at"] > 600:
                        _feishu_auth_flows.pop(flow_id, None)
                        flow = None
                if not flow:
                    return self._json(410, {"error": "登录确认已过期，请重新连接飞书。"})
                try:
                    _run_feishu_cli(["auth", "login", "--device-code", flow["device_code"], "--json"], timeout=30)
                except FeishuCliError as exc:
                    return self._json(exc.status, {"error": str(exc)})
                with _lock:
                    _feishu_auth_flows.pop(flow_id, None)
                return self._json(200, {"connected": True})
            if self.path == "/api/sessions":
                brief = str(body.get("brief", "")).strip()
                entry_mode = body.get("entry_mode", "idea")
                if entry_mode not in ("idea", "prd"):
                    return self._json(400, {"error": "需求入口类型无效。"})
                session_id = str(uuid.uuid4())
                session = {"id": session_id, "title": (brief[:38] or "新建产品项目"), "brief": brief,
                           "phase": "brief", "status": "ready", "generation_scope": "fullstack",
                           "entry_mode": entry_mode, "discovery": _new_discovery(entry_mode),
                           "selected_components": [], "approvals": {}, "phase_results": {},
                           "messages": ([{"role": "user", "content": brief, "created_at": _now()}] if brief else []),
                           "error": "", "created_at": _now(), "updated_at": _now()}
                with _lock:
                    state = _read_state()
                    state["sessions"].insert(0, session)
                    _save_state(state)
                return self._json(201, {"session": session})
            match = re.fullmatch(r"/api/sessions/([a-f0-9-]+)/rename", self.path)
            if match:
                title = body.get("title")
                if not isinstance(title, str):
                    return self._json(400, {"error": "项目名称格式无效。"})
                title = title.strip()
                if not title or len(title) > 80 or any(ord(char) < 32 for char in title):
                    return self._json(400, {"error": "项目名称需为 1 至 80 个字符。"})
                with _lock:
                    state = _read_state()
                    session = _find_session(state, match.group(1))
                    if not session:
                        return self._json(404, {"error": "项目不存在。"})
                    session["title"] = title
                    session["updated_at"] = _now()
                    _save_state(state)
                    return self._json(200, {"session": _public_session(session)})
            match = re.fullmatch(r"/api/sessions/([a-f0-9-]+)/delete", self.path)
            if match:
                session_id = match.group(1)
                with _lock:
                    state = _read_state()
                    session = _find_session(state, session_id)
                    if not session:
                        return self._json(404, {"error": "项目不存在。"})
                    if session.get("status") == "running":
                        return self._json(409, {"error": "项目任务仍在运行，请等待完成后再删除。"})
                    output_path = OUTPUT_DIR / session_id
                    if output_path.is_symlink():
                        return self._json(400, {"error": "项目成果路径无效。"})
                    output_folder = output_path.resolve()
                    if output_folder.parent != OUTPUT_DIR.resolve():
                        return self._json(400, {"error": "项目成果路径无效。"})
                    if output_folder.exists():
                        shutil.rmtree(output_folder)
                    state["sessions"] = [item for item in state["sessions"] if item.get("id") != session_id]
                    _save_state(state)
                return self._json(200, {"deleted": True})
            if self.path == "/api/discovery/start":
                session_id = str(body.get("session_id", ""))
                with _lock:
                    state = _read_state()
                    session = _find_session(state, session_id)
                    if not session:
                        return self._json(404, {"error": "项目不存在。"})
                    discovery = session.setdefault("discovery", _new_discovery(session.get("entry_mode", "idea")))
                    if session["status"] == "running":
                        return self._json(409, {"error": "当前任务仍在运行。"})
                    if discovery.get("status") == "draft":
                        return self._json(409, {"error": "PRD 草稿已生成，可继续修改或确认。"})
                    _invalidate_downstream(session, "brief")
                    session["phase"], session["status"], session["error"] = "brief", "running", ""
                    session["updated_at"] = _now()
                    _save_state(state)
                    prompt = _discovery_context(session)
                operation_id = str(uuid.uuid4())
                with _lock:
                    _operations[operation_id] = {"id": operation_id, "status": "running"}
                threading.Thread(target=_run_job, args=(operation_id, session_id, "brief", prompt, False, "discovery"), daemon=True).start()
                return self._json(202, {"job_id": operation_id})
            if self.path == "/api/discovery/draft":
                session_id = str(body.get("session_id", ""))
                with _lock:
                    state = _read_state()
                    session = _find_session(state, session_id)
                    if not session:
                        return self._json(404, {"error": "项目不存在。"})
                    discovery = session.get("discovery") or _new_discovery(session.get("entry_mode", "idea"))
                    if session["status"] == "running":
                        return self._json(409, {"error": "访谈回复尚未完成，请稍后整理。"})
                    if not all(key in discovery.get("facts", {}) for key in DISCOVERY_FIELDS):
                        return self._json(409, {"error": "核心需求还未明确，请继续访谈后再整理 PRD。"})
                    text = _build_prd(discovery)
                    discovery["status"] = "draft"
                    discovery["can_draft"] = True
                    discovery["draft_version"] = int(discovery.get("draft_version", 0)) + 1
                    filename = f"prd-draft-v{discovery['draft_version']}.md"
                    artifact_dir = OUTPUT_DIR / session_id
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    (artifact_dir / filename).write_text(text, encoding="utf-8")
                    result = {"text": text, "artifacts": [filename], "phase": "brief"}
                    session["discovery"] = discovery
                    session["brief"] = text
                    session["phase_results"]["brief"] = result
                    session["phase"], session["status"], session["updated_at"] = "brief", "ready", _now()
                    _save_state(state)
                    return self._json(200, {"session": session})
            if self.path == "/api/discovery/reopen":
                session_id = str(body.get("session_id", ""))
                with _lock:
                    state = _read_state()
                    session = _find_session(state, session_id)
                    if not session:
                        return self._json(404, {"error": "项目不存在。"})
                    if session["status"] == "running":
                        return self._json(409, {"error": "当前任务仍在运行，请完成后再修改需求。"})
                    _invalidate_downstream(session, "brief")
                    discovery = session.setdefault("discovery", _new_discovery(session.get("entry_mode", "idea")))
                    discovery["status"] = "interviewing"
                    discovery["started"] = True
                    discovery["can_draft"] = all(key in discovery.get("facts", {}) for key in DISCOVERY_FIELDS)
                    session["phase"], session["status"], session["error"] = "brief", "ready", ""
                    session["updated_at"] = _now()
                    _save_state(state)
                    return self._json(200, {"session": session})
            if self.path == "/api/chat":
                session_id = str(body.get("session_id", ""))
                message = str(body.get("message", "")).strip()
                if not message:
                    return self._json(400, {"error": "请输入消息。"})
                with _lock:
                    state = _read_state()
                    session = _find_session(state, session_id)
                    if not session:
                        return self._json(404, {"error": "项目不存在。"})
                    if session["status"] == "running":
                        return self._json(409, {"error": "当前任务仍在运行，请等待完成。"})
                    is_discovery = session.get("phase") == "brief"
                    if is_discovery:
                        _invalidate_downstream(session, "brief")
                        discovery = session.setdefault("discovery", _new_discovery(session.get("entry_mode", "idea")))
                        discovery["status"] = "interviewing"
                        session["phase"] = "brief"
                    session["messages"].append({"role": "user", "content": message, "created_at": _now()})
                    if not is_discovery:
                        session["brief"] = (session.get("brief", "") + "\n" + message).strip()
                    session["status"] = "running"
                    session["error"] = ""
                    session["updated_at"] = _now()
                    _save_state(state)
                    phase = session["phase"]
                    task_mode = "discovery" if is_discovery else "phase"
                    prompt = _discovery_context(session) if is_discovery else (
                        _phase_context(session, phase) + "\n\n" +
                        "\n".join(f"{m['role']}: {m['content']}" for m in session["messages"][-30:]))
                operation_id = str(uuid.uuid4())
                with _lock:
                    _operations[operation_id] = {"id": operation_id, "status": "running"}
                threading.Thread(target=_run_job, args=(operation_id, session_id, phase, prompt, False, task_mode), daemon=True).start()
                return self._json(202, {"job_id": operation_id})
            if self.path == "/api/continue":
                session_id = str(body.get("session_id", ""))
                with _lock:
                    state = _read_state()
                    session = _find_session(state, session_id)
                    if not session:
                        return self._json(404, {"error": "项目不存在。"})
                    if session["status"] == "running":
                        return self._json(409, {"error": "当前已有任务运行中。"})
                    messages = session.get("messages", [])
                    if session["phase"] == "brief" or not messages or messages[-1].get("role") != "assistant":
                        return self._json(409, {"error": "当前没有可续写的阶段回复。"})
                    partial = messages[-1].get("content", "")
                    last_user = next((m.get("content", "") for m in reversed(messages[:-1]) if m.get("role") == "user"), "")
                    phase = session["phase"]
                    session["status"], session["error"] = "running", ""
                    _save_state(state)
                operation_id = str(uuid.uuid4())
                with _lock:
                    _operations[operation_id] = {"id": operation_id, "status": "running"}
                threading.Thread(target=_continue_job, args=(operation_id, session_id, phase, partial, last_user), daemon=True).start()
                return self._json(202, {"job_id": operation_id})
            if self.path == "/api/run":
                session_id, phase = str(body.get("session_id", "")), str(body.get("phase", ""))
                if phase not in PHASES:
                    return self._json(400, {"error": "未知的工作阶段。"})
                if phase == "brief":
                    return self._json(409, {"error": "需求阶段请通过访谈收集信息，再整理 PRD 草稿。"})
                with _lock:
                    state = _read_state()
                    session = _find_session(state, session_id)
                    if not session:
                        return self._json(404, {"error": "项目不存在。"})
                    gates = {"components": "brief", "architecture": "components", "development": "architecture", "ui_design": "development", "frontend": "ui_design", "backend": "development", "verification": "development"}
                    gate = gates.get(phase)
                    if gate and not session["approvals"].get(gate):
                        return self._json(409, {"error": f"请先确认「{PHASE_LABELS[gate]}」阶段。"})
                    if phase == "verification":
                        required = (["frontend", "backend"] if session["generation_scope"] == "fullstack"
                                    else [session["generation_scope"]])
                        missing = [part for part in required if not session["phase_results"].get(part)]
                        if missing:
                            names = "、".join("前端" if p == "frontend" else "后端" for p in missing)
                            return self._json(409, {"error": f"请先完成并验证{names}开发。"})
                    if phase in ("frontend", "backend") and session["generation_scope"] not in ("fullstack", phase):
                        return self._json(409, {"error": "当前生成范围不包含该部分。"})
                    if session["status"] == "running":
                        return self._json(409, {"error": "当前已有任务运行中。"})
                    _invalidate_downstream(session, phase)
                    session["phase"], session["status"], session["error"] = phase, "running", ""
                    session["updated_at"] = _now()
                    _save_state(state)
                    context = _phase_context(session, phase) + "\n请直接完成本阶段并给出可审阅产物。"
                operation_id = str(uuid.uuid4())
                with _lock:
                    _operations[operation_id] = {"id": operation_id, "status": "running"}
                threading.Thread(target=_run_job, args=(operation_id, session_id, phase, context), daemon=True).start()
                return self._json(202, {"job_id": operation_id})
            if self.path == "/api/review":
                session_id, phase = str(body.get("session_id", "")), str(body.get("phase", ""))
                if phase not in ("brief", "components", "architecture", "development", "ui_design"):
                    return self._json(400, {"error": "该阶段不支持确认。"})
                with _lock:
                    state = _read_state()
                    session = _find_session(state, session_id)
                    if not session:
                        return self._json(404, {"error": "项目不存在。"})
                    if session["status"] == "running":
                        return self._json(409, {"error": "当前阶段正在运行，完成后再确认。"})
                    prerequisites = {"components": "brief", "architecture": "components", "development": "architecture", "ui_design": "development"}
                    prerequisite = prerequisites.get(phase)
                    if prerequisite and not session["approvals"].get(prerequisite):
                        return self._json(409, {"error": f"请先确认「{PHASE_LABELS[prerequisite]}」阶段。"})
                    if not session["phase_results"].get(phase):
                        return self._json(409, {"error": "请先生成并查看当前阶段成果，再确认。"})
                    if phase == "brief":
                        discovery = session.get("discovery", {})
                        if discovery.get("status") != "draft" or not all(
                                key in discovery.get("facts", {}) for key in DISCOVERY_FIELDS):
                            return self._json(409, {"error": "请完成需求访谈并整理 PRD 草稿后再确认。"})
                        discovery["status"] = "confirmed"
                    if phase == "components":
                        available = {key for key, _label, _description in COMPONENTS}
                        requested = body.get("selected_components", [])
                        if not isinstance(requested, list):
                            return self._json(400, {"error": "组件选择格式无效。"})
                        selected = [x for x in requested if isinstance(x, str) and x in available]
                        if selected != session.get("selected_components", []):
                            current_result = session["phase_results"].get(phase)
                            _invalidate_downstream(session, phase)
                            session["phase_results"][phase] = current_result
                        session["selected_components"] = selected
                    if phase == "development":
                        scope = body.get("generation_scope", "fullstack")
                        if scope not in ("frontend", "backend", "fullstack"):
                            return self._json(400, {"error": "生成范围无效。"})
                        if scope != session.get("generation_scope", "fullstack"):
                            current_result = session["phase_results"].get(phase)
                            _invalidate_downstream(session, phase)
                            session["phase_results"][phase] = current_result
                        session["generation_scope"] = scope
                    session["approvals"][phase] = True
                    next_phase = {"brief": "components", "components": "architecture", "architecture": "development", "development": ("backend" if session.get("generation_scope") == "backend" else "ui_design"), "ui_design": "frontend"}[phase]
                    session["phase"], session["updated_at"] = next_phase, _now()
                    _save_state(state)
                    return self._json(200, {"session": session})
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "接口不存在。"})


if __name__ == "__main__":
    if HOST != "127.0.0.1":
        raise RuntimeError("本地工作台必须绑定到 127.0.0.1。")
    _recover_interrupted_sessions()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Nexus 工作台已启动：http://{HOST}:{PORT}")
    server.serve_forever()
