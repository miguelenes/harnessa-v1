<script lang="ts">
  import type { Snippet } from "svelte";
  import type { HTMLAttributes } from "svelte/elements";
  import { cn } from "./utils.js";
  type Props = HTMLAttributes<HTMLDivElement> & {
    variant?: "default" | "info" | "success" | "warning" | "destructive";
    class?: string;
    children?: Snippet;
  };
  let { variant = "default", class: className, children, ...rest }: Props = $props();
  const variants = {
    default: "bg-card text-card-foreground",
    info: "border-primary/30 bg-primary/5 text-foreground",
    success: "border-success/35 bg-success/5 text-foreground",
    warning: "border-warning/35 bg-warning/5 text-foreground",
    destructive: "border-destructive/35 bg-destructive/5 text-foreground",
  };
</script>

<div role={variant === "destructive" ? "alert" : "status"} data-slot="alert" class={cn("relative w-full rounded-xl border p-4 text-sm", variants[variant], className)} {...rest}>
  {@render children?.()}
</div>
