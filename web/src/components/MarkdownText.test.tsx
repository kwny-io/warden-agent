// MarkdownText.tsx —— 极简行内 Markdown 渲染。
// 关键风险：模型输出里可能混入 HTML/脚本，这里必须当**纯文本**渲染，不能变成元素。
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import MarkdownText from "./MarkdownText";

describe("MarkdownText", () => {
  it("**加粗** 渲染成 strong", () => {
    const { container } = render(<MarkdownText text="这是**重点**内容" />);
    const strong = container.querySelector("strong");
    expect(strong).not.toBeNull();
    expect(strong?.textContent).toBe("重点");
  });

  it("`行内代码` 渲染成 code", () => {
    const { container } = render(<MarkdownText text="执行 `npm run build` 即可" />);
    expect(container.querySelector("code")?.textContent).toBe("npm run build");
  });

  it("*斜体* 渲染成 em，且不与加粗冲突", () => {
    const { container } = render(
      <MarkdownText text={"*斜* 与 **粗** 并存"} />,
    );
    expect(container.querySelector("em")?.textContent).toBe("斜");
    expect(container.querySelector("strong")?.textContent).toBe("粗");
  });

  it("模型输出里的 HTML/脚本被当纯文本，不产生真实元素", () => {
    const payload = '<img src=x onerror=alert(1)><script>alert(2)</script>';
    const { container } = render(<MarkdownText text={payload} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toBe(payload);
  });

  it("空字符串不崩、不产生节点", () => {
    const { container } = render(<MarkdownText text="" />);
    expect(container.textContent).toBe("");
  });

  it("未闭合的标记按普通文本处理，不会被吃掉", () => {
    const { container } = render(<MarkdownText text="半个**加粗" />);
    expect(container.querySelector("strong")).toBeNull();
    expect(container.textContent).toBe("半个**加粗");
  });
});
