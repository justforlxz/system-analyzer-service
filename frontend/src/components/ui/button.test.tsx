import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Button } from "@/components/ui/button";

describe("Button", () => {
  it("渲染传入的文字", () => {
    render(<Button>开始采集</Button>);

    expect(screen.getByRole("button", { name: "开始采集" })).toBeInTheDocument();
  });

  it("点击时调用处理函数", async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>导入</Button>);

    await userEvent.click(screen.getByRole("button", { name: "导入" }));

    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("默认样式与自定义类名合并", () => {
    render(<Button className="custom-class">确认</Button>);
    const button = screen.getByRole("button", { name: "确认" });

    expect(button).toHaveClass("bg-slate-900");
    expect(button).toHaveClass("custom-class");
  });
});
