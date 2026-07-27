type BraceExpansionOptions = {
  max?: number;
  maxLength?: number;
};

declare function braceExpansion(
  pattern: string,
  options?: BraceExpansionOptions,
): string[];

declare namespace braceExpansion {
  const EXPANSION_MAX: 100000;
  const EXPANSION_MAX_LENGTH: 4000000;
  const expand: typeof braceExpansion;
}

export = braceExpansion;
