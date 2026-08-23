# nix/orion-agent.nix — Overridable Orion Agent package
#
# callPackage auto-wires nixpkgs args; flake inputs are passed explicitly.
# Users override via:
#   pkgs.orion-agent.override { extraPythonPackages = [...]; }
#   pkgs.orion-agent.override { extraDependencyGroups = [ "hindsight" ]; }
{
  lib,
  stdenv,
  makeWrapper,
  callPackage,
  python312,
  electron,
  ripgrep,
  git,
  openssh,
  ffmpeg,
  tirith,

  # linux-only deps
  wl-clipboard,
  xclip,

  # linux-only dev deps
  cage,

  # Flake inputs — passed explicitly by packages.nix and overlays.nix
  uv2nix,
  pyproject-nix,
  pyproject-build-systems,
  npm-lockfile-fix,
  # Locked git revision of the flake source — embedded so banner.py can
  # check for updates without needing a local .git directory. Null for
  # impure / dirty builds where flakes can't determine a rev.
  rev ? null,
  # Overridable parameters
  extraPythonPackages ? [ ],
  extraDependencyGroups ? [ ],
}:
let
  mkOrionVenv =
    extraDependencyGroups:
    callPackage ./python.nix {
      inherit uv2nix pyproject-nix pyproject-build-systems;
      pythonSrc = orionNpmLib.pythonSrc;
      dependency-groups = [ "all" ] ++ extraDependencyGroups;
    };

  orionVenv = (mkOrionVenv extraDependencyGroups).venv;

  orionNpmLib = callPackage ./lib.nix {
    inherit npm-lockfile-fix;
  };

  orionTui = callPackage ./tui.nix {
    inherit orionNpmLib;
  };

  orionWeb = callPackage ./web.nix {
    inherit orionNpmLib;
  };

  bundledSkills = lib.cleanSourceWith {
    src = ../skills;
    filter = path: _type: !(lib.hasInfix "/index-cache/" path) && !(lib.hasInfix "/__pycache__/" path);
  };

  # Optional skills are NOT in the wheel (pythonSrc excludes them, see
  # lib.nix) — the wrapper exposes them via ORION_OPTIONAL_SKILLS, the
  # same mechanism Homebrew packaging uses.
  bundledOptionalSkills = lib.cleanSourceWith {
    src = ../optional-skills;
    filter = path: _type: !(lib.hasInfix "/index-cache/" path) && !(lib.hasInfix "/__pycache__/" path);
  };

  # Import bundled plugins (memory, context_engine, platforms/*).  Keeping
  # them out of the Python site-packages keeps import semantics identical
  # to a dev checkout — the loader reads them from ORION_BUNDLED_PLUGINS.
  bundledPlugins = lib.cleanSourceWith {
    src = ../plugins;
    filter = path: _type: !(lib.hasInfix "/__pycache__/" path);
  };

  # i18n locale catalogs (locales/*.yaml). Shipped into the store and pointed
  # at by ORION_BUNDLED_LOCALES so the wrapped binary always resolves human
  # strings instead of raw i18n keys (#23943 / #27632 / #35374).
  bundledLocales = lib.cleanSource ../locales;

  # Shipped MCP catalog (optional-mcps/<name>/manifest.yaml). Same bare-data-dir
  # case as locales: not a Python package, so it's symlinked into the store and
  # exposed via ORION_OPTIONAL_MCPS.
  bundledOptionalMcps = lib.cleanSourceWith {
    src = ../optional-mcps;
    filter = path: _type: !(lib.hasInfix "/__pycache__/" path);
  };

  runtimeDeps = [
    orionNpmLib.nodejs
    ripgrep
    git
    openssh
    ffmpeg
    tirith
  ]
  ++ lib.optionals stdenv.isLinux [
    wl-clipboard
    xclip
  ];

  runtimePath = lib.makeBinPath runtimeDeps;

  sitePackagesPath = python312.sitePackages;

  # Walk propagatedBuildInputs to include transitive Python deps in PYTHONPATH.
  # Without this, a plugin listing e.g. requests as a dep would fail at runtime
  # if requests isn't already in the sealed uv2nix venv.
  allExtraPythonPackages = python312.pkgs.requiredPythonModules extraPythonPackages;

  pythonPath = lib.makeSearchPath sitePackagesPath allExtraPythonPackages;

  checkPackageCollisions = ''
    import pathlib, sys, re

    def canonical(name):
        return re.sub(r'[-_.]+', '-', name).lower()

    # Collect core venv package names
    core = set()
    venv_sp = pathlib.Path('${orionVenv}/${sitePackagesPath}')
    for di in venv_sp.glob('*.dist-info'):
        meta = di / 'METADATA'
        if meta.exists():
            for line in meta.read_text().splitlines():
                if line.startswith('Name:'):
                    core.add(canonical(line.split(':', 1)[1].strip()))
                    break

    # Check each extra package for collisions
    extras_dirs = [${lib.concatMapStringsSep ", " (p: "'${toString p}'") allExtraPythonPackages}]
    for edir in extras_dirs:
        sp = pathlib.Path(edir) / '${sitePackagesPath}'
        if not sp.exists():
            continue
        for di in sp.glob('*.dist-info'):
            meta = di / 'METADATA'
            if not meta.exists():
                continue
            for line in meta.read_text().splitlines():
                if line.startswith('Name:'):
                    pkg = canonical(line.split(':', 1)[1].strip())
                    if pkg in core:
                        print(f'ERROR: plugin package \"{pkg}\" collides with a package in orion sealed venv', file=sys.stderr)
                        print(f'  from: {di}', file=sys.stderr)
                        print(f'  Remove this dependency from extraPythonPackages.', file=sys.stderr)
                        sys.exit(1)
                    break

    print('No collisions found.')
  '';
in
stdenv.mkDerivation (finalAttrs: {
  pname = "orion-agent";
  version = (fromTOML (builtins.readFile ../pyproject.toml)).project.version;

  dontUnpack = true;
  dontBuild = true;
  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall

    # Symlinks, not copies: these are all store paths already, and the
    # wrapper env vars just hold paths.  Symlinking keeps this derivation
    # near-instant when only the venv changed, with an identical closure.
    mkdir -p $out/share/orion-agent $out/bin
    ln -s ${bundledSkills} $out/share/orion-agent/skills
    ln -s ${bundledOptionalSkills} $out/share/orion-agent/optional-skills
    ln -s ${bundledPlugins} $out/share/orion-agent/plugins
    ln -s ${bundledLocales} $out/share/orion-agent/locales
    ln -s ${bundledOptionalMcps} $out/share/orion-agent/optional-mcps
    ln -s ${orionWeb} $out/share/orion-agent/web_dist
    ln -s ${orionTui}/lib/orion-tui $out/ui-tui

    ${lib.concatMapStringsSep "\n"
      (name: ''
        makeWrapper ${orionVenv}/bin/${name} $out/bin/${name} \
          --suffix PATH : "${runtimePath}" \
          --set ORION_BUNDLED_SKILLS $out/share/orion-agent/skills \
          --set ORION_OPTIONAL_SKILLS $out/share/orion-agent/optional-skills \
          --set ORION_BUNDLED_PLUGINS $out/share/orion-agent/plugins \
          --set ORION_BUNDLED_LOCALES $out/share/orion-agent/locales \
          --set ORION_OPTIONAL_MCPS $out/share/orion-agent/optional-mcps \
          --set ORION_WEB_DIST $out/share/orion-agent/web_dist \
          --set ORION_TUI_DIR $out/ui-tui \
          --set-default ORION_BIN $out/bin/orion \
          --set ORION_PYTHON ${orionVenv}/bin/python3 \
          --set ORION_NODE ${lib.getExe orionNpmLib.nodejs}${
            # Fold the line continuation INTO the optionalString: a bare
            # `\` on the line above an empty expansion would dangle onto a
            # blank line, ending the makeWrapper command early and running
            # the next flag as its own shell command (`--suffix: command
            # not found`). Only reproduces when rev == null (dirty trees).
            lib.optionalString (rev != null) " \\\n          --set ORION_REVISION ${rev}"
          }${
            lib.optionalString (
              extraPythonPackages != [ ]
            ) " \\\n          --suffix PYTHONPATH : \"${pythonPath}\""
          }
      '')
      [
        "orion"
        "orion-agent"
        "orion-acp"
      ]
    }

    ${lib.optionalString (extraPythonPackages != [ ]) ''
      echo "=== Checking for plugin/core package collisions ==="
      ${orionVenv}/bin/python3 -c "${checkPackageCollisions}"
      echo "=== No collisions ==="
    ''}

    runHook postInstall
  '';

  passthru =
    let
      devPython = (mkOrionVenv (extraDependencyGroups ++ [ "dev" ])).editableVenv;
    in
    {
      inherit
        orionTui
        orionWeb
        orionNpmLib
        orionVenv
        ;

      # `orionDesktop` references `finalAttrs.finalPackage` (this whole
      # derivation, after all overrides are applied) so the desktop wrapper
      # can prepend its `/bin` to PATH.  The desktop's resolver step 4
      # ("existing orion on PATH") then picks up the fully wrapped
      # `orion` binary — venv with all deps, bundled skills/plugins,
      # runtime PATH (ripgrep/git/ffmpeg/etc).  No re-implementation
      # of the agent resolution in the desktop wrapper.
      orionDesktop = callPackage ./desktop.nix {
        inherit orionNpmLib electron;
        orionAgent = finalAttrs.finalPackage;
      };

      devShellHook = ''
        export ORION_PYTHON=${devPython}/bin/python3
      '';

      devDeps =
        runtimeDeps
        ++ [
          devPython
        ]
        ++ lib.optionals stdenv.isLinux [
          cage # for running e2e tests without popping windows
        ];
    };

  meta = with lib; {
    description = "AI agent with advanced tool-calling capabilities";
    homepage = "https://github.com/zacharyjleach-stack/Aries";
    mainProgram = "orion";
    license = licenses.mit;
    platforms = platforms.unix;
  };
})
