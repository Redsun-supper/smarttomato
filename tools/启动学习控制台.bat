@echo off
chcp 65001 >nul
title 学习控制台 - smarttomato
echo 正在启动学习控制台...
echo 启动后请在浏览器打开: http://127.0.0.1:8790
echo 关闭本窗口即停止面板（板子上的脚本不受影响）
echo.
"C:\Users\student\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe" "%~dp0learn_panel.py"
pause
