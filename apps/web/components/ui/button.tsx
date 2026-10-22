import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";
const buttonVariants = cva("inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-50", { variants: { variant: { default: "bg-emerald-800 text-white hover:bg-emerald-900", outline: "border border-stone-300 bg-white hover:bg-stone-50", ghost: "hover:bg-stone-100" }, size: { default: "h-10 px-4 py-2", sm: "h-9 px-3", lg: "h-11 px-6" } }, defaultVariants: { variant: "default", size: "default" } });
export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {}
export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, ...props }, ref) => <button ref={ref} className={cn(buttonVariants({ variant, size, className }))} {...props} />);
Button.displayName = "Button";
