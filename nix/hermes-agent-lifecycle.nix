{ configMergeScript
, lib
, pkgs
, ...
}:
{
  mkNativeLifecycle =
    { name
    , cfg
    , package
    , configFile ? null
    , configYamlMode ? "0640"
    , description ? "Hermes Agent ${name} gateway"
    , includeHome ? false
    , managedMode ? "0640"
    , protectHome ? "read-only"
    , readOnlyState ? false
    , setupAfter ? [ "users" ]
    , extraActivation ? ""
    , extraUserAttrs ? { }
    , enableService ? true
    , effectiveWorkingDirectory ? cfg.workingDirectory
    }:
    let
      generatedConfigFile = pkgs.writeText "${name}-config.yaml" (
        builtins.toJSON (lib.recursiveUpdate
          { terminal.cwd = effectiveWorkingDirectory; }
          cfg.settings)
      );
      documentDerivation = pkgs.linkFarm "${name}-documents" (
        lib.mapAttrsToList (documentName: value: {
          name = documentName;
          path =
            if builtins.isPath value || lib.isStorePath value
            then value
            else pkgs.writeText "${name}-document" value;
        }) cfg.documents
      );
      envFileContent = lib.concatStringsSep "\n"
        (lib.mapAttrsToList (key: value: "${key}=${lib.escapeShellArg value}") cfg.environment);
    in
    {
      assertions = [
        {
          assertion = lib.hasPrefix "/" cfg.stateDir;
          message = "${name}: stateDir must be absolute.";
        }
        {
          assertion = lib.hasPrefix "/" cfg.workingDirectory;
          message = "${name}: workingDirectory must be absolute.";
        }
        {
          assertion = lib.all (documentName:
            builtins.match "^[^/]+$" documentName != null
            && documentName != "."
            && documentName != ".."
          ) (builtins.attrNames cfg.documents);
          message = "${name}: document names must be plain filenames without path separators.";
        }
      ];

      users.groups = lib.mkIf cfg.createUser {
        ${cfg.group} = { };
      };

      users.users = lib.mkIf cfg.createUser {
        ${cfg.user} = {
          isSystemUser = true;
          group = cfg.group;
          home = cfg.stateDir;
          createHome = true;
          shell = pkgs.bashInteractive;
        } // extraUserAttrs;
      };

      systemd.tmpfiles.rules = [
        "d ${cfg.stateDir}                2770 ${cfg.user} ${cfg.group} - -"
        "d ${cfg.stateDir}/.hermes        2770 ${cfg.user} ${cfg.group} - -"
        "d ${cfg.stateDir}/.hermes/cron   2770 ${cfg.user} ${cfg.group} - -"
        "d ${cfg.stateDir}/.hermes/sessions 2770 ${cfg.user} ${cfg.group} - -"
        "d ${cfg.stateDir}/.hermes/logs   2770 ${cfg.user} ${cfg.group} - -"
        "d ${cfg.stateDir}/.hermes/memories 2770 ${cfg.user} ${cfg.group} - -"
        "d ${cfg.stateDir}/.hermes/plugins 2770 ${cfg.user} ${cfg.group} - -"
      ]
      ++ lib.optional includeHome "d ${cfg.stateDir}/home           0750 ${cfg.user} ${cfg.group} - -"
      ++ [
        "d ${cfg.workingDirectory}         2770 ${cfg.user} ${cfg.group} - -"
      ];

      system.activationScripts."${name}-setup" = lib.stringAfter setupAfter ''
        state_dir=${lib.escapeShellArg cfg.stateDir}
        hermes_dir=${lib.escapeShellArg "${cfg.stateDir}/.hermes"}
        working_dir=${lib.escapeShellArg cfg.workingDirectory}
        ${lib.optionalString includeHome ''
          home_dir=${lib.escapeShellArg "${cfg.stateDir}/home"}
        ''}

        reject_symlink() {
          if [ -L "$1" ]; then
            echo "hermes-agent: refusing to follow symlink $1 during activation" >&2
            exit 1
          fi
        }

        reject_symlink "$state_dir"
        reject_symlink "$hermes_dir"
        reject_symlink "$working_dir"
        ${lib.optionalString includeHome ''
          reject_symlink "$home_dir"
        ''}

        mkdir -p "$hermes_dir" "$working_dir"
        ${lib.optionalString includeHome ''
          mkdir -p "$home_dir"
        ''}
        chown ${lib.escapeShellArg cfg.user}:${lib.escapeShellArg cfg.group} \
          "$state_dir" "$hermes_dir" "$working_dir"
        chmod 2770 "$state_dir" "$hermes_dir" "$working_dir"
        ${lib.optionalString includeHome ''
          chown ${lib.escapeShellArg cfg.user}:${lib.escapeShellArg cfg.group} "$home_dir"
          chmod 0750 "$home_dir"
        ''}

        find "$hermes_dir" -maxdepth 1 \
          \( -name "*.db" -o -name "*.db-wal" -o -name "*.db-shm" -o -name "SOUL.md" \) \
          -exec chmod g+rw {} + 2>/dev/null || true
        for _subdir in cron sessions logs memories plugins; do
          mkdir -p "$hermes_dir/$_subdir"
          chown ${lib.escapeShellArg cfg.user}:${lib.escapeShellArg cfg.group} \
            "$hermes_dir/$_subdir"
          chmod 2770 "$hermes_dir/$_subdir"
          find "$hermes_dir/$_subdir" -type f \
            -exec chmod g+rw {} + 2>/dev/null || true
        done

        config_path=${lib.escapeShellArg "${cfg.stateDir}/.hermes/config.yaml"}
        reject_symlink "$config_path"
        ${if configFile != null then ''
          install -o ${lib.escapeShellArg cfg.user} -g ${lib.escapeShellArg cfg.group} \
            -m ${configYamlMode} ${lib.escapeShellArg (toString configFile)} \
            "$config_path"
        '' else ''
          ${configMergeScript} ${lib.escapeShellArg (toString generatedConfigFile)} \
            "$config_path"
          chown ${lib.escapeShellArg cfg.user}:${lib.escapeShellArg cfg.group} \
            "$config_path"
          chmod ${configYamlMode} "$config_path"
        ''}

        managed_path=${lib.escapeShellArg "${cfg.stateDir}/.hermes/.managed"}
        reject_symlink "$managed_path"
        touch "$managed_path"
        chown ${lib.escapeShellArg cfg.user}:${lib.escapeShellArg cfg.group} \
          "$managed_path"
        chmod ${managedMode} "$managed_path"

        ${lib.optionalString (cfg.environment != { } || cfg.environmentFiles != [ ]) ''
          ENV_FILE=${lib.escapeShellArg "${cfg.stateDir}/.hermes/.env"}
          reject_symlink "$ENV_FILE"
          install -o ${lib.escapeShellArg cfg.user} -g ${lib.escapeShellArg cfg.group} \
            -m 0640 /dev/null "$ENV_FILE"
          printf '%s\n' ${lib.escapeShellArg envFileContent} > "$ENV_FILE"
          ${lib.concatStringsSep "\n" (map (file: ''
            if [ -f ${lib.escapeShellArg file} ]; then
              echo "" >> "$ENV_FILE"
              cat ${lib.escapeShellArg file} >> "$ENV_FILE"
            fi
          '') cfg.environmentFiles)}
        ''}

        ${lib.concatStringsSep "\n" (lib.mapAttrsToList (documentName: _value: ''
          document_path=${lib.escapeShellArg "${cfg.workingDirectory}/${documentName}"}
          reject_symlink "$document_path"
          install -o ${lib.escapeShellArg cfg.user} -g ${lib.escapeShellArg cfg.group} -m 0640 \
            ${lib.escapeShellArg "${documentDerivation}/${documentName}"} \
            "$document_path"
        '') cfg.documents)}

        ${extraActivation}
      '';

      systemd.services = lib.optionalAttrs enableService {
        ${name} = {
          inherit description;
          wantedBy = [ "multi-user.target" ];
          after = [ "network-online.target" ];
          wants = [ "network-online.target" ];
          environment = {
            HOME = cfg.stateDir;
            HERMES_HOME = "${cfg.stateDir}/.hermes";
            HERMES_MANAGED = "true";
          } // lib.optionalAttrs (cfg.allowedToolsets != null) {
            HERMES_ALLOWED_TOOLSETS = lib.concatStringsSep "," cfg.allowedToolsets;
          };
          serviceConfig = {
            User = cfg.user;
            Group = cfg.group;
            WorkingDirectory = cfg.workingDirectory;
            ExecStart = lib.escapeShellArgs ([ "${package}/bin/hermes" "gateway" ] ++ cfg.extraArgs);
            Restart = cfg.restart;
            RestartSec = cfg.restartSec;
            UMask = "0007";
            NoNewPrivileges = true;
            ProtectSystem = "strict";
            ProtectHome = protectHome;
            ReadWritePaths = [ cfg.stateDir cfg.workingDirectory ];
            ReadOnlyPaths =
              lib.optional readOnlyState "${cfg.stateDir}/.hermes/config.yaml"
              ++ lib.optional (readOnlyState && (cfg.environment != { } || cfg.environmentFiles != [ ])
                ) "${cfg.stateDir}/.hermes/.env";
            PrivateTmp = true;
          };
          path = [
            package
            pkgs.bash
            pkgs.coreutils
            pkgs.git
          ] ++ cfg.extraPackages;
        };
      };
    };
}
