@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist .env copy .env.example .env
echo Starting Job Search Automation System at http://127.0.0.1:8000 ...
.venv\Scripts\python run.py
