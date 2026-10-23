// 语义替换层：把后端的技术标识翻译成人话。
//
// 为什么要有这一层：界面上原本直接显示 `git.apply_patch`、`WAITING_APPROVAL`、
// `run-mvjzgmos` 这类标识——技术人员一眼就懂，但把产品交给不写代码的人用时，
// 他们看到的是「一串看不懂的英文字母」。这不是美观问题，是**可理解性**问题：
// 看不懂工具名，就没法判断这条审批该不该批。
//
// 设计取舍：
//   · 纯函数、无 React 依赖 —— 可以在任何地方复用，也能单测。
//   · 未知名字**原样返回**，不编造。宁可显示 `foo.bar`，也不能猜成「未知操作」——
//     猜错比不猜更危险（用户会以为自己看懂了）。
//   · 原始标识一律保留在 `title` / 可切换的原始视图里，技术用户随手能看回原文。

/** 工具名 → 中文短标签。键名与后端注册的工具名逐字对齐（见 tool/catalog.py）。 */
const TOOL_LABELS: Record<string, string> = {
  // 执行 / 代码
  "shell.run": "执行命令",
  "code.run": "运行代码",
  "code.read": "读取代码",
  "code.list": "列出代码文件",
  "git.apply_patch": "应用代码补丁",
  "git.read": "查看代码仓库",
  // 文件
  "fs.write": "写入文件",
  "fs.read": "读取文件",
  "fs.delete": "删除文件",
  "fs.move": "移动文件",
  // 记忆
  "memory.remember": "记住信息",
  "memory.remember_text": "记住信息",
  "memory.confirm": "确认记忆",
  "memory.recall": "回忆记忆",
  // 联网 / 知识
  "web.search": "联网搜索",
  "web.fetch": "抓取网页",
  "knowledge.search": "检索知识库",
  // 机器人
  "robot.state": "读取机器人状态",
  "robot.move": "移动机器人",
  "robot.stop": "停止机器人",
  "robot.reset": "复位机器人",
  // 其他
  "weather.get": "查询天气",
};

/** Run 状态（RunStatus 枚举名）→ 中文。 */
const STATUS_LABELS: Record<string, string> = {
  PENDING: "待开始",
  QUEUED: "排队中",
  RUNNING: "执行中",
  SUSPENDING: "暂停中",
  SUSPENDED: "已暂停",
  WAITING_INTERACTION: "等待回复",
  WAITING_APPROVAL: "等待审批",
  COMPLETING: "收尾中",
  COMPLETED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
  TIMED_OUT: "已超时",
};

/**
 * 工具名 → 中文短标签；没登记的名字**原样返回**。
 *
 * `null` / 空串返回空串：界面上「没有工具名」不该显示成一串空白或占位符。
 */
export function toolLabel(name: string | null | undefined): string {
  if (!name) return "";
  return TOOL_LABELS[name] ?? name;
}

/** 该工具名是否有语义替换（用来决定「切换原始标识」的按钮要不要显示）。 */
export function hasToolLabel(name: string | null | undefined): boolean {
  return !!name && name in TOOL_LABELS;
}

/** Run 状态 → 中文；未知状态原样返回（后端加了新状态时界面不会变成空白）。 */
export function statusLabel(status: string | null | undefined): string {
  if (!status) return "";
  return STATUS_LABELS[status] ?? status;
}

/**
 * 会话的展示名：优先用对话标题，没有标题才退回 run_id。
 *
 * 为什么不是反过来：`run-mvjzgmos` 对用户没有信息量，而标题（首条消息摘要）
 * 是他自己写的那句话。用户找「昨天问过的那件事」时认的是标题，不是 ID。
 */
export function runDisplayName(runId: string, title?: string | null): string {
  const t = (title ?? "").trim();
  return t || runId;
}

/** ISO 时间 → 本地「MM-DD HH:mm」；无/非法时间返回空串（老数据不编造时间）。 */
export function fmtTime(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
