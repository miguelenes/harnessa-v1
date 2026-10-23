import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SafeMarkdown } from "../components/safe-markdown";

describe("safe Markdown", () => {
  afterEach(cleanup);

  it("keeps basic Markdown while sanitizing executable HTML and unsafe links", () => {
    render(<SafeMarkdown>{"**safe text** <img src=x onerror=alert(1)> <script>alert(2)</script> [unsafe](javascript:alert(3))"}</SafeMarkdown>);
    expect(screen.getByText("safe text").tagName).toBe("STRONG");
    expect(document.querySelector("script")).toBeNull();
    expect(document.querySelector("img")?.getAttribute("onerror") ?? null).toBeNull();
    expect(screen.getByText("unsafe").closest("a")?.getAttribute("href") ?? null).toBeNull();
  });
});
