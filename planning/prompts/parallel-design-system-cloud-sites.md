# Parallel workstream — Harnessa design system, marketing site, and cloud portal foundation

This is a standalone parallel workstream. It can run alongside numbered backend milestones because it does not change API contracts, backend services, persistence, or numbered milestone ordering. It is not milestone 18 and must not be archived into the numbered sequence.

## Isolation and ownership

1. Inspect the current repository, `AGENTS.md`, active worktrees, lockfiles, package-manager version, planning index, architecture decisions, and milestone reports before editing.
2. Use an isolated worktree at the current repository base. Do not copy, stash, reset, or overwrite the user's uncommitted checkout. Record the base SHA in the report and merge only the workstream's reviewed files.
3. A single integration owner owns `pnpm-workspace.yaml`, the root `package.json`, and the root `pnpm-lock.yaml`. Other contributors own disjoint package/app/documentation paths and must not edit root workspace or lockfiles. Coordinate exact paths and the shared design token contract before parallel implementation.
4. Keep this workstream scoped to shared UI foundations, marketing, and a static portal shell. Do not edit API/OpenAPI/database contracts or alter `apps/web` routes/API integration.
5. Create a new private Google Stitch project for Harnessa. Do not reuse an unrelated Stitch project. Store `docs/design-system.md` in the repository as the token and component authority, upload it to Stitch, create/apply the Stitch design system, and use the generated screen as visual direction. Record the project and design-system IDs in the report.

## Product and hosting boundary

Create a polished public marketing site at `https://harnessa.cloud` and a separate static cloud portal shell at `https://app.harnessa.cloud`. Deploy each as its own Cloudflare Pages project in the user-selected `swarmnet` account. The marketing site links to the portal; the portal links back to the marketing site. Keep the applications separate: Astro may render standalone Svelte components, but it must not embed or replace the SvelteKit application.

The cloud offering is not defined yet. The portal may show product navigation, account/setup framing, documentation links, and honest unavailable/setup states only. Do not add authentication, customer data, billing, cloud-resource actions, API calls, or invented availability/usage metrics. Marketing copy must distinguish planned product direction from currently available functionality.

Inspect Pages project names and all DNS records for both requested hostnames before deployment. Preserve unrelated DNS records, especially email records. Do not replace an existing site record without checking what it serves and recording the intended change. If a record is required for Pages ownership, register the hostname with its Pages project, then point it to `<project>.pages.dev` with a CNAME; use DNS-only if a proxied custom-domain request fails, and preserve unrelated MX/TXT records at an apex. Make only the target-host change after successful static builds and a reviewed deployment plan. Record the `pages.dev` preview URL and the custom-domain result independently.

## Parallel implementation lanes

Contributors may work concurrently only after token names, package contracts, and ownership are agreed:

- **Design tokens and component packages:** publish framework-neutral CSS tokens and equivalent React `shadcn/ui`-style and Svelte `shadcn-svelte` component entry points. Use semantic CSS roles and matching names/variants/states across frameworks. Keep package dependencies framework-appropriate and expose typed entry points. Include accessible Button, Badge, Card, Input, Alert, and any required primitives. Keep a React package that the existing Next.js app can adopt during prompt 8.
- **Astro marketing app:** create a static Astro site, include the shared tokens and Svelte integration for optional standalone components, tell a truthful Harnessa product story, and link to `app.harnessa.cloud`.
- **SvelteKit portal app:** create a separate SvelteKit app with adapter-static, a designed portal shell, no customer or cloud-resource data, and explicit setup/unavailable language.
- **Integration owner:** owns root workspace/lockfiles, top-level scripts, final package integration, both Cloudflare Pages projects/deployments, and the consolidated workstream report.

All contributors preserve other worktree changes and coordinate before touching a path outside their ownership. Do not add a dependency to the root unless the integration owner agrees and updates the lockfile.

## Planning updates

- Add `docs/design-system.md` describing product character, logo/typography, token roles, component API/variants, accessibility, and application guidance.
- Add this standalone prompt at `planning/prompts/parallel-design-system-cloud-sites.md` and identify it in the planning index under a separate “Parallel workstreams” heading/table, with explicit non-milestone status.
- Add the architecture decision for two independently deployed static sites, the shared framework-neutral token contract, React/Svelte entry points, the deferred cloud portal scope, and Cloudflare domain ownership.
- Update prompt 8 to use the React package when its UI adoption work begins; do not start prompt 8 implementation here.
- Add a short follow-up to `docs/milestones/01.md`: its original reference to `docs/design-system.md` had no matching artifact in the checkout at that time; this workstream supplies the shared design system and packages for later adoption. Preserve the original milestone 1 evidence.
- Write a non-numbered `docs/milestones/parallel-web-foundation.md` report with base SHA, exact framework/package/tool versions, implementations, validation and browser evidence, deployment IDs/URLs, remaining setup, and next steps.
- Leave milestones 2–4 and their reports unchanged. Leave prompt 13's self-hosted release requirements and scope unchanged.

## Acceptance

- `docs/design-system.md` is the source of truth and corresponds to the new Harnessa Stitch system.
- Token package exports stable light/dark semantic tokens; React and Svelte packages provide equivalent typed, keyboard-accessible components with consistent visual states.
- Existing Next.js routes, backend calls, and OpenAPI/database contracts are unchanged.
- Astro and SvelteKit are separate builds with static output suitable for Cloudflare Pages.
- Both applications render at 360px/mobile and desktop widths, work by keyboard, expose visible focus, preserve accessible names/status text, respect reduced motion, and maintain theme consistency.
- Run the package typechecks/builds and static builds, then inspect both sites with Browser Harness. Capture the viewport and accessibility evidence in the report.
- Deploy separate Pages projects and smoke-check their `pages.dev` deployments. Attach each approved custom hostname where DNS ownership and provider configuration allow. Never claim a custom hostname is live until its Pages domain status and HTTPS response confirm it.
- Record exact command output/results, package versions, Pages project/deployment IDs, URLs, browser viewport/keyboard checks, DNS changes, and remaining setup. If cloud portal product decisions are still unavailable, keep those features visibly unavailable and document what is needed next.
