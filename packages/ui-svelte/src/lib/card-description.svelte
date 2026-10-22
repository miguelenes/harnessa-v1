<script lang="ts">
  import type { Snippet } from "svelte";
  import type { HTMLAttributes } from "svelte/elements";
  import { cn } from "./utils.js";
  type Props = HTMLAttributes<HTMLParagraphElement> & { class?: string; children?: Snippet };
  let { class: className, children, ...rest }: Props = $props();
</script>
<p data-slot="card-description" class={cn("text-sm leading-6 text-muted-foreground", className)} {...rest}>{@render children?.()}</p>
