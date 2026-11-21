; ClipForge NSIS installer hooks (Tauri v2 `bundle.windows.nsis.installerHooks`).
;
; Why this file exists
; --------------------
; The ClipForge desktop app ships a sidecar server, clipforge-server.exe,
; next to the main app exe. The app hides to the tray on close, so both
; processes can still be running when the user runs a new installer. A
; running exe is locked by Windows, so the installer then fails with the
; raw NSIS error about a file that cannot be opened for writing
; (seen on the v0.8.0 install, for `...\clipforge-server.exe`).
;
; Tauri's own NSIS template only guards the *main* binary: it runs
; `CheckIfAppIsRunning "$INSTDIR\${MAINBINARYNAME}.exe"` just before
; copying files, but it never checks the externalBin sidecar. This hook
; closes that gap.
;
; Mechanism
; ---------
; Tauri v2's bundler (`tauri-bundler` 2.x, `bundle > windows > nsis >
; installerHooks`) `!include`s this file into the generated installer.nsi
; and expands `NSIS_HOOK_PREINSTALL` inside `Section Install` *before*
; the first `File` instruction, and `NSIS_HOOK_PREUNINSTALL` inside
; `Section Uninstall` *before* the first `Delete`. So no file is ever
; written or removed before the processes below are gone.
;
; For each process the hook:
;   1. runs `taskkill /F /IM <name> /T` (force-kill the whole tree) and
;      ignores the result — exit code 128 just means "not running", which
;      is the normal case on a fresh install;
;   2. polls `tasklist` for up to ~10 seconds until the image is gone;
;   3. if it is STILL running after that, shows a clear message telling
;      the user how to end it and aborts the install *before anything
;      has been written* — so the install never ends up half-installed
;      and the user never sees the raw NSIS dialog about a file that
;      cannot be opened for writing.
;
; Only built-in NSIS instructions and the stock nsExec plugin (shipped
; with NSIS 3.x, which Tauri downloads) are used, so there is no extra
; dependency. Plain labels/Goto are used instead of LogicLib so the hook
; does not depend on which headers the generated template includes.
;
; Process names killed:
;   - clipforge-server.exe  — the sidecar server (the file that locked
;     the v0.8.0 install).
;   - clipforge-desktop.exe — the main app exe as built (the Cargo
;     binary name; Tauri copies it under this name into $INSTDIR).
;   - ClipForge.exe         — the product-name spelling, killed as well
;     so the hook still works if the main binary is ever renamed to the
;     product name. Killing a process that is not running is harmless.

; CLIPFORGE_STOP_PROCESS <image-name> <tag>
;   Force-kills <image-name> (if running), then waits up to ~10 seconds
;   for it to disappear from the task list. Aborts the install/uninstall
;   with a clear message if it is still running afterwards. <tag> must be
;   unique per expansion so the labels do not collide.
!macro CLIPFORGE_STOP_PROCESS IMAGE_NAME TAG
  Push $0
  Push $R0
  Push $R1

  DetailPrint "ClipForge: stopping ${IMAGE_NAME} (if running) so locked files can be replaced..."
  nsExec::Exec 'taskkill /F /IM ${IMAGE_NAME} /T'
  Pop $0 ; taskkill exit code: 0 = killed, 128 = not running; both are fine.

  ; Wait up to 20 x 500 ms for the process to disappear. `find` exits 0
  ; when the image name is still listed by tasklist, 1 when it is gone.
  StrCpy $R0 0
  clipforge_wait_${TAG}:
    nsExec::Exec 'cmd /c tasklist /FI "IMAGENAME eq ${IMAGE_NAME}" /NH | find /I "${IMAGE_NAME}" >NUL'
    Pop $R1
    StrCmp $R1 "1" clipforge_gone_${TAG}
    IntOp $R0 $R0 + 1
    ; $R0 < 20 -> sleep and retry; $R0 >= 20 -> give up with a message.
    IntCmp $R0 20 clipforge_sleep_${TAG} clipforge_locked_${TAG} clipforge_locked_${TAG}
  clipforge_sleep_${TAG}:
    Sleep 500
    Goto clipforge_wait_${TAG}
  clipforge_locked_${TAG}:
    MessageBox MB_OK|MB_ICONSTOP "ClipForge could not be updated because ${IMAGE_NAME} is still running.$\r$\n$\r$\nPlease close ClipForge (right-click its tray icon and choose Quit) or end ${IMAGE_NAME} in Task Manager (Details tab), then run the installer again."
    Abort ; Nothing has been written yet, so the install is cleanly cancelled.
  clipforge_gone_${TAG}:

  Pop $R1
  Pop $R0
  Pop $0
!macroend

; Runs before the installer copies any file, sets any registry key, or
; creates any shortcut. Kill the sidecar first: it is the process that
; holds clipforge-server.exe open.
!macro NSIS_HOOK_PREINSTALL
  !insertmacro CLIPFORGE_STOP_PROCESS "clipforge-server.exe" "pre_server"
  !insertmacro CLIPFORGE_STOP_PROCESS "clipforge-desktop.exe" "pre_app"
  !insertmacro CLIPFORGE_STOP_PROCESS "ClipForge.exe" "pre_app_product"
!macroend

; Runs before the uninstaller deletes any file. Same lock problem as the
; installer: deleting a running clipforge-server.exe would silently fail
; and leave a stale exe behind.
!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro CLIPFORGE_STOP_PROCESS "clipforge-server.exe" "un_server"
  !insertmacro CLIPFORGE_STOP_PROCESS "clipforge-desktop.exe" "un_app"
  !insertmacro CLIPFORGE_STOP_PROCESS "ClipForge.exe" "un_app_product"
!macroend
