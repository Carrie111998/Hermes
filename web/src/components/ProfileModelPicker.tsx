import { Check, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { Input } from "@nous-research/ui/ui/components/input";
import { cn } from "@/lib/utils";
import {
  filterProfileModelChoices,
  profileModelKey,
  type ProfileModelChoice,
} from "@/lib/profile-model-picker";

interface ProfileModelPickerProps {
  choices: ProfileModelChoice[] | null;
  emptyLabel: string;
  inheritLabel?: string;
  loadingLabel: string;
  onSelect(value: string): void;
  searchInputId?: string;
  selected: string;
}

export function ProfileModelPicker({
  choices,
  emptyLabel,
  inheritLabel,
  loadingLabel,
  onSelect,
  searchInputId,
  selected,
}: ProfileModelPickerProps) {
  const [query, setQuery] = useState("");
  const filteredChoices = useMemo(
    () => filterProfileModelChoices(choices ?? [], query),
    [choices, query],
  );

  const rowClass = (active: boolean) =>
    cn(
      "flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-mono transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring",
      active
        ? "bg-primary/10 text-primary"
        : "text-foreground hover:bg-muted/50",
    );

  return (
    <div className="grid gap-2">
      <div className="relative">
        <Search
          aria-hidden
          className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
        />
        <Input
          id={searchInputId}
          aria-label="Search models"
          className="h-8 pl-8 text-sm"
          disabled={choices === null}
          placeholder="Filter models…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>

      <div className="max-h-80 overflow-y-auto rounded border border-border">
        {inheritLabel && choices !== null && (
          <button
            type="button"
            aria-pressed={selected === ""}
            className={rowClass(selected === "")}
            onClick={() => onSelect("")}
          >
            <Check
              aria-hidden
              className={cn(
                "h-3 w-3 shrink-0",
                selected === "" ? "text-primary" : "text-transparent",
              )}
            />
            <span className="truncate">{inheritLabel}</span>
          </button>
        )}

        {choices === null ? (
          <p className="p-3 text-xs text-muted-foreground">{loadingLabel}</p>
        ) : choices.length === 0 ? (
          <p className="p-3 text-xs text-muted-foreground">{emptyLabel}</p>
        ) : filteredChoices.length === 0 ? (
          <p className="p-3 text-xs text-muted-foreground">
            No models match your search.
          </p>
        ) : (
          filteredChoices.map((choice) => {
            const key = profileModelKey(choice);
            const active = selected === key;
            return (
              <button
                key={key}
                type="button"
                aria-pressed={active}
                className={rowClass(active)}
                onClick={() => onSelect(key)}
              >
                <Check
                  aria-hidden
                  className={cn(
                    "h-3 w-3 shrink-0",
                    active ? "text-primary" : "text-transparent",
                  )}
                />
                <span className="truncate">{choice.label}</span>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
