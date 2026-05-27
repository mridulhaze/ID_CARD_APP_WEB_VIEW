# ID Card App (Web View)
<img width="1351" height="577" alt="image" src="https://github.com/user-attachments/assets/b466a2cf-0a6a-418e-88fb-ee0e561d7eef" />
<img width="1365" height="593" alt="image" src="https://github.com/user-attachments/assets/d03fca55-857a-45c9-8097-f8c83a55a28d" />
<img width="1339" height="587" alt="image" src="https://github.com/user-attachments/assets/6979ecde-1140-43f8-b2ff-ad6dfa5841a6" />




Simple Flask-based ID card management web app that uses SQL Server for storage.

## Requirements

- Python 3.10+ (3.8+ should also work)
- SQL Server (local or remote)
- Python packages: see `requirements.txt`

## Quick start (development)

1. Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Configure database connection (SQL Server):

- By default the app tries common local SQL Server instance names.
- To force a specific instance, set `FORCE_SERVER` at the top of `app.py`, e.g.:

```py
# FORCE_SERVER = r"MY-PC\SQLEXPRESS"
FORCE_SERVER = None
```

Or set the value to the exact instance name before starting the app.

4. Run the app:

```powershell
python app.py
```

The app will initialize the database tables automatically on first run.

## CI

A basic GitHub Actions workflow is included at `.github/workflows/ci.yml` that installs dependencies and performs a syntax check via `compileall`.

## Notes

- The project uses `pyodbc` to connect to SQL Server; ensure the appropriate ODBC driver is installed on your platform.
- Secrets and production configuration (e.g., secret keys, production DB credentials) are not stored here — configure them in your deployment environment.
# ID_CARD_APP_WEB_VIEW
simple id card generate and edit and export in various format with automatic all user create with credentials in python
