# Harnessa Design System

**Status:** shared foundation used by the marketing site, static cloud portal shell, and the Next.js Harnessa workspace.

## Product character

Harnessa is an engineering workbench for governed, observable agent workflows. Its interface should feel dependable, deliberate, and technically literate: quiet neutrals, crisp typography, purposeful indigo, and small green signals reserved for verified or ready states. Avoid generic sci-fi art, glowing gradients, fabricated metrics, and decorative dashboard clutter.

## Brand and visual language

- **Wordmark:** Harnessa, paired with a native SVG mark built from a simple bracket-and-path motif. Keep the mark readable at small sizes and never bake text into the artwork.
- **Palette:** deep ink `#111820`, soft paper `#F5F6F4`, white surface `#FFFFFF`, primary indigo `#5146E5`, indigo wash `#ECEBFF`, teal `#0C766E`, verified green `#137A54`, amber `#96600C`, and red `#B53636`.
- **Typography:** Geist for interface and headings, Geist Mono for code, IDs, and compact metadata. Use system fallbacks; pages must remain legible if remote fonts are unavailable.
- **Shape:** 10px default corners, 16px for prominent cards, pill shape only for compact labels and segmented controls.
- **Elevation:** use quiet 1px borders and restrained shadows. Stronger contrast comes from hierarchy and spacing, not stacked shadows.
- **Iconography:** simple 1.75px stroke SVG line icons with consistent optical size. Use Lucide-compatible geometry, never emoji as controls.

## Tokens

Framework-neutral tokens live in `packages/design-tokens/src/tokens.css`. Both light and dark semantic palettes are required. Components consume semantic roles instead of hard-coded product colors.

| Role | Light | Dark |
| --- | --- | --- |
| Background | `#F5F6F4` | `#10151B` |
| Foreground | `#111820` | `#F3F5F7` |
| Surface | `#FFFFFF` | `#171E26` |
| Muted surface | `#ECEFED` | `#202933` |
| Muted text | `#5E6872` | `#A9B3BD` |
| Border | `#D9DEE0` | `#303B47` |
| Primary | `#5146E5` | `#A8A2FF` |
| Primary foreground | `#FFFFFF` | `#17143B` |
| Accent | `#0C766E` | `#63C8BC` |
| Success | `#137A54` | `#64D5A2` |
| Warning | `#96600C` | `#F4C56C` |
| Danger | `#B53636` | `#FF8A8A` |

Spacing uses a 4px base: `1` = 4px, `2` = 8px, `3` = 12px, `4` = 16px, `6` = 24px, `8` = 32px, `12` = 48px, `16` = 64px, `24` = 96px. Content width is 1160px; readable prose is 68ch. The focus indicator is a 2px primary ring with a 2px offset.

## Component contract

Framework-native components are published from `@harnessa/ui-react` and `@harnessa/ui-svelte`; both consume `@harnessa/design-tokens` and keep equivalent names, variants, semantic colors, and accessibility behavior.

Initial primitives: Button, Badge, Card, Input, Textarea, Alert, Separator, and VisuallyHidden. Interactive APIs remain intentionally small. Native buttons, labels, and form controls are preferred where no complex keyboard behavior is required. The packages do not introduce a second API, auth, or data layer.

Buttons have `primary`, `secondary`, `outline`, `ghost`, and `destructive` variants with `default`, `sm`, `lg`, and `icon` sizes. Inputs need visible labels and descriptions at the call site. Status badges must include readable text, not color alone. Disabled and unavailable states remain distinct from loading states.

## Accessibility and responsive behavior

- Meet WCAG 2.2 AA contrast for normal text and controls; check both themes.
- All controls have visible keyboard focus and work without a pointer.
- Use semantic headings, landmarks, links, buttons, labels, and status announcements.
- Preserve reduced-motion settings; never make motion the only way to understand state.
- At 360px viewport width, layouts become a readable single column, navigation can be operated without hover, and cards do not overflow.
- At wide widths, marketing content remains centered and the portal retains a compact, readable working canvas.

## Application guidance

- The Astro marketing site explains what Harnessa does without implying that the public cloud service is ready. Keep calls to action honest: read the project, request access only if a real contact path is configured, or open the setup-status portal.
- The separate SvelteKit portal is a static shell. Label provisioning as not yet available, explain that the offering is being prepared, and provide the public marketing-site link. Do not display invented organizations, usage, billing, resources, or user identities.
- The Next.js workspace imports `@harnessa/ui-react` and `@harnessa/design-tokens`, with Tailwind CSS v4 for layout and semantic token utilities. Route-specific controls must use server-returned capabilities; shared visual components do not grant backend permission.
- Board task stage, run status, and specification readiness are separate signals. Green indicates verified or ready state only; unavailable routes must say when their backend workflow is not yet available.
- Astro can render standalone Svelte components through `@astrojs/svelte`; it does not embed or replace the separate SvelteKit app.

## References

- Source tokens: `packages/design-tokens/src/tokens.css`
- React package: `packages/ui-react/`
- Svelte package: `packages/ui-svelte/`
- Marketing site: `apps/marketing/`
- Static portal: `apps/cloud-portal/`
- Stitch project: Harnessa Design System and Cloud Sites (`projects/11838975989788558565`)
