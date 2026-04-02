# NetTrack — Backend API

FastAPI + PostgreSQL backend for the NetTrack network device inventory,
with JWT auth, role-based access control, SSO (OIDC), and audit logging.

---

## Requirements

- Python 3.11+
- PostgreSQL 14+

---

## 1. PostgreSQL setup

```sql
CREATE USER nettrack WITH PASSWORD 'changeme';
CREATE DATABASE nettrack OWNER nettrack;
```

---

## 2. Install dependencies

```bash
cd nettrack/
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 3. Configure environment

Create a `.env` file:

```env
# Database
DATABASE_URL=postgresql://nettrack:changeme@localhost:5432/nettrack

# JWT — generate with: openssl rand -hex 32
SECRET_KEY=your-secret-key-here
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=7

# SSO / OIDC (optional — remove if not using SSO)
OIDC_CLIENT_ID=your-client-id
OIDC_CLIENT_SECRET=your-client-secret
OIDC_DISCOVERY_URL=https://login.microsoftonline.com/{tenant}/v2.0
OIDC_REDIRECT_URI=https://nettrack.yourcompany.com/api/auth/callback
OIDC_DEFAULT_ROLE=read_only
FRONTEND_URL=https://nettrack.yourcompany.com
```

**OIDC discovery URLs by provider:**
| Provider | URL |
|----------|-----|
| Azure AD | `https://login.microsoftonline.com/{tenant-id}/v2.0` |
| Okta | `https://{your-domain}.okta.com/oauth2/default` |
| Google Workspace | `https://accounts.google.com` |

---

## 4. Create the first admin user

Run this once after the DB is set up:

```bash
python << 'PYEOF'
from database import SessionLocal
import models, auth
models.Base.metadata.create_all(bind=auth.engine if hasattr(auth, "engine") else __import__("database").engine)
db = SessionLocal()
admin = models.User(
    email="admin@yourcompany.com",
    full_name="Admin",
    role="admin",
    auth_provider="local",
    is_active=True,
    password_hash=auth.hash_password("changeme"),
)
db.add(admin)
db.commit()
print("Admin user created")
PYEOF
```

---

## 5. Run

**Development:**
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

**Production (systemd):**

```ini
# /etc/systemd/system/nettrack.service
[Unit]
Description=NetTrack API
After=network.target postgresql.service

[Service]
User=www-data
WorkingDirectory=/opt/nettrack
EnvironmentFile=/opt/nettrack/.env
ExecStart=/opt/nettrack/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## 6. Nginx reverse proxy

```nginx
server {
    listen 80;
    server_name nettrack.yourcompany.internal;

    location /api/ {
        proxy_pass       http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location / {
        root /opt/nettrack/frontend;
        try_files $uri $uri/ /index.html;
    }
}
```

---

## Role permissions

| Action | read_only | modify | admin |
|--------|-----------|--------|-------|
| View devices / switches | ✓ | ✓ | ✓ |
| Create / edit devices | — | ✓ | ✓ |
| Delete devices | — | — | ✓ |
| Manage users | — | — | ✓ |
| View audit log | — | — | ✓ |

---

## API endpoints

### Auth
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/login` | Local login → returns JWT pair |
| POST | `/api/auth/refresh` | Refresh access token |
| POST | `/api/auth/logout` | Revoke refresh token |
| GET | `/api/auth/sso/login` | Redirect to OIDC provider |
| GET | `/api/auth/callback` | OIDC callback handler |

### Users (admin only)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/users` | List all users |
| GET | `/api/users/me` | Current user profile |
| POST | `/api/users` | Create user |
| PUT | `/api/users/{id}` | Update user / role / password |
| DELETE | `/api/users/{id}` | Delete user |

### Devices
| Method | Path | Role required |
|--------|------|---------------|
| GET | `/api/devices` | read_only |
| POST | `/api/devices` | modify |
| PUT | `/api/devices/{id}` | modify |
| DELETE | `/api/devices/{id}` | admin |
| POST | `/api/devices/bulk-upsert` | modify |

### Audit log (admin only)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/audit` | Query audit log (`?resource=`, `?user_id=`, `?action=`) |

---

## Wiring the frontend

**Login:**
```js
const res = await fetch('/api/auth/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  body: new URLSearchParams({ username: email, password }),
});
const { access_token, refresh_token } = await res.json();
localStorage.setItem('access_token', access_token);
```

**Authenticated requests:**
```js
const res = await fetch('/api/devices', {
  headers: { Authorization: `Bearer ${localStorage.getItem('access_token')}` },
});
```

**SSO login button:**
```js
window.location.href = '/api/auth/sso/login';
```


---

## Database migrations (Alembic)

NetTrack uses Alembic for all schema changes. Never edit the database by hand.

### First-time setup

After creating your PostgreSQL database and setting `DATABASE_URL`, apply the
baseline migration to create all tables:

```bash
alembic upgrade head
```

That's it — no need to run the old `create_all` bootstrap script.

### Everyday workflow

| Task | Command |
|------|---------|
| Apply all pending migrations | `alembic upgrade head` |
| Roll back the last migration | `alembic downgrade -1` |
| Roll back to a specific revision | `alembic downgrade 0001` |
| Show current revision | `alembic current` |
| Show migration history | `alembic history --verbose` |

### Adding a new migration (e.g. adding a column)

1. Edit your SQLAlchemy model in `models.py`
2. Auto-generate the migration:
   ```bash
   alembic revision --autogenerate -m "add serial_number to devices"
   ```
3. Review the generated file in `alembic/versions/` — always check it before applying
4. Apply it:
   ```bash
   alembic upgrade head
   ```

### Generating SQL without applying (for review or staging)

```bash
alembic upgrade head --sql > pending_migration.sql
```

### Stamping an existing database

If you already have tables created by the old `create_all()` and want to bring
them under Alembic management without re-creating them:

```bash
alembic stamp 0001
```

This marks the database as being at revision `0001` without running any SQL.
From that point forward, use `alembic upgrade head` for all changes.
