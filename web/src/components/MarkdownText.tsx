// MarkdownText：极简 Markdown 行内渲染（零依赖）。
// 只处理模型回复里最常见的 **加粗**、*斜体*、`行内代码`，其余原样输出。
// 拆成 React 节点而不是 dangerouslySetInnerHTML，天然防注入。

import type { ReactNode } from "react";

const INLINE_PATTERN = /(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|`[^`]+`)/g;

export default function MarkdownText({ text }: { text: string }) {
  return <>{renderInline(text)}</>;
}

function renderInline(text: string): ReactNode[] {
  return text
    .split(INLINE_PATTERN)
    .filter((p) => p !== "")
    .map((part, i) => {
      if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
        return <strong key={i}>{part.slice(2, -2)}</strong>;
      }
      if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
        return (
          <code
            key={i}
            className="px-1 py-0.5 rounded bg-black/30 text-[0.85em] font-mono"
          >
            {part.slice(1, -1)}
          </code>
        );
      }
      if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
        return <em key={i}>{part.slice(1, -1)}</em>;
      }
      return <span key={i}>{part}</span>;
    });
}
