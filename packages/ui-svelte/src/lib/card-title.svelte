<script lang="ts">
  import type { Snippet } from "svelte";
  import type { HTMLAttributes } from "svelte/elements";
  import { cn } from "./utils.js";
  type Props = HTMLAttributes<HTMLHeadingElement> & { class?: string; children?: Snippet };
  let { class: className, children, ...rest }: Props = $props();
</script>
<h3 data-slot="card-title" class={cn("text-base font-semibold leading-none tracking-tight", className)} {...rest}>{@render children?.()}</h3>
