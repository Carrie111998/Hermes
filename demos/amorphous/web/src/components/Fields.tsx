/* Typed input field system — every workflow/component can declare inputs of
   any kind and get sleek, token-consistent controls (Radix + Station tokens):
     {name, label, type: text|textarea|number|select|switch|slider|date|password,
      required?, placeholder?, default?, options?[{value,label}], min?, max?, step?}
*/
import * as SelectPrimitive from "@radix-ui/react-select";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import * as SliderPrimitive from "@radix-ui/react-slider";
import { Check, ChevronDown } from "lucide-react";
import { cn } from "./ui";

export interface FieldSpec {
  name: string;
  label?: string;
  type?: string;           // default "text"
  required?: boolean;
  placeholder?: string;
  default?: any;
  options?: { value: string; label?: string }[] | string[];
  min?: number;
  max?: number;
  step?: number;
  rows?: number;
}

export function fieldDefaults(specs: FieldSpec[]): Record<string, any> {
  const out: Record<string, any> = {};
  for (const f of specs) {
    if (f.default !== undefined) out[f.name] = f.default;
    else if (f.type === "switch") out[f.name] = false;
    else if (f.type === "slider") out[f.name] = f.min ?? 0;
    else out[f.name] = "";
  }
  return out;
}

export function missingRequired(specs: FieldSpec[], values: Record<string, any>): string[] {
  return specs
    .filter((f) => f.required !== false && f.type !== "switch" && f.type !== "slider")
    .filter((f) => String(values[f.name] ?? "").trim() === "")
    .map((f) => f.label || f.name);
}

const inputCls =
  "w-full h-8 px-3 bg-[#0d1526] border border-line-2 rounded-lg text-[13px] text-ink " +
  "placeholder:text-ink-4 outline-none focus:border-blue/60 focus:ring-1 focus:ring-blue/30 transition-colors";

export function Field({ spec, value, onChange, invalid }: {
  spec: FieldSpec; value: any; onChange: (v: any) => void; invalid?: boolean;
}) {
  const type = spec.type || "text";
  const label = spec.label || spec.name;
  const err = invalid ? "border-red/60 focus:border-red/60 focus:ring-red/25" : "";

  return (
    <label className="flex flex-col gap-1 min-w-0">
      <span className="text-[10.5px] w590 uppercase tracking-[0.07em] text-ink-4 flex items-center gap-1">
        {label}
        {spec.required !== false && type !== "switch" && type !== "slider" && (
          <span className={invalid ? "text-red" : "text-ink-4/60"}>*</span>
        )}
      </span>

      {type === "textarea" && (
        <textarea rows={spec.rows ?? 3} value={value ?? ""} placeholder={spec.placeholder}
                  onChange={(e) => onChange(e.target.value)}
                  className={cn(inputCls, "h-auto py-2 resize-y leading-relaxed", err)} />
      )}

      {(type === "text" || type === "password" || type === "date") && (
        <input type={type} value={value ?? ""} placeholder={spec.placeholder}
               onChange={(e) => onChange(e.target.value)}
               className={cn(inputCls, type === "date" && "[color-scheme:dark]", err)} />
      )}

      {type === "number" && (
        <input type="number" value={value ?? ""} placeholder={spec.placeholder}
               min={spec.min} max={spec.max} step={spec.step}
               onChange={(e) => onChange(e.target.value)}
               className={cn(inputCls, "tabular-nums", err)} />
      )}

      {type === "select" && (
        <SelectField spec={spec} value={value} onChange={onChange} invalid={invalid} />
      )}

      {type === "switch" && (
        <div className="flex items-center gap-2.5 h-8">
          <SwitchPrimitive.Root checked={!!value} onCheckedChange={onChange}
            className="w-9 h-[22px] rounded-full bg-line-2 data-[state=checked]:bg-blue transition-colors relative outline-none focus-visible:ring-2 focus-visible:ring-blue/40 cursor-pointer">
            <SwitchPrimitive.Thumb
              className="block w-[18px] h-[18px] bg-ink rounded-full translate-x-[2px] data-[state=checked]:translate-x-[18px] transition-transform shadow" />
          </SwitchPrimitive.Root>
          <span className="text-[12.5px] text-ink-3">{value ? "On" : "Off"}</span>
        </div>
      )}

      {type === "slider" && (
        <div className="flex items-center gap-3 h-8">
          <SliderPrimitive.Root
            value={[Number(value ?? spec.min ?? 0)]}
            min={spec.min ?? 0} max={spec.max ?? 100} step={spec.step ?? 1}
            onValueChange={([v]) => onChange(v)}
            className="relative flex items-center flex-1 h-4 cursor-pointer">
            <SliderPrimitive.Track className="relative h-[4px] flex-1 rounded-full bg-line-2">
              <SliderPrimitive.Range className="absolute h-full rounded-full bg-blue" />
            </SliderPrimitive.Track>
            <SliderPrimitive.Thumb
              className="block w-3.5 h-3.5 bg-ink rounded-full shadow outline-none focus-visible:ring-2 focus-visible:ring-blue/40" />
          </SliderPrimitive.Root>
          <span className="text-[12px] tabular-nums text-ink-2 w-10 text-right">{value ?? spec.min ?? 0}</span>
        </div>
      )}
    </label>
  );
}

function SelectField({ spec, value, onChange, invalid }: {
  spec: FieldSpec; value: any; onChange: (v: any) => void; invalid?: boolean;
}) {
  const opts = (spec.options || []).map((o) =>
    typeof o === "string" ? { value: o, label: o } : { value: o.value, label: o.label || o.value });
  return (
    <SelectPrimitive.Root value={value || undefined} onValueChange={onChange}>
      <SelectPrimitive.Trigger
        className={cn(inputCls, "inline-flex items-center justify-between gap-2 cursor-pointer data-[placeholder]:text-ink-4",
          invalid && "border-red/60")}>
        <SelectPrimitive.Value placeholder={spec.placeholder || "Select…"} />
        <SelectPrimitive.Icon><ChevronDown size={14} className="text-ink-4" /></SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content position="popper" sideOffset={4}
          className="z-[150] min-w-[var(--radix-select-trigger-width)] bg-surface-2 border border-line-2 rounded-lg p-1 shadow-[0_12px_36px_rgba(2,6,23,.6)]">
          <SelectPrimitive.Viewport>
            {opts.map((o) => (
              <SelectPrimitive.Item key={o.value} value={o.value}
                className="flex items-center gap-2 px-2.5 py-1.5 rounded-md text-[13px] text-ink-2 cursor-pointer outline-none data-[highlighted]:bg-blue/12 data-[highlighted]:text-ink">
                <SelectPrimitive.ItemText>{o.label}</SelectPrimitive.ItemText>
                <SelectPrimitive.ItemIndicator className="ml-auto"><Check size={13} className="text-blue-2" /></SelectPrimitive.ItemIndicator>
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
}

/* Derive input specs from a workflow prompt template's {placeholders} when the
   component doesn't declare props.inputs — no workflow can render without its
   fields again. */
export function deriveInputs(promptTemplate: string | undefined, declared?: FieldSpec[]): FieldSpec[] {
  const specs: FieldSpec[] = [...(declared || [])];
  const have = new Set(specs.map((s) => s.name));
  if (promptTemplate) {
    for (const m of promptTemplate.matchAll(/\{(\w+)\}/g)) {
      const name = m[1];
      if (name === "context" || have.has(name)) continue;
      have.add(name);
      specs.push({
        name,
        label: name.replace(/_/g, " "),
        type: /number|count|limit|id$|^n$/i.test(name) ? "number" : "text",
        required: true,
      });
    }
  }
  return specs;
}
