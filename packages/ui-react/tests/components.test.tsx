import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
  Input,
  cn,
} from "../src/index.js";

describe("React design system components", () => {
  it("renders card content and input semantics while merging utility classes", () => {
    render(
      <Card aria-label="Project card">
        <CardHeader>
          <CardTitle>Project alpha</CardTitle>
          <CardDescription>Scoped workspace</CardDescription>
        </CardHeader>
        <CardContent>
          <Input aria-label="Project name" placeholder="Name" className="px-6" />
        </CardContent>
        <CardFooter>Ready</CardFooter>
      </Card>,
    );

    expect(screen.getByRole("region", { name: "Project card" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Project alpha" }).dataset.slot).toBe("card-title");
    expect(screen.getByText("Scoped workspace").dataset.slot).toBe("card-description");
    expect(screen.getByLabelText("Project name").getAttribute("class")).toContain("px-6");
    expect(screen.getByText("Ready").dataset.slot).toBe("card-footer");
    expect(cn("px-3", "px-6", "text-sm")).toBe("px-6 text-sm");
  });

  it("sets alert roles and variant styling", () => {
    const { rerender } = render(
      <Alert>
        <AlertTitle>Saved</AlertTitle>
        <AlertDescription>Changes are ready.</AlertDescription>
      </Alert>,
    );
    expect(screen.getByRole("status").dataset.slot).toBe("alert");
    expect(screen.getByText("Saved").dataset.slot).toBe("alert-title");
    expect(screen.getByText("Changes are ready.").dataset.slot).toBe("alert-description");

    rerender(<Alert variant="destructive">Cannot save</Alert>);
    expect(screen.getByRole("alert").textContent).toBe("Cannot save");
  });

  it("supports button variants, disabled state, and composition", () => {
    const onClick = vi.fn();
    const { rerender } = render(
      <Button variant="outline" size="sm" disabled onClick={onClick}>
        Save
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Save" });
    expect(button.dataset.slot).toBe("button");
    expect(button.hasAttribute("disabled")).toBe(true);
    fireEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();

    rerender(
      <Button asChild variant="link">
        <a href="/projects">Projects</a>
      </Button>,
    );
    expect(screen.getByRole("link", { name: "Projects" }).getAttribute("href")).toBe("/projects");
  });

  it("renders badge variants and forwards input type", () => {
    render(
      <>
        <Badge variant="success">READY</Badge>
        <Input type="email" aria-label="Email" />
      </>,
    );
    expect(screen.getByText("READY").dataset.slot).toBe("badge");
    expect(screen.getByLabelText("Email").getAttribute("type")).toBe("email");
  });
});
