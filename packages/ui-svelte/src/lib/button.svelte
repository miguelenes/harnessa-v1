<script lang="ts">
  import type { Snippet } from "svelte";
  import type { HTMLButtonAttributes } from "svelte/elements";
  import { buttonVariants } from "./button-variants.js";
  import { cn } from "./utils.js";

  type Props = HTMLButtonAttributes & {
    variant?: "default" | "primary" | "secondary" | "outline" | "ghost" | "destructive" | "link";
    size?: "default" | "sm" | "lg" | "icon";
    class?: string;
    children?: Snippet;
  };

  let { variant = "default", size = "default", class: className, children, ...rest }: Props = $props();
</script>

<button data-slot="button" class={cn(buttonVariants({ variant, size }), className)} {...rest}>
  {@render children?.()}
</button>
