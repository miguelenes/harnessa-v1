<script lang="ts">
  import type { Snippet } from "svelte";
  import type { HTMLAnchorAttributes } from "svelte/elements";
  import { buttonVariants } from "./button-variants.js";
  import { cn } from "./utils.js";

  type Props = HTMLAnchorAttributes & {
    href: string;
    variant?: "default" | "primary" | "secondary" | "outline" | "ghost" | "destructive" | "link";
    size?: "default" | "sm" | "lg" | "icon";
    class?: string;
    children?: Snippet;
  };

  let { href, variant = "default", size = "default", class: className, children, ...rest }: Props = $props();
</script>

<a href={href} data-slot="button" class={cn(buttonVariants({ variant, size }), className)} {...rest}>
  {@render children?.()}
</a>
