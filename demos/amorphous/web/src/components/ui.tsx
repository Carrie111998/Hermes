/* shadcn-style primitives on Station tokens: Button, Badge, Dialog, Tooltip, Tabs.
   Same architecture as shadcn/ui (Radix + CVA + tailwind-merge), themed for
   the Station design system. */
import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import * as TabsPrimitive from "@radix-ui/react-tabs";
import { cva, type VariantProps } from "class-variance-authority";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import { X } from "lucide-react";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/* ============ Button ============ */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md text-[13px] w510 transition-colors outline-none focus-visible:ring-2 focus-visible:ring-blue/50 disabled:pointer-events-none disabled:opacity-50 [&_svg]:shrink-0 cursor-pointer",
  {
    variants: {
      variant: {
        default: "bg-blue text-white hover:bg-blue-2 shadow-[0_1px_2px_rgba(2,6,23,.4),inset_0_1px_0_rgba(255,255,255,.12)]",
        secondary: "border border-line-2 bg-surface text-ink-2 hover:bg-surface-2 hover:text-ink",
        ghost: "text-ink-3 hover:text-ink hover:bg-surface-2",
        destructive: "border border-red/40 text-red hover:bg-red/10",
        outline: "border border-line-2 bg-transparent text-ink-2 hover:bg-surface hover:text-ink",
      },
      size: {
        default: "h-8 px-3.5",
        sm: "h-7 px-2.5 text-[12.5px] rounded-[5px]",
        lg: "h-10 px-5 text-[14px]",
        icon: "h-7 w-7 rounded-[6px]",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />
  )
);
Button.displayName = "Button";

/* ============ Badge ============ */
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10.5px] w590 uppercase tracking-[0.06em] w-fit whitespace-nowrap",
  {
    variants: {
      variant: {
        default: "bg-blue/15 text-blue-2",
        success: "bg-green/12 text-green",
        warning: "bg-amber/12 text-amber",
        danger: "bg-red/12 text-red",
        neutral: "bg-line text-ink-3",
        outline: "border border-line-2 text-ink-3",
      },
    },
    defaultVariants: { variant: "neutral" },
  }
);
export function Badge({ className, variant, ...props }:
  React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

/* ============ Dialog ============ */
export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export function DialogContent({ className, children, ...props }:
  React.ComponentProps<typeof DialogPrimitive.Content>) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay
        className="fixed inset-0 z-[120] bg-[#05080f]/75 backdrop-blur-[3px] data-[state=open]:animate-in data-[state=open]:fade-in-0" />
      <DialogPrimitive.Content
        className={cn(
          "fixed inset-0 z-[121] m-auto",
          "w-[min(1060px,92vw)] h-[min(720px,86vh)] flex flex-col",
          "bg-panel border border-line-2 rounded-2xl overflow-hidden",
          "shadow-[0_32px_90px_rgba(2,6,23,.8),0_0_0_1px_rgba(255,255,255,.03)]",
          className)}
        {...props}>
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

export function DialogHeader({ title, subtitle, children }:
  { title: React.ReactNode; subtitle?: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div className="flex items-center gap-3 h-12 px-4 border-b border-line shrink-0">
      <DialogPrimitive.Title className="text-[14px] w590 truncate">{title}</DialogPrimitive.Title>
      {subtitle && <span className="text-[12px] text-ink-4 truncate">{subtitle}</span>}
      <div className="ml-auto flex items-center gap-1">
        {children}
        <DialogPrimitive.Close asChild>
          <Button variant="ghost" size="icon" aria-label="Close"><X size={15} /></Button>
        </DialogPrimitive.Close>
      </div>
    </div>
  );
}

/* ============ Tooltip ============ */
export const TooltipProvider = TooltipPrimitive.Provider;
export function Tip({ label, children, side = "top" }:
  { label: string; children: React.ReactNode; side?: "top" | "bottom" | "left" | "right" }) {
  return (
    <TooltipPrimitive.Root delayDuration={350}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content side={side} sideOffset={6}
          className="z-[130] rounded-md bg-surface-2 border border-line-2 px-2.5 py-1 text-[11.5px] text-ink-2 shadow-[0_8px_24px_rgba(2,6,23,.6)] select-none">
          {label}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

/* ============ Tabs ============ */
export const Tabs = TabsPrimitive.Root;
export function TabsList({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      className={cn("inline-flex items-center gap-0.5 rounded-lg bg-surface border border-line p-0.5", className)}
      {...props} />
  );
}
export function TabsTrigger({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        "inline-flex items-center gap-1.5 h-7 px-3 rounded-[6px] text-[12.5px] text-ink-3 cursor-pointer",
        "transition-colors hover:text-ink-2",
        "data-[state=active]:bg-surface-2 data-[state=active]:text-ink data-[state=active]:w510",
        "data-[state=active]:shadow-[inset_0_1px_0_rgba(255,255,255,.05),0_1px_2px_rgba(2,6,23,.4)]",
        className)}
      {...props} />
  );
}
export const TabsContent = TabsPrimitive.Content;
