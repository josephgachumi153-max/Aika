@echo off
REM Starts Aika on http://localhost:8000
REM Change these before real use:
set AIKA_ADMIN_EMAIL=admin@aika.local
set AIKA_ADMIN_PASSWORD=admin123
set AIKA_BUSINESS=Aika
set AIKA_PHONE=07XX XXX XXX
set AIKA_LOCATION=Limuru, Kenya

if not exist venv (
    python -m venv venv
    call venv\Scripts\activate
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate
)
uvicorn app.main:app --host 0.0.0.0 --port 8000
