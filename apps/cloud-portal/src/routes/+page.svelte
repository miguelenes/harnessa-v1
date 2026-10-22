<script lang="ts">
  import {
    Alert,
    Badge,
    Button,
    Card,
    CardContent,
    CardDescription,
    CardHeader,
    CardTitle,
    LinkButton,
  } from "@harnessa/ui-svelte";

  const readiness = [
    { title: "Cloud product definition", detail: "Service scope and operating model", state: "Being designed" },
    { title: "Account and access model", detail: "Identity, tenancy, and permissions", state: "Not configured" },
    { title: "Workspace provisioning", detail: "Customer setup and resource lifecycle", state: "Not available" },
  ];

  const nextSteps = [
    { id: "01", title: "Define the hosted offering" },
    { id: "02", title: "Design account boundaries" },
    { id: "03", title: "Build a secure provisioning path" },
  ];
</script>

<svelte:head>
  <title>Cloud preview · Harnessa</title>
  <meta name="description" content="The Harnessa Cloud portal foundation is in development. Account provisioning is not yet available." />
</svelte:head>

<div class="portal-shell">
  <a class="skip-link" href="#overview">Skip to content</a>
  <header class="portal-topbar">
    <a class="portal-brand" href="https://harnessa.cloud" aria-label="Harnessa home">
      <svg viewBox="0 0 36 36" fill="none" aria-hidden="true">
        <path d="M13 8H8v20h5M23 8h5v20h-5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
        <path d="m14 21 4-6 4 6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" />
        <circle cx="18" cy="25" r="1.5" fill="currentColor" />
      </svg>
      <span>harnessa</span>
    </a>
    <div class="topbar-right">
      <span class="topbar-label">Cloud control plane</span>
      <span class="preview-pill"><span aria-hidden="true"></span> Preview</span>
    </div>
  </header>

  <div class="portal-body">
    <aside class="portal-sidebar" aria-label="Cloud portal navigation">
      <div class="environment-card">
        <div class="environment-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none"><path d="M4 7.5 12 3l8 4.5v9L12 21l-8-4.5v-9Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="m4.5 7.7 7.5 4.4 7.5-4.4M12 12v8.5" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>
        </div>
        <div class="environment-meta"><strong>Harnessa Cloud</strong><small>Foundation preview</small></div>
      </div>

      <div class="side-label">Workspace</div>
      <nav class="side-nav" aria-label="Workspace">
        <a class="side-link active" href="#overview" aria-current="page">
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3.5" y="3.5" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.7"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.7"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.7"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.7"/></svg>
          Overview
        </a>
        <span class="side-link disabled" aria-disabled="true">
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 7.5 12 3l8 4.5v9L12 21l-8-4.5v-9Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="m4.5 7.7 7.5 4.4 7.5-4.4" stroke="currentColor" stroke-width="1.7"/></svg>
          Workspaces <span class="soon-tag">Soon</span>
        </span>
        <span class="side-link disabled" aria-disabled="true">
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 18.5h16M6 15V9m4 6V5m4 10v-4m4 4V7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
          Usage <span class="soon-tag">Soon</span>
        </span>
        <span class="side-link disabled" aria-disabled="true">
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4Z" stroke="currentColor" stroke-width="1.7"/><path d="m19.4 15 .1.1a1.7 1.7 0 0 1-2.4 2.4l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a1.7 1.7 0 0 1-3.4 0v-.2a1.7 1.7 0 0 0-2.9-1.2l-.1.1a1.7 1.7 0 0 1-2.4-2.4l.1-.1A1.7 1.7 0 0 0 4.2 12h-.2a1.7 1.7 0 0 1 0-3.4h.2a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a1.7 1.7 0 0 1 2.4-2.4l.1.1a1.7 1.7 0 0 0 2.9-1.2v-.2a1.7 1.7 0 0 1 3.4 0v.2a1.7 1.7 0 0 0 2.9 1.2l.1-.1a1.7 1.7 0 0 1 2.4 2.4l-.1.1A1.7 1.7 0 0 0 20.6 9h.2a1.7 1.7 0 0 1 0 3.4h-.2a1.7 1.7 0 0 0-1.2 2.6Z" stroke="currentColor" stroke-width="1.25" stroke-linejoin="round"/></svg>
          Settings <span class="soon-tag">Soon</span>
        </span>
      </nav>

      <p class="sidebar-note">This preview contains no connected account or customer workspace.</p>
    </aside>

    <main id="overview" class="portal-main" tabindex="-1">
      <div class="breadcrumb"><span>Harnessa Cloud</span><span aria-hidden="true">/</span><strong>Overview</strong></div>

      <div class="portal-heading">
        <div><h1>Cloud workspace</h1><p>The portal foundation is taking shape. Provisioning is not yet available.</p></div>
        <Badge variant="outline" class="heading-badge"><span class="badge-dot" aria-hidden="true"></span> Static preview</Badge>
      </div>

      <Alert variant="info" class="portal-alert">
        <div class="alert-icon" aria-hidden="true">i</div>
        <div><strong>Harnessa Cloud is in development</strong><p>This page is a static design preview. It is not connected to an account, and it cannot create or manage cloud resources.</p></div>
      </Alert>

      <div class="overview-grid">
        <Card class="portal-card setup-card">
          <CardHeader>
            <span class="card-eyebrow"><span></span> SETUP STATUS</span>
            <CardTitle>Workspace provisioning is not available yet</CardTitle>
            <CardDescription>We are defining the hosted product and its account model before enabling customer setup.</CardDescription>
          </CardHeader>
          <CardContent class="setup-card-content">
            <div class="empty-symbol" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none"><path d="M4 7.5 12 3l8 4.5v9L12 21l-8-4.5v-9Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="m4.5 7.7 7.5 4.4 7.5-4.4M12 12v8.5" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M17.5 3.5v4m-2-2h4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
            </div>
            <h2>No workspace connected</h2>
            <p>There is no account data associated with this preview. When the service is ready, this area will guide you through setup.</p>
            <div class="setup-note"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7"/><path d="M12 10.5v5m0-8v.1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg><span>Nothing will be provisioned or billed from this page.</span></div>
          </CardContent>
        </Card>

        <Card class="portal-card">
          <CardHeader>
            <span class="card-eyebrow"><span></span> PRODUCT READINESS</span>
            <CardTitle>What is being prepared</CardTitle>
            <CardDescription>These are design areas, not live system health checks.</CardDescription>
          </CardHeader>
          <CardContent>
            <div class="readiness-list">
              {#each readiness as item}
                <div class="readiness-item">
                  <span class="readiness-mark" aria-hidden="true">—</span>
                  <div><strong>{item.title}</strong><small>{item.detail}</small></div>
                  <span class="state">{item.state}</span>
                </div>
              {/each}
            </div>
          </CardContent>
        </Card>
      </div>

      <div class="lower-grid">
        <Card class="portal-card">
          <CardHeader>
            <span class="card-eyebrow"><span></span> NEXT IN DESIGN</span>
            <CardTitle>Foundations before features</CardTitle>
            <CardDescription>The hosted service needs clear operating boundaries before customer workflows are enabled.</CardDescription>
          </CardHeader>
          <CardContent>
            <div class="next-steps">
              {#each nextSteps as step}
                <div class="next-step"><small>{step.id}</small><strong>{step.title}</strong></div>
              {/each}
            </div>
          </CardContent>
        </Card>

        <Card class="portal-card site-link-card">
          <CardHeader>
            <span class="card-eyebrow"><span></span> ABOUT HARNESA</span>
            <CardTitle>Agentic engineering, kept in the loop.</CardTitle>
            <CardDescription>Learn about the product direction and the principles shaping the workbench.</CardDescription>
          </CardHeader>
          <CardContent>
            <LinkButton href="https://harnessa.cloud" variant="outline" size="sm">Visit harnessa.cloud <span aria-hidden="true">↗</span></LinkButton>
          </CardContent>
        </Card>
      </div>

      <footer class="portal-footer"><span>Harnessa Cloud · Static preview</span><a href="https://harnessa.cloud">About the project <span aria-hidden="true">↗</span></a></footer>
    </main>
  </div>
</div>

<style>
  :global(.heading-badge .badge-dot) { width: 6px; height: 6px; border-radius: 50%; background: var(--primary); }
  :global(.portal-alert) { display: flex; align-items: flex-start; gap: 10px; }
  .alert-icon { display: grid; width: 18px; height: 18px; flex: 0 0 18px; place-items: center; border: 1px solid #c8c5f2; border-radius: 50%; color: #5146c5; font-family: var(--font-mono); font-size: 10px; font-weight: 650; }
  :global(.setup-card-content h2) { margin-bottom: 6px; }
</style>
