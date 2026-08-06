# nix/python.nix — uv2nix virtual environment builder
{
  python312,
  lib,
  callPackage,
  uv2nix,
  pyproject-nix,
  pyproject-build-systems,
  stdenv,
  # Filtered Python source (see lib.nix pythonSrc) — keeps JS/docs/skills
  # edits from invalidating the venv derivation.
  pythonSrc,
  dependency-groups ? [ "all" ],
}:
let
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = pythonSrc; };
  hacks = callPackage pyproject-nix.build.hacks { };

  # Parse uv.lock to build a version map for the auto-substitution version
  # check.  We read uv.lock from the repo root (../uv.lock from this file).
  # The version map prevents substituting packages whose nixpkgs version
  # differs from the uv.lock pin — mismatches cause metadata check failures
  # and pull in incompatible transitive deps.
  uvLockVersions =
    builtins.listToAttrs
      (map
        (pkg: {
          name = pkg.name;
          value = pkg.version;
        })
        (builtins.fromTOML (builtins.readFile ../uv.lock)).package);

  overlay = workspace.mkPyprojectOverlay {
    sourcePreference = "wheel";
  };

  # Substitute a uv2nix-built package with the equivalent nixpkgs prebuilt
  # version.  nixpkgs builds are cached on cache.nixos.org, so this avoids
  # hundreds of local compilations — many of which are native (C/Rust/Cython)
  # and slow to build from source.
  #
  # The uv2nix overlay populates `passthru.dependencies` from uv.lock.  We
  # preserve that passthru so the venv resolver still sees the correct
  # dependency graph — no manual dependency lists needed.
  #
  # nixpkgsPrebuilt searches `prev.nativeBuildInputs` for `pyproject-hook` to
  # determine the Python version for ABI compatibility checks.  The uv2nix
  # package may use `pyprojectWheelHook` instead (wheel-only packages), so we
  # always inject `pyprojectHook` to satisfy that lookup.
  mkPrebuiltOverride =
    final: from: prevPkg:
    hacks.nixpkgsPrebuilt {
      inherit from;
      prev = {
        nativeBuildInputs = [ final.pyprojectHook ];
        passthru = prevPkg.passthru;
      };
    };

  # Auto-substitute every package that exists in both the uv2nix overlay and
  # nixpkgs.  We build a set of nixpkgs Python package names (without evaluating
  # their values) and intersect it with the uv2nix package names.
  #
  # Packages not in nixpkgs (e.g. ruamel-yaml, some alibabacloud SDKs) fall
  # through to the normal uv2nix build automatically.  When a new dep is added
  # to hermes-agent that already exists in nixpkgs, it's auto-substituted —
  # no manual list to maintain.
  #
  # Packages that throw on evaluation (e.g. olm marked insecure) are excluded
  # via tryEval on .pname — they fall through to the normal uv2nix build.
  # Note: tryEval catches `throw` but NOT `assert` failures.  nixpkgs'
  # insecure-package checks use `assert`, so packages with `assert`-based
  # guards need an explicit denylist entry.
  wellKnown = [
    "python"
    "pkgs"
    "stdenv"
    "pythonPkgsBuildHost"
    "resolveBuildSystem"
    "resolveVirtualEnv"
    "mkVirtualEnv"
    "hooks"
    "callPackage"
  ];

  # Packages that can't be safely evaluated from nixpkgs (assert-based guards
  # that tryEval can't catch) OR whose nixpkgs versions have significantly
  # heavier transitive dependencies than their uv.lock counterparts.
  #
  # ML/AI packages like tokenizers and huggingface-hub pull in torch, scipy,
  # transformers etc. via the nixpkgs store closure. nixpkgsPrebuilt symlinks
  # to the nixpkgs package's output, which drags the entire heavy closure into
  # the build graph — even with an empty passthru stub. These packages fall
  # through to the normal uv2nix build, which uses the lighter uv.lock deps.
  denylist = [
    "python-olm"       # olm marked insecure via assert, not throw
    "ctranslate2"      # nixpkgs pulls in torch
    "faster-whisper"   # nixpkgs pulls in torch, transformers
    "hf-xet"           # nixpkgs pulls in torch
    "huggingface-hub"  # nixpkgs pulls in torch, pyarrow
    "onnxruntime"      # nixpkgs pulls in torch, onnx
    "tokenizers"       # nixpkgs pulls in torch, transformers
    # Build-system packages — must stay as uv2nix builds for correct hook
    # integration.  Substituting them with nixpkgs prebuilt versions breaks
    # uv2nix's build isolation (setuptools not found in build env).
    "setuptools"       # used by buildSystemOverrides for legacy sdist packages
    "wheel"            # build-system dep
    "pip"              # build-system dep
  ];

  # Build a set of nixpkgs Python package names without evaluating values.
  # hasAttr does not force the attribute's value, so this is safe for packages
  # that would throw/assert on evaluation.
  nixpkgsNames = builtins.attrNames python312.pkgs;

  prebuiltOverrides =
    final: prev:
    builtins.listToAttrs (
      builtins.filter
        (s: s != null)
        (map
          (name:
            if builtins.elem name wellKnown
              || builtins.elem name denylist
              || !(builtins.elem name nixpkgsNames)
            then null
            else
              let
                # tryEval catches packages that throw on evaluation (e.g.
                # broken/unfree via throw).  assert-based guards (e.g. insecure)
                # are handled by the denylist above.
                evaled = builtins.tryEval python312.pkgs.${name}.pname;
              in
              if !evaled.success
              then null
              else
                let
                  nixpkgsPkg = python312.pkgs.${name};
                  # Only substitute when the nixpkgs version matches the uv.lock
                  # version.  Version mismatches cause metadata check failures
                  # and pull in incompatible transitive deps (e.g. nixpkgs modal
                  # 1.5.3 pulls in grpcio-tools, while uv.lock pins 1.3.4 which
                  # doesn't).  Packages with mismatched versions fall through
                  # to the normal uv2nix build.
                  lockVersion = uvLockVersions.${name} or null;
                in
                if lockVersion == null || lockVersion != nixpkgsPkg.version
                then null
                else {
                  inherit name;
                  value = mkPrebuiltOverride final nixpkgsPkg prev.${name};
                })
          (builtins.attrNames prev)
        )
    );

  # Legacy alibabacloud packages ship only sdists with setup.py/setup.cfg
  # and no pyproject.toml, so setuptools isn't declared as a build dep.
  buildSystemOverrides =
    final: prev:
    builtins.mapAttrs
      (
        name: _:
        prev.${name}.overrideAttrs (old: {
          nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ final.setuptools ];
        })
      )
      (
        lib.genAttrs [
          "alibabacloud-credentials-api"
          "alibabacloud-endpoint-util"
          "alibabacloud-gateway-dingtalk"
          "alibabacloud-gateway-spi"
          "alibabacloud-tea"
        ] (_: null)
      );

  pythonPackageOverrides = prebuiltOverrides;

  pythonSet =
    (callPackage pyproject-nix.build.packages {
      python = python312;
    }).overrideScope
      (
        lib.composeManyExtensions [
          pyproject-build-systems.overlays.default
          overlay
          buildSystemOverrides
          pythonPackageOverrides
          # ``setup.py`` permits wheel/sdist creation only from the sealed
          # Hermes derivation. This is deliberately a derivation environment
          # variable, not a devShell variable: ``nix develop -c uv build``
          # must remain blocked.
          (final: prev: {
            hermes-agent = prev.hermes-agent.overrideAttrs (_old: {
              HERMES_NIX_BUILD = "1";
            });
          })
        ]
      );

  # The editable venv points at the live checkout, so it uses an
  # UNFILTERED workspace rooted at a real path — mkEditablePyprojectOverlay
  # computes relative paths via lib.path.splitRoot, which rejects the
  # filtered pythonSrc (a cleanSourceWith set, not a path).  Filtering
  # buys nothing here anyway: the editable install reads from
  # $HERMES_PYTHON_SRC_ROOT at runtime.
  workspaceRoot = ./..;
  editableWorkspace = uv2nix.lib.workspace.loadWorkspace { inherit workspaceRoot; };
  editableOverlay = editableWorkspace.mkEditablePyprojectOverlay {
    root = "$HERMES_PYTHON_SRC_ROOT"; # resolved at shellHook time
  };

  editableSet = pythonSet.overrideScope (
    lib.composeManyExtensions [
      editableOverlay
      (final: prev: {
        hermes-agent = prev.hermes-agent.overrideAttrs (old: {
          # point straight at the real source instead of the filtered nix store copy
          src = workspaceRoot;
          nativeBuildInputs = old.nativeBuildInputs ++ final.resolveBuildSystem { editables = [ ]; };
        });
      })
    ]
  );
in
{
  venv = pythonSet.mkVirtualEnv "hermes-agent-env" {
    hermes-agent = dependency-groups;
  };
  editableVenv = editableSet.mkVirtualEnv "hermes-agent-editable-env" {
    hermes-agent = dependency-groups;
  };
}
