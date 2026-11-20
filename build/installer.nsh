; Extra uninstall step for RGB Studio's installer (electron-builder NSIS).
; The app registers a "RGB Studio" scheduled task for itself (prompt-free admin
; start). Remove it on a real uninstall, but keep it during upgrades.
!macro customUnInstall
  ${ifNot} ${isUpdated}
    nsExec::Exec 'schtasks /delete /tn "RGB Studio" /f'
    Pop $0
  ${endIf}
!macroend
