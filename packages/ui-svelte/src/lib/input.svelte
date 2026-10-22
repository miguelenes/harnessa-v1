<script lang="ts">
  import type { HTMLInputAttributes } from "svelte/elements";
  import { cn } from "./utils.js";
  type Props = HTMLInputAttributes & { class?: string; value?: string | number };
  let { class: className, value = $bindable(), type = "text", ...rest }: Props = $props();
</script>

<input bind:value type={type} data-slot="input" class={cn("flex h-10 w-full rounded-lg border border-input bg-background px-3 py-2 text-sm text-foreground shadow-sm outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:cursor-not-allowed disabled:opacity-50", className)} {...rest} />
