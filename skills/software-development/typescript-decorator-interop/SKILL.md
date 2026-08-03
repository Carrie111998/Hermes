---
name: typescript-decorator-interop
description: "Diagnose TypeScript legacy and Stage 3 decorator boundaries."
version: 1.0.0
author: Heeho3 (heeho3) with Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [typescript, decorators, stage-3, esm, commonjs, interop]
    related_skills: [test-driven-development, systematic-debugging]
---

# TypeScript Decorator Interoperability Skill

Use this skill to determine whether a TypeScript application's module format and decorator mode can safely consume a decorator-based dependency. It diagnoses project metadata and explains safe boundaries; it does not rewrite third-party decorators or claim that CommonJS and legacy decorators can be made equivalent to Stage 3 decorators.

## When to Use

- A NestJS or other `experimentalDecorators` application needs a dependency documented for standard (Stage 3) decorators.
- An ESM-only package must be reached from CommonJS code.
- A TypeScript build reports TS1241 or decorator signature/context errors after adding a package.
- Before creating a bridge, sidecar, subproject, or a new tsconfig for a decorator-based library.

Do not use this skill for ordinary import spelling or a runtime error unrelated to TypeScript compilation.

## Prerequisites

- Use the `terminal` tool with Python 3 available.
- Run from the TypeScript project's root, or provide its root path explicitly.
- The diagnostic reads `package.json`, `tsconfig.json`, and local JSONC `extends` files. It does not install dependencies, contact npm, or modify project files.

## How to Run

Run the shipped diagnostic before changing configuration:

```bash
python3 skills/software-development/typescript-decorator-interop/scripts/inspect_project.py /path/to/project
```

For a non-default config, pass it explicitly:

```bash
python3 skills/software-development/typescript-decorator-interop/scripts/inspect_project.py /path/to/project \
  --tsconfig /path/to/project/tsconfig.build.json
```

The output is JSON. Treat `severity: "error"` as a compilation-boundary problem, not a prompt to change a decorator's parameter types.

## Quick Reference

| Situation | Safe interpretation |
|---|---|
| `experimentalDecorators: true` | Legacy TypeScript decorator calling convention for that compilation unit. |
| `experimentalDecorators: false` or omitted in current TypeScript | Standard (Stage 3) decorator calling convention. |
| Legacy and standard decorator source in one `tsconfig` program | Unsupported: TypeScript selects one decorator mode for the whole compilation unit. |
| CommonJS caller needs an ESM-only runtime package | Put the runtime entry behind an async `import()` boundary; this does not make source decorators compatible. |
| A dependency has a `require` export | It may be runtime-loadable from CJS, but its TypeScript source/decorator mode still must match its own documentation. |

## Procedure

1. **Inspect the actual build config.** Run the diagnostic against the tsconfig used by the application build, not an editor-only config. Completion criterion: record `decorator_mode`, `module`, `module_resolution`, and every finding.
2. **Classify the boundary.** If `standard-decorator-dependency-in-legacy-mode` and `separate-compilation-unit-required` appear together, preserve the legacy application compilation unit. Do not toggle its `experimentalDecorators` setting just to satisfy another library. Completion criterion: the application decorators still compile in their original mode.
3. **Separate source compilation only when source decorators are needed.** Compile TypeMCP/TypeChain-style Stage 3-decorated source in a dedicated ESM/standard-decorator project reference, package, or independently built entrypoint. Keep the boundary explicit through plain runtime values, JSON/RPC, CLI, HTTP, or another stable non-decorator contract. Completion criterion: no source file is checked under both decorator modes.
4. **Handle runtime module format independently.** For an ESM-only dependency called from CJS, use an async dynamic-import boundary owned by a CJS-safe module. Do not use `require()` merely because it suppresses an import error; verify the dependency's export map. Completion criterion: the CJS process loads the ESM entry through its documented runtime path.
5. **Validate both ends.** Run each compilation unit's real typecheck/build command and one integration path across the boundary. Completion criterion: the legacy app and standard-decorator entrypoint each compile independently, and the handoff executes without decorator metadata assumptions.

## TypeMCP and TypeChain Notes

As verified from their published package metadata and README at the time this skill was added:

- `@theorvane/type-mcp` publishes an ESM-first package with a root `require` export, but its standard decorator examples require Node-aware resolution and explicitly reject legacy `experimentalDecorators` mode.
- `@theorvane/type-chain` publishes ESM-only exports and requires standard (Stage 3) decorators. A CommonJS caller needs an async ESM runtime boundary even after source compilation is isolated.
- Neither fact changes TypeScript's compiler rule: decorator mode is selected per `tsconfig` compilation unit. A dual export map solves runtime loading only; it cannot make a legacy decorator emit and a standard decorator emit coexist in one program.

Do not invent TypeMCP/TypeChain decorators, metadata formats, or adapter APIs. Use their installed package version's documentation for the Stage 3 entrypoint and expose only a documented value-level boundary to the legacy application.

## Pitfalls

1. **Changing `experimentalDecorators` globally.** This can break NestJS-style legacy decorator signatures and metadata behavior. Keep the existing application compiler mode intact.
2. **Confusing ESM/CJS with decorator mode.** They are separate axes. CJS may dynamically import ESM at runtime, but this does not change how TypeScript typechecks or emits decorators.
3. **Using `skipLibCheck` as a source fix.** It can hide declaration checking problems but does not make incompatible decorated application source compile or run correctly.
4. **Using a declaration shim for decorated source.** A shim can conceal a type error while leaving incompatible runtime decorator invocation semantics intact.
5. **Assuming `require` support means legacy decorator support.** Export maps control loading. Decorator calling conventions are chosen by TypeScript during compilation.

## Verification

```bash
scripts/run_tests.sh tests/skills/test_typescript_decorator_interop_skill.py -q
```

For a consumer project, run the diagnostic plus that project's actual legacy and Stage 3 compilation commands separately. A correct result explicitly reports a boundary when both decorator modes are required; it never promises a one-tsconfig workaround.
