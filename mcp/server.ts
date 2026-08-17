import { readFileSync } from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod/v3";

const VERSION = "0.2.0";
const TEMPLATE_URI = "ui://tune-task-context/context-control-v1.html";
const currentFile = fileURLToPath(import.meta.url);
const pluginRoot = path.resolve(path.dirname(currentFile), "..");
const launcherPath = path.join(pluginRoot, "scripts", "context_launcher.py");
const widgetHtml = readFileSync(path.join(pluginRoot, "ui", "context-control.html"), "utf8");

type JsonRecord = Record<string, unknown>;

const taskInputSchema = {
  project_path: z.string().min(1).describe("Absolute path to the current workspace or project root."),
  task_description: z.string().optional().default("").describe("The task the next Codex run will perform."),
  is_resume: z.boolean().optional().default(false).describe("Whether the next run resumes an old task."),
};

const planOutputSchema = {
  model: z.string(),
  task_class: z.enum(["compact", "standard", "large", "maximum"]),
  source: z.string(),
  requested_tokens: z.number().int(),
  effective_tokens: z.number().int(),
  compact_at_tokens: z.number().int(),
  model_default_tokens: z.number().int(),
  model_max_tokens: z.number().int(),
  effective_percent: z.number().int(),
  repo_files_seen: z.number().int(),
  score: z.number().int(),
  reasons: z.array(z.string()),
  notes: z.array(z.string()),
  summary: z.string(),
  project_path: z.string(),
  applied_to: z.string().optional(),
  applied_scope: z.enum(["project", "user"]).optional(),
  applies_to: z.string().optional(),
};

function asInteger(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : fallback;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function summarize(taskClass: string, fileCount: number): string {
  const classLabels: Record<string, string> = {
    compact: "任务较轻，优先保持响应灵活",
    standard: "常规项目工作量，使用模型默认窗口",
    large: "多文件或长任务，建议扩大上下文",
    maximum: "仓库级或超长任务，建议使用模型上限",
  };
  const base = classLabels[taskClass] ?? "已按当前任务规模完成分析";
  return `${base} · 已统计 ${fileCount.toLocaleString("zh-CN")} 个项目文件`;
}

function sanitizePlan(raw: JsonRecord, projectPath: string): JsonRecord {
  const taskClass = typeof raw.task_class === "string" ? raw.task_class : "standard";
  const fileCount = asInteger(raw.repo_files_seen);
  return {
    model: String(raw.model ?? "unknown"),
    task_class: taskClass,
    source: String(raw.source ?? "auto:standard"),
    requested_tokens: asInteger(raw.requested_tokens),
    effective_tokens: asInteger(raw.effective_tokens),
    compact_at_tokens: asInteger(raw.compact_at_tokens),
    model_default_tokens: asInteger(raw.model_default_tokens),
    model_max_tokens: asInteger(raw.model_max_tokens),
    effective_percent: asInteger(raw.effective_percent, 95),
    repo_files_seen: fileCount,
    score: asInteger(raw.score),
    reasons: asStringArray(raw.reasons),
    notes: asStringArray(raw.notes),
    summary: summarize(taskClass, fileCount),
    project_path: projectPath,
    ...(typeof raw.applied_to === "string" ? { applied_to: raw.applied_to } : {}),
    ...(raw.applied_scope === "project" || raw.applied_scope === "user"
      ? { applied_scope: raw.applied_scope }
      : {}),
    ...(typeof raw.applies_to === "string" ? { applies_to: raw.applies_to } : {}),
  };
}

async function runLauncher(options: {
  projectPath: string;
  taskDescription: string;
  isResume: boolean;
  context: "auto" | string;
  applyScope?: "project" | "user";
}): Promise<JsonRecord> {
  const projectPath = path.resolve(options.projectPath);
  const args = [launcherPath, "--cwd", projectPath, "--context", options.context];
  if (options.isResume) args.push("--resume", "last");
  if (options.applyScope) args.push("--apply-config", options.applyScope);
  else args.push("--dry-run");
  if (options.taskDescription.trim()) args.push("--", options.taskDescription.trim());

  const python = process.env.PYTHON || "python3";
  const raw = await new Promise<string>((resolve, reject) => {
    const child = spawn(python, args, {
      cwd: projectPath,
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timeout = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error("上下文分析超时"));
    }, 20_000);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      if (stdout.length > 2_000_000) child.kill("SIGTERM");
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timeout);
      if (code === 0) resolve(stdout);
      else reject(new Error(stderr.trim() || `context launcher exited with code ${code}`));
    });
  });

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("上下文分析返回了无效数据");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("上下文分析没有返回计划对象");
  }
  return sanitizePlan(parsed as JsonRecord, projectPath);
}

function resultFor(plan: JsonRecord, action: "analyzed" | "shown" | "applied") {
  const requested = asInteger(plan.requested_tokens).toLocaleString("en-US");
  const effective = asInteger(plan.effective_tokens).toLocaleString("en-US");
  const verbs = {
    analyzed: "已分析",
    shown: "已打开上下文设置",
    applied: "已应用到下一次新建或恢复的任务",
  };
  return {
    structuredContent: plan,
    content: [
      {
        type: "text" as const,
        text: `${verbs[action]}：请求 ${requested} tokens，可用约 ${effective} tokens。当前任务未被热切换。`,
      },
    ],
  };
}

const server = new McpServer(
  { name: "tune-task-context", version: VERSION },
  { capabilities: { resources: {}, tools: {} } },
);

server.registerResource("context-control", TEMPLATE_URI, {}, async () => ({
  contents: [
    {
      uri: TEMPLATE_URI,
      mimeType: "text/html;profile=mcp-app",
      text: widgetHtml,
      _meta: { ui: { prefersBorder: true } },
    },
  ],
}));

server.registerTool(
  "analyze_context",
  {
    title: "分析项目上下文",
    description:
      "只读取任务规模信号和实时模型目录，估算下一次 Codex 任务需要的上下文窗口；不会写配置。",
    inputSchema: taskInputSchema,
    outputSchema: planOutputSchema,
    annotations: { readOnlyHint: true, openWorldHint: false, destructiveHint: false },
    _meta: {
      "openai/toolInvocation/invoking": "正在分析项目…",
      "openai/toolInvocation/invoked": "项目分析完成",
    },
  },
  async ({ project_path, task_description, is_resume }) => {
    const plan = await runLauncher({
      projectPath: project_path,
      taskDescription: task_description,
      isResume: is_resume,
      context: "auto",
    });
    return resultFor(plan, "analyzed");
  },
);

server.registerTool(
  "show_context_control",
  {
    title: "打开上下文设置",
    description:
      "打开“上下文”内嵌设置卡片。先分析当前项目，再让用户选择自动模式、预设滑杆或精确 token 值。",
    inputSchema: taskInputSchema,
    outputSchema: planOutputSchema,
    annotations: { readOnlyHint: true, openWorldHint: false, destructiveHint: false },
    _meta: {
      ui: { resourceUri: TEMPLATE_URI },
      "openai/outputTemplate": TEMPLATE_URI,
      "openai/widgetAccessible": true,
      "openai/toolInvocation/invoking": "正在打开上下文设置…",
      "openai/toolInvocation/invoked": "上下文设置已打开",
    },
  },
  async ({ project_path, task_description, is_resume }) => {
    const plan = await runLauncher({
      projectPath: project_path,
      taskDescription: task_description,
      isResume: is_resume,
      context: "auto",
    });
    return resultFor(plan, "shown");
  },
);

server.registerTool(
  "apply_context",
  {
    title: "应用上下文设置",
    description:
      "在用户明确确认后，仅更新项目或用户配置中的两个上下文键。只对下一次新建或恢复的任务生效。",
    inputSchema: {
      ...taskInputSchema,
      mode: z.enum(["auto", "manual"]),
      context_tokens: z.number().int().min(32_000).max(1_000_000).optional(),
      scope: z.enum(["project", "user"]).optional().default("project"),
      confirmed: z.boolean().describe("Must be true only after the user presses Apply or explicitly confirms."),
    },
    outputSchema: planOutputSchema,
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: false, destructiveHint: false },
    _meta: {
      "openai/toolInvocation/invoking": "正在应用上下文设置…",
      "openai/toolInvocation/invoked": "上下文设置已应用",
    },
  },
  async ({
    project_path,
    task_description,
    is_resume,
    mode,
    context_tokens,
    scope,
    confirmed,
  }) => {
    if (!confirmed) throw new Error("需要用户明确确认后才能写入上下文设置");
    if (mode === "manual" && context_tokens === undefined) {
      throw new Error("详情模式必须提供精确的 context_tokens");
    }
    const context = mode === "auto" ? "auto" : `${context_tokens}`;
    const plan = await runLauncher({
      projectPath: project_path,
      taskDescription: task_description,
      isResume: is_resume,
      context,
      applyScope: scope,
    });
    return resultFor(plan, "applied");
  },
);

const transport = new StdioServerTransport();
await server.connect(transport);
