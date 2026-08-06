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

  overlay = workspace.mkPyprojectOverlay {
    sourcePreference = "wheel";
  };

  isAarch64Darwin = stdenv.hostPlatform.system == "aarch64-darwin";

  # Keep the workspace locked through uv2nix, but supply the local voice stack
  # from nixpkgs so wheel-only transitive artifacts do not break evaluation.
  mkPrebuiltPassthru = dependencies: {
    inherit dependencies;
    optional-dependencies = { };
    dependency-groups = { };
  };

  mkPrebuiltOverride =
    final: from: dependencies:
    hacks.nixpkgsPrebuilt {
      inherit from;
      prev = {
        nativeBuildInputs = [ final.pyprojectHook ];
        passthru = mkPrebuiltPassthru dependencies;
      };
    };

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

  # Packages to substitute with nixpkgs prebuilt versions.
  #
  # uv2nix builds every Python dependency from source (wheel or sdist).
  # Many of these packages already exist in nixpkgs with identical upstream
  # versions, and nixpkgs builds are cached on cache.nixos.org.  Substituting
  # them with mkPrebuiltOverride eliminates hundreds of derivations from the
  # build graph, cutting build time from hours to minutes on a fresh install.
  #
  # The overrides below apply on ALL platforms.  The original Darwin-only
  # restriction was added because some wheel-only transitive deps (e.g.
  # onnxruntime's coloredlogs) don't build from sdist on aarch64-darwin.
  # On Linux everything builds, but substituting from nixpkgs is still
  # strictly better: same upstream version, cached, no local compilation.
  #
  # Dependency lists: mkPrebuiltOverride replaces the package's passthru
  # with a minimal stub.  The `dependencies` attrset tells the venv resolver
  # which deps the prebuilt package needs.  Passing `{}` means "no deps
  # declared here" — the deps are still resolved transitively through the
  # root package (hermes-agent) and other packages in the venv.  Explicit
  # deps are only listed for packages whose deps might not be pulled in by
  # anything else in the venv (e.g. onnxruntime → coloredlogs).
  prebuiltOverrides = final: {
    # ── Native/compiled packages (slow to build from source) ──────────
    numpy = mkPrebuiltOverride final python312.pkgs.numpy { };

    av = mkPrebuiltOverride final python312.pkgs.av { };

    pillow = mkPrebuiltOverride final python312.pkgs.pillow { };

    sounddevice = mkPrebuiltOverride final python312.pkgs.sounddevice { };

    onnxruntime = mkPrebuiltOverride final python312.pkgs.onnxruntime {
      coloredlogs = [ ];
      numpy = [ ];
      packaging = [ ];
    };

    ctranslate2 = mkPrebuiltOverride final python312.pkgs.ctranslate2 {
      numpy = [ ];
      pyyaml = [ ];
    };

    faster-whisper = mkPrebuiltOverride final python312.pkgs.faster-whisper {
      av = [ ];
      ctranslate2 = [ ];
      huggingface-hub = [ ];
      onnxruntime = [ ];
      tokenizers = [ ];
      tqdm = [ ];
    };

    tokenizers = mkPrebuiltOverride final python312.pkgs.tokenizers { };

    flatbuffers = mkPrebuiltOverride final python312.pkgs.flatbuffers { };

    # ── Crypto / C extensions ─────────────────────────────────────────
    cryptography = mkPrebuiltOverride final python312.pkgs.cryptography { };

    cffi = mkPrebuiltOverride final python312.pkgs.cffi { };

    pycparser = mkPrebuiltOverride final python312.pkgs.pycparser { };

    # ── Rust-backed packages ──────────────────────────────────────────
    pydantic-core = mkPrebuiltOverride final python312.pkgs.pydantic-core { };

    jiter = mkPrebuiltOverride final python312.pkgs.jiter { };

    rpds-py = mkPrebuiltOverride final python312.pkgs.rpds-py { };

    watchfiles = mkPrebuiltOverride final python312.pkgs.watchfiles { };

    hf-xet = mkPrebuiltOverride final python312.pkgs.hf-xet { };

    # ── Cython / C extension packages ──────────────────────────────────
    msgpack = mkPrebuiltOverride final python312.pkgs.msgpack { };

    brotlicffi = mkPrebuiltOverride final python312.pkgs.brotlicffi { };

    uvloop = mkPrebuiltOverride final python312.pkgs.uvloop { };

    asyncpg = mkPrebuiltOverride final python312.pkgs.asyncpg { };

    propcache = mkPrebuiltOverride final python312.pkgs.propcache { };

    cbor2 = mkPrebuiltOverride final python312.pkgs.cbor2 { };

    ruamel-yaml-clib = mkPrebuiltOverride final python312.pkgs.ruamel-yaml-clib { };

    # ── Platform-specific (Darwin-only, kept for aarch64-darwin) ───────
    pyarrow = mkPrebuiltOverride final python312.pkgs.pyarrow { };

    humanfriendly = mkPrebuiltOverride final python312.pkgs.humanfriendly { };

    coloredlogs = mkPrebuiltOverride final python312.pkgs.coloredlogs {
      humanfriendly = [ ];
    };
  };

  pythonPackageOverrides =
    final: _prev:
    (prebuiltOverrides final)
    // lib.optionalAttrs isAarch64Darwin {
      # aarch64-darwin-only: humanfriendly/coloredlogs are already in
      # prebuiltOverrides above, but we keep this for clarity and future
      # Darwin-specific additions.
    };

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
