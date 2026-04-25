SmartPay India License Backend - PostgreSQL Version

1. In Render web service environment variables, set DATABASE_URL to the INTERNAL DATABASE URL of smartpay-license-db.
2. Also set ADMIN_PASSWORD, SECRET_KEY, and ADMIN_KEY.
3. Replace app.py, requirements.txt, and Procfile in GitHub with these files.
4. Commit changes and redeploy Render.
5. Open /health and confirm database=postgresql and db_status=ok.
6. Open /admin, generate a test license, refresh/redeploy, and verify the license remains.
