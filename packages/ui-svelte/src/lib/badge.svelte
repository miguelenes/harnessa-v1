<script lang="ts">
  import type { Snippet } from "svelte";
  import type { HTMLAttributes } from "svelte/elements";
  import { badgeVariants } from "./badge-variants.js";
  import { cn } from "./utils.js";

  type Props = HTMLAttributes<HTMLSpanElement> & {
    variant?: "default" | "secondary" | "outline" | "success" | "warning" | "destructive";
    class?: string;
    children?: Snippet;
  };

  let { variant = "default", class: className, children, ...rest }: Props = $props();
</script>

<span data-slot="badge" class={cn(badgeVariants({ variant }), className)} {...rest}>
  {@render children?.()}
</span>
