{ inputs, ... }:
{
  flake.nixosModules.instances = { config, lib, pkgs, ... }:
    let
      inherit (lib) mkOption types;
      cfg = config.services.hermes-agent;
      system = pkgs.stdenv.hostPlatform.system;
      hermesAgentPackage = inputs.self.packages.${system}.default;
      nativeLifecycle = import ./hermes-agent-lifecycle.nix {
        inherit lib pkgs;
        configMergeScript = pkgs.callPackage ./configMergeScript.nix { };
      };

      instanceModule = { config, name, ... }: {
        options = {
          enable = lib.mkEnableOption "Hermes Agent instance ${name}";

          package = mkOption {
            type = types.package;
            default = hermesAgentPackage;
            description = "Hermes package for this instance.";
          };

          user = mkOption {
            type = types.str;
            default = "hermes-${name}";
            description = "System user running this instance.";
          };

          group = mkOption {
            type = types.str;
            default = "hermes-${name}";
            description = "System group running this instance.";
          };

          createUser = mkOption {
            type = types.bool;
            default = true;
            description = "Create the instance user and group.";
          };

          stateDir = mkOption {
            type = types.str;
            default = "/var/lib/hermes-${name}";
            description = "State directory containing this instance's Hermes home.";
          };

          workingDirectory = mkOption {
            type = types.str;
            default = "${config.stateDir}/workspace";
            description = "Working directory for this instance.";
          };

          configFile = mkOption {
            type = types.nullOr types.path;
            default = null;
            description = "Existing config.yaml to install instead of generated settings.";
          };

          settings = mkOption {
            type = types.attrs;
            default = { };
            description = "Declarative Hermes settings for this instance.";
          };

          environment = mkOption {
            type = types.attrsOf types.str;
            default = { };
            description = "Non-secret environment values written to this instance's .env.";
          };

          environmentFiles = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = "KEY=value files appended to this instance's .env.";
          };

          documents = mkOption {
            type = types.attrsOf (types.either types.str types.path);
            default = { };
            description = "Workspace documents installed for this instance.";
          };

          extraPackages = mkOption {
            type = types.listOf types.package;
            default = [ ];
            description = "Packages exposed to this instance.";
          };

          extraArgs = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = "Extra arguments passed to `hermes gateway`.";
          };

          allowedToolsets = mkOption {
            type = types.nullOr (types.listOf types.str);
            default = null;
            description = "Hard allowlist of toolsets for this instance.";
          };

          readOnlyState = mkOption {
            type = types.bool;
            default = false;
            description = "Protect this instance's Nix-managed config and environment files.";
          };

          restart = mkOption {
            type = types.str;
            default = "always";
            description = "systemd Restart= policy.";
          };

          restartSec = mkOption {
            type = types.int;
            default = 5;
            description = "systemd RestartSec= value.";
          };
        };
      };

      mkInstance = name: instanceCfg:
        let
          lifecycle = nativeLifecycle.mkNativeLifecycle {
            name = "hermes-agent-${name}";
            description = "Hermes Agent ${name} gateway";
            cfg = instanceCfg;
            package = instanceCfg.package;
            configFile = instanceCfg.configFile;
            readOnlyState = instanceCfg.readOnlyState;
            extraUserAttrs = {
              packages = instanceCfg.extraPackages;
            };
          };
        in
        if instanceCfg.enable then
          lifecycle
          // {
            assertions = [
              {
                assertion = builtins.match "^[a-z0-9][a-z0-9-]*$" name != null;
                message = "services.hermes-agent.instances.${name}: instance names must be lowercase letters, digits, and hyphens.";
              }
              {
                assertion = lib.hasPrefix "/" instanceCfg.stateDir;
                message = "services.hermes-agent.instances.${name}.stateDir must be absolute.";
              }
              {
                assertion = lib.hasPrefix "/" instanceCfg.workingDirectory;
                message = "services.hermes-agent.instances.${name}.workingDirectory must be absolute.";
              }
            ] ++ (lifecycle.assertions or [ ]);
          }
        else { };

      instanceConfigs = lib.mapAttrsToList mkInstance cfg.instances;
    in
    {
      options.services.hermes-agent.instances = mkOption {
        type = types.attrsOf (types.submodule instanceModule);
        default = { };
        description = "Independent native Hermes Agent gateway instances.";
      };

      config = {
        assertions = lib.concatMap (instance: instance.assertions or [ ]) instanceConfigs;
        system = lib.mkMerge (map (instance: instance.system or { }) instanceConfigs);
        systemd = lib.mkMerge (map (instance: instance.systemd or { }) instanceConfigs);
        users = lib.mkMerge (map (instance: instance.users or { }) instanceConfigs);
      };
    };
}
