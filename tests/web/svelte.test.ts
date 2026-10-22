import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it } from "vitest";
import CloudPortal from "../../apps/cloud-portal/src/routes/+page.svelte";
import DesignSystemFixture from "./svelte-components.svelte";

describe("Svelte application surfaces", () => {
  afterEach(cleanup);

  it("renders shared Svelte controls with their accessible semantics", () => {
    render(DesignSystemFixture);
    expect(screen.getByRole("alert").textContent).toBe("Unavailable");
    expect(screen.getByText("READY").dataset.slot).toBe("badge");
    expect(screen.getByRole("button", { name: "Save" }).dataset.slot).toBe("button");
    expect(screen.getByRole("heading", { name: "Workspace" }).dataset.slot).toBe("card-title");
    expect(screen.getByText("Repository scope").dataset.slot).toBe("card-description");
    expect(screen.getByLabelText("Workspace name").getAttribute("type")).toBe("text");
    expect(screen.getByText("Review required").dataset.slot).toBe("card-footer");
    expect(screen.getByRole("link", { name: "Projects" }).getAttribute("href")).toBe("/projects");
  });

  it("renders the static cloud portal preview and its links", () => {
    render(CloudPortal);
    expect(screen.getByRole("heading", { name: /cloud/i })).toBeTruthy();
    expect(screen.getByText("Harnessa Cloud · Static preview")).toBeTruthy();
    expect(
      screen
        .getAllByRole("link", { name: /github/i })
        .some((link) => {
          const href = link.getAttribute("href");
          return href !== null && new URL(href).hostname === "github.com";
        }),
    ).toBe(true);
  });
});
