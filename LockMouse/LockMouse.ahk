; 按 F8 锁定/解锁鼠标当前位置（AutoHotkey v1.1）
; 需要以 AutoHotkeyU64.exe 运行本脚本
Locked := false
x := 0
y := 0

F8::
  global Locked, x, y
  Locked := !Locked
  if Locked {
    MouseGetPos, x, y
    SetTimer, Lock, 20
  } else {
    SetTimer, Lock, Off
  }
return

Lock:
  global Locked, x, y
  if Locked
    MouseMove, %x%, %y%, 0
return

; 按 Win+Esc 退出脚本
#Esc::ExitApp
