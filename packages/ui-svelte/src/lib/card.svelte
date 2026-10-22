<script lang="ts">
  import type { Snippet } from "svelte";
  import type { HTMLAttributes } from "svelte/elements";
  import { cn } from "./utils.js";

  type Props = HTMLAttributes<HTMLElement> & { class?: string; children?: Snippet };
  let { class: className, children, ...rest }: Props = $props();
</script>

<section data-slot="card" class={cn("rounded-2xl border bg-card text-card-foreground shadow-sm", className)} {...rest}>
  {@render children?.()}
</section>
