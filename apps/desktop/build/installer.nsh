!macro customInstall
  ; The installer destination directory is $INSTDIR.
  ; We write HERMES_HOME to the HKCU Environment registry so that main.ts resolveHermesHome picks it up.
  ; We only do this if it's not already set, to avoid overriding user's manual configuration.
  ReadRegStr $0 HKCU "Environment" "HERMES_HOME"
  ${If} $0 == ""
    WriteRegStr HKCU "Environment" "HERMES_HOME" "$INSTDIR\hermes-home"
  ${EndIf}
!macroend

!macro customUnInstall
  ; On uninstall, we clean up the environment variable if it matches what we set.
  ReadRegStr $0 HKCU "Environment" "HERMES_HOME"
  ${If} $0 == "$INSTDIR\hermes-home"
    DeleteRegValue HKCU "Environment" "HERMES_HOME"
  ${EndIf}
!macroend
