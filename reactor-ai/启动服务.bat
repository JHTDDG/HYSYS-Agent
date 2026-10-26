@echo off
chcp 65001 >nul
title AI 化工反应器智能建模
cd /d %~dp0
echo ============================================
echo   AI 化工反应器智能建模 - 启动服务
echo   启动后浏览器打开 http://127.0.0.1:8000
echo ============================================
E:\anaconda3\envs\test_ai\python.exe -m app.main
pause
