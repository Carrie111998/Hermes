{ inputs, ... }:
{
  flake.nixosModules.instances = { config, lib, pkgs, ... }:
    let
      inherit (lib) mkIf mkOption types;
      cfg = config.services.hermes-agent;
      system = pkgs.stdenv.hostPlatform.system;
      hermesAgentPackage = inputs.self.packages.${system}.default;
      configMergeScript = pkgs.callPackage ./configMergeScript.nix { };

      instanceModule = { name, ... }: {
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
            default = "/var/lib/hermes-${name}/workspace";
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

      mkInstance = name: cfg:
        let
          unitName = "hermes-agent-${name}";
          generatedConfigFile = pkgs.writeText "${unitName}-config.yaml" (
            builtins.toJSON cfg.settings
          );
          documentDerivation = pkgs.runCommand "${unitName}-documents" { } ''
            mkdir -p $out
            ${lib.concatStringsSep "\n" (lib.mapAttrsToList (documentName: value:
              if builtins.isPath value || lib.isStorePath value
              then "cp ${lib.escapeShellArg (toString value)} $out/${lib.escapeShellArg documentName}"
              else "cat > $out/${lib.escapeShellArg documentName} <<'HERMES_DOCUMENT_EOF'\n${value}\nHERMES_DOCUMENT_EOF"
            ) cfg.documents)}
          '';
          envFileContent = lib.concatStringsSep "\n" (
            lib.mapAttrsToList (key: value: "${key}=${lib.escapeShellArg value}") cfg.environment
          );
        in
        if cfg.enable then {
          assertions = [
            {
              assertion = builtins.match "^[a-z0-9][a-z0-9-]*$" name != null;
              message = "services.hermes-agent.instances.${name}: instance names must be lowercase letters, digits, and hyphens.";
            }
            {
              assertion = lib.hasPrefix "/" cfg.stateDir;
              message = "services.hermes-agent.instances.${name}.stateDir must be absolute.";
            }
            {
              assertion = lib.hasPrefix "/" cfg.workingDirectory;
              message = "services.hermes-agent.instances.${name}.workingDirectory must be absolute.";
            }
          ];

          users.groups = mkIf cfg.createUser {
            ${cfg.group} = { };
          };
          users.users = mkIf cfg.createUser {
            ${cfg.user} = {
              isSystemUser = true;
              group = cfg.group;
              home = cfg.stateDir;
              createHome = true;
              shell = pkgs.bashInteractive;
              packages = cfg.extraPackages;
            };
          };

          systemd.tmpfiles.rules = [
            "d ${cfg.stateDir} 2770 ${cfg.user} ${cfg.group} - -"
            "d ${cfg.stateDir}/.hermes 2770 ${cfg.user} ${cfg.group} - -"
            "d ${cfg.stateDir}/.hermes/cron 2770 ${cfg.user} ${cfg.group} - -"
            "d ${cfg.stateDir}/.hermes/sessions 2770 ${cfg.user} ${cfg.group} - -"
            "d ${cfg.stateDir}/.hermes/logs 2770 ${cfg.user} ${cfg.group} - -"
            "d ${cfg.stateDir}/.hermes/memories 2770 ${cfg.user} ${cfg.group} - -"
            "d ${cfg.stateDir}/.hermes/plugins 2770 ${cfg.user} ${cfg.group} - -"
            "d ${cfg.workingDirectory} 2770 ${cfg.user} ${cfg.group} - -"
          ];

          system.activationScripts."${unitName}-setup" = lib.stringAfter [ "users" ] ''
            mkdir -p ${lib.escapeShellArg cfg.stateDir}/.hermes ${lib.escapeShellArg cfg.workingDirectory}
            chown ${lib.escapeShellArg cfg.user}:${lib.escapeShellArg cfg.group} \
              ${lib.escapeShellArg cfg.stateDir} \
              ${lib.escapeShellArg "${cfg.stateDir}/.hermes"} \
              ${lib.escapeShellArg cfg.workingDirectory}
            chmod 2770 ${lib.escapeShellArg cfg.stateDir} \
              ${lib.escapeShellArg "${cfg.stateDir}/.hermes"} \
              ${lib.escapeShellArg cfg.workingDirectory}

            ${if cfg.configFile != null then ''
              install -o ${lib.escapeShellArg cfg.user} -g ${lib.escapeShellArg cfg.group} \
                -m 0640 ${lib.escapeShellArg (toString cfg.configFile)} \
                ${lib.escapeShellArg "${cfg.stateDir}/.hermes/config.yaml"}
            '' else ''
              ${configMergeScript} ${lib.escapeShellArg (toString generatedConfigFile)} \
                ${lib.escapeShellArg "${cfg.stateDir}/.hermes/config.yaml"}
              chown ${lib.escapeShellArg cfg.user}:${lib.escapeShellArg cfg.group} \
                ${lib.escapeShellArg "${cfg.stateDir}/.hermes/config.yaml"}
              chmod 0640 ${lib.escapeShellArg "${cfg.stateDir}/.hermes/config.yaml"}
            ''}

            touch ${lib.escapeShellArg "${cfg.stateDir}/.hermes/.managed"}
            chown ${lib.escapeShellArg cfg.user}:${lib.escapeShellArg cfg.group} \
              ${lib.escapeShellArg "${cfg.stateDir}/.hermes/.managed"}
            chmod 0640 ${lib.escapeShellArg "${cfg.stateDir}/.hermes/.managed"}

            ${lib.optionalString (cfg.environment != { } || cfg.environmentFiles != [ ]) ''
              env_file=${lib.escapeShellArg "${cfg.stateDir}/.hermes/.env"}
              install -o ${lib.escapeShellArg cfg.user} -g ${lib.escapeShellArg cfg.group} \
                -m 0640 /dev/null "$env_file"
              printf '%s\n' ${lib.escapeShellArg envFileContent} > "$env_file"
              ${lib.concatMapStringsSep "\n" (file: ''
                if [ -f ${lib.escapeShellArg file} ]; then
                  printf '\n' >> "$env_file"
                  cat ${lib.escapeShellArg file} >> "$env_file"
                fi
              '') cfg.environmentFiles}
            ''}

            ${lib.concatStringsSep "\n" (lib.mapAttrsToList (documentName: _value: ''
              install -o ${lib.escapeShellArg cfg.user} -g ${lib.escapeShellArg cfg.group} -m 0640 \
                ${lib.escapeShellArg "${documentDerivation}/${documentName}"} \
                ${lib.escapeShellArg "${cfg.workingDirectory}/${documentName}"}
            '') cfg.documents)}
          '';

          systemd.services.${unitName} = {
            description = "Hermes Agent ${name} gateway";
            wantedBy = [ "multi-user.target" ];
            after = [ "network-online.target" ];
            wants = [ "network-online.target" ];
            environment = {
              HOME = cfg.stateDir;
              HERMES_HOME = "${cfg.stateDir}/.hermes";
              HERMES_MANAGED = "true";
              MESSAGING_CWD = cfg.workingDirectory;
            };
            serviceConfig = {
              User = cfg.user;
              Group = cfg.group;
              WorkingDirectory = cfg.workingDirectory;
              ExecStart = lib.concatStringsSep " " (
                [ "${cfg.package}/bin/hermes" "gateway" ] ++ cfg.extraArgs
              );
              Restart = cfg.restart;
              RestartSec = cfg.restartSec;
              UMask = "0007";
              NoNewPrivileges = true;
              ProtectSystem = "strict";
              ProtectHome = "read-only";
              ReadWritePaths = [ cfg.stateDir cfg.workingDirectory ];
              PrivateTmp = true;
            };
            path = [
              cfg.package
              pkgs.bash
              pkgs.coreutils
              pkgs.git
            ] ++ cfg.extraPackages;
          };
        } else { };
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
