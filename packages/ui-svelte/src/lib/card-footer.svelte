<script lang="ts">
  import type { Snippet } from "svelte";
  import type { HTMLAttributes } from "svelte/elements";
  import { cn } from "./utils.js";
  type Props = HTMLAttributes<HTMLDivElement> & { class?: string; children?: Snippet };
  let { class: className, children, ...rest }: Props = $props();
</script>
<div data-slot="card-footer" class={cn("flex items-center gap-2 px-6 pb-6", className)} {...rest}>{@render children?.()}</div>
