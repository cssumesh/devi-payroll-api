SmartPay India Render License Backend

Files:
- app.py
- requirements.txt
- Procfile

Deploy:
1. Replace files in GitHub repo cssumesh/devi-payroll-api.
2. Important: file name must be Procfile, not Procfile.txt.
3. Commit changes.
4. Render > Manual Deploy > Deploy latest commit.
5. Open:
   https://devi-payroll-api.onrender.com/admin

Default admin password:
admin123

Recommended Render Environment Variables:
ADMIN_PASSWORD = your-new-password
SECRET_KEY = any-long-random-text
ADMIN_KEY = any-long-random-text

API endpoints:
GET  /
GET  /health
GET  /api/health
POST /api/license/activate
POST /api/license/verify
Legacy:
POST /activate
POST /validate
