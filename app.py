from flask import Flask, render_template, request, redirect, url_for, session, flash
import pyodbc
import base64
from functools import wraps
from datetime import datetime
import socket

app = Flask(__name__)
app.secret_key = "idcard_secret_2024"

# ── SQL Server auto-detection ──────────────────────────────────────────────────
_CANDIDATE_SERVERS = [
    r"localhost\SQLEXPRESS",
    r".\SQLEXPRESS",
    r"(local)\SQLEXPRESS",
    rf"{socket.gethostname()}\SQLEXPRESS",
    r"localhost\MSSQLSERVER",
    r"localhost",
    r".",
]
_DB_NAME = "IDCARD"

def _build_conn_str(server):
    return (
        f"Driver={{SQL Server}};"
        f"Server={server};"
        f"Database={_DB_NAME};"
        f"Trusted_Connection=yes;"
    )

def _detect_server():
    for server in _CANDIDATE_SERVERS:
        try:
            conn = pyodbc.connect(_build_conn_str(server), timeout=3)
            conn.close()
            print(f"[DB] Connected using Server={server!r}")
            return server
        except pyodbc.Error:
            print(f"[DB] Failed: Server={server!r}")
    raise RuntimeError(
        "\nERROR: Could not connect to SQL Server.\n"
        "Tried: " + ", ".join(_CANDIDATE_SERVERS) + "\n"
        "Set FORCE_SERVER at the top of app.py to your exact instance name.\n"
        "Run: sqlcmd -L   to list available instances."
    )

# Set this to skip auto-detection, e.g. FORCE_SERVER = r"MY-PC\SQLEXPRESS"
FORCE_SERVER = None

_RESOLVED_SERVER = None

def get_db():
    global _RESOLVED_SERVER
    if _RESOLVED_SERVER is None:
        _RESOLVED_SERVER = FORCE_SERVER or _detect_server()
    return pyodbc.connect(_build_conn_str(_RESOLVED_SERVER))

# ── Phone normalizer ───────────────────────────────────────────────────────────
def normalize_phone(phone):
    """Normalize BD phone to 01XXXXXXXXX. Returns None if empty, raises ValueError if bad."""
    if not phone or not phone.strip():
        return None
    p = (phone.strip()
         .replace("-", "").replace(" ", "")
         .replace("(", "").replace(")", ""))
    if p.startswith("+880"):
        normalized = "0" + p[4:]
    elif p.startswith("880"):
        normalized = "0" + p[3:]
    elif p.startswith("01"):
        normalized = p
    elif p.startswith("1") and len(p) == 10:
        normalized = "0" + p
    else:
        normalized = p
    if normalized.startswith("01") and len(normalized) != 11:
        raise ValueError(
            f'Phone "{phone}" → "{normalized}" is {len(normalized)} digits, expected 11.'
        )
    return normalized

# ── User sync helper ───────────────────────────────────────────────────────────
def create_or_update_user_for_card(cur, employee_id, phone, card_id):
    """
    Safely create or update the linked user account for an id_card.
    Returns (action, username): action = 'created' | 'updated' | 'skipped'
    Raises ValueError on duplicate phone conflict.
    """
    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        return ("skipped", None)

    # Case 1: user already linked to this card → update credentials
    cur.execute("SELECT id FROM users WHERE card_id=?", (card_id,))
    existing = cur.fetchone()
    if existing:
        cur.execute(
            "UPDATE users SET username=?, password=? WHERE id=?",
            (normalized_phone, employee_id, existing[0])
        )
        return ("updated", normalized_phone)

    # Case 2: phone exists but user is unlinked → claim it
    cur.execute("SELECT id, card_id FROM users WHERE username=?", (normalized_phone,))
    conflict = cur.fetchone()
    if conflict:
        if conflict[1] is None:
            cur.execute(
                "UPDATE users SET card_id=?, password=?, role='user' WHERE id=?",
                (card_id, employee_id, conflict[0])
            )
            return ("updated", normalized_phone)
        elif conflict[1] != card_id:
            raise ValueError(
                f"Phone {normalized_phone} is already used by another employee "
                f"(card_id={conflict[1]}). Please use a unique phone number."
            )

    # Case 3: fresh user
    cur.execute(
        "INSERT INTO users (username, password, role, card_id) VALUES (?,?,'user',?)",
        (normalized_phone, employee_id, card_id)
    )
    return ("created", normalized_phone)

# ── Date helpers ───────────────────────────────────────────────────────────────
def fix_card_dates(row):
    card = list(row)
    for idx in [8, 9]:
        if card[idx] and isinstance(card[idx], str):
            try:
                card[idx] = datetime.strptime(card[idx][:10], "%Y-%m-%d").date()
            except ValueError:
                card[idx] = None
    return card

def fix_comment_dates(rows):
    fixed = []
    for row in rows:
        cm = list(row)
        if cm[3] and isinstance(cm[3], str):
            try:
                cm[3] = datetime.strptime(cm[3][:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                cm[3] = None
        fixed.append(cm)
    return fixed

# ── Dropdown helper ────────────────────────────────────────────────────────────
def get_dropdowns():
    """Return (designations, departments) lists from DB for form dropdowns."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT id, designation FROM designation ORDER BY designation")
    designations = cur.fetchall()
    cur.execute("SELECT id, department FROM department ORDER BY department")
    departments = cur.fetchall()
    conn.close()
    return designations, departments

# ── DB Init ────────────────────────────────────────────────────────────────────
def init_db():
    conn = get_db()
    cur  = conn.cursor()

    # users table
    cur.execute("""
        IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='users' AND xtype='U')
        CREATE TABLE users (
            id          INT IDENTITY(1,1) PRIMARY KEY,
            username    NVARCHAR(100) NOT NULL UNIQUE,
            password    NVARCHAR(255) NOT NULL,
            role        NVARCHAR(10)  NOT NULL DEFAULT 'user',
            card_id     INT NULL,
            created_at  DATETIME DEFAULT GETDATE()
        )
    """)
    cur.execute("""
        IF NOT EXISTS (
            SELECT * FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME='users' AND COLUMN_NAME='card_id'
        )
        ALTER TABLE users ADD card_id INT NULL
    """)

    # designation table
    cur.execute("""
        IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='designation' AND xtype='U')
        CREATE TABLE designation (
            id   INT IDENTITY(1,1) PRIMARY KEY,
            name NVARCHAR(150) NOT NULL UNIQUE
        )
    """)

    # department table
    cur.execute("""
        IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='department' AND xtype='U')
        CREATE TABLE department (
            id   INT IDENTITY(1,1) PRIMARY KEY,
            name NVARCHAR(150) NOT NULL UNIQUE
        )
    """)

    # id_cards table
    cur.execute("""
        IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='id_cards' AND xtype='U')
        CREATE TABLE id_cards (
            id            INT IDENTITY(1,1) PRIMARY KEY,
            employee_id   NVARCHAR(50)  NOT NULL UNIQUE,
            full_name     NVARCHAR(200) NOT NULL,
            designation   NVARCHAR(150),
            department    NVARCHAR(150),
            email         NVARCHAR(200),
            phone         NVARCHAR(30),
            blood_group   NVARCHAR(5),
            date_of_birth DATE,
            join_date     DATE,
            address       NVARCHAR(500),
            photo_url     NVARCHAR(500),
            is_active     BIT DEFAULT 1,
            created_at    DATETIME DEFAULT GETDATE(),
            updated_at    DATETIME DEFAULT GETDATE()
        )
    """)

    # comments table
    cur.execute("""
        IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='comments' AND xtype='U')
        CREATE TABLE comments (
            id           INT IDENTITY(1,1) PRIMARY KEY,
            card_id      INT NOT NULL FOREIGN KEY REFERENCES id_cards(id),
            user_id      INT NOT NULL FOREIGN KEY REFERENCES users(id),
            username     NVARCHAR(100) NOT NULL,
            comment_text NVARCHAR(1000) NOT NULL,
            is_read      BIT DEFAULT 0,
            created_at   DATETIME DEFAULT GETDATE()
        )
    """)

    # Seed designations
    cur.execute("SELECT COUNT(*) FROM designation")
    if cur.fetchone()[0] == 0:
        for name in [
            'Professor', 'Associate Professor', 'Assistant Professor',
            'Lecturer', 'Senior Lecturer', 'Director', 'Deputy Director',
            'Assistant Director', 'Officer', 'Senior Officer',
            'Principal', 'Vice Principal', 'Registrar', 'Accountant',
            'Staff', 'Lab Assistant', 'Computer Operator',
            'Senior Software Engineer', 'Product Manager',
            'UI/UX Designer', 'Data Analyst', 'DevOps Engineer',
        ]:
            cur.execute("INSERT INTO designation (name) VALUES (?)", (name,))

    # Seed departments
    cur.execute("SELECT COUNT(*) FROM department")
    if cur.fetchone()[0] == 0:
        for name in [
            'Administration', 'Accounting & Finance', 'Computer Science',
            'Mathematics', 'Physics', 'Chemistry', 'English',
            'Bangla', 'History', 'Islamic Studies', 'Law',
            'Management', 'Marketing', 'Economics',
            'Library', 'Examination', 'IT', 'HR',
            'Engineering', 'Product', 'Design',
            'Analytics', 'Infrastructure',
        ]:
            cur.execute("INSERT INTO department (name) VALUES (?)", (name,))

    # Seed users
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        for u in [
            ("admin",      "admin123", "admin"),
            ("john_doe",   "user123",  "user"),
            ("jane_smith", "user123",  "user"),
            ("bob_wilson", "user123",  "user"),
        ]:
            cur.execute(
                "INSERT INTO users (username, password, role) VALUES (?,?,?)", u
            )

    # Seed id_cards
    cur.execute("SELECT COUNT(*) FROM id_cards")
    if cur.fetchone()[0] == 0:
        sample_cards = [
            ("EMP-001", "John Doe",     "Senior Software Engineer", "Engineering",
             "john.doe@company.com",    "+880-1711-000001", "B+",
             "1990-05-15", "2020-03-01", "123 Main Street, Dhaka",
             "https://api.dicebear.com/7.x/personas/svg?seed=John"),
            ("EMP-002", "Jane Smith",   "Product Manager",          "Product",
             "jane.smith@company.com",  "+880-1711-000002", "A+",
             "1988-08-22", "2019-07-15", "456 Park Ave, Dhaka",
             "https://api.dicebear.com/7.x/personas/svg?seed=Jane"),
            ("EMP-003", "Bob Wilson",   "UI/UX Designer",           "Design",
             "bob.wilson@company.com",  "+880-1711-000003", "O+",
             "1992-11-30", "2021-01-10", "789 Garden Road, Chittagong",
             "https://api.dicebear.com/7.x/personas/svg?seed=Bob"),
            ("EMP-004", "Alice Rahman", "Data Analyst",             "Analytics",
             "alice.rahman@company.com","+880-1711-000004", "AB+",
             "1995-03-18", "2022-06-01", "321 Lake View, Sylhet",
             "https://api.dicebear.com/7.x/personas/svg?seed=Alice"),
            ("EMP-005", "Carlos Hasan","DevOps Engineer",           "Infrastructure",
             "carlos.hasan@company.com","+880-1711-000005", "B-",
             "1987-07-25", "2018-09-20", "654 Hill Street, Dhaka",
             "https://api.dicebear.com/7.x/personas/svg?seed=Carlos"),
        ]
        for c in sample_cards:
            cur.execute("""
                INSERT INTO id_cards
                    (employee_id, full_name, designation, department, email, phone,
                     blood_group, date_of_birth, join_date, address, photo_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, c)

    # Link sample users to their cards
    user_card_map = {
        "john_doe":   "John Doe",
        "jane_smith": "Jane Smith",
        "bob_wilson": "Bob Wilson",
    }
    for username, full_name in user_card_map.items():
        cur.execute("""
            UPDATE users
            SET card_id = (SELECT id FROM id_cards WHERE full_name = ?)
            WHERE username = ? AND card_id IS NULL
        """, (full_name, username))

    conn.commit()
    conn.close()
    print("[DB] Database initialised successfully.")

# ── Auth decorators ────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT id, username, role, card_id FROM users WHERE username=? AND password=?",
            (username, password)
        )
        user = cur.fetchone()
        conn.close()
        if user:
            session["user_id"]  = user[0]
            session["username"] = user[1]
            session["role"]     = user[2]
            session["card_id"]  = user[3]
            flash(f"Welcome back, {user[1]}!", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    cur  = conn.cursor()
    if session.get("role") == "admin":
        cur.execute("""
            SELECT id, employee_id, full_name, designation, department,
                   blood_group, is_active, photo_url
            FROM id_cards ORDER BY full_name
        """)
    else:
        user_card_id = session.get("card_id")
        if user_card_id:
            cur.execute("""
                SELECT id, employee_id, full_name, designation, department,
                       blood_group, is_active, photo_url
                FROM id_cards WHERE id=?
            """, (user_card_id,))
        else:
            cur.execute("""
                SELECT id, employee_id, full_name, designation, department,
                       blood_group, is_active, photo_url
                FROM id_cards WHERE 1=0
            """)
    cards = cur.fetchall()
    unread_count = 0
    if session.get("role") == "admin":
        cur.execute("SELECT COUNT(*) FROM comments WHERE is_read=0")
        unread_count = cur.fetchone()[0]
    conn.close()
    return render_template("dashboard.html", cards=cards, unread_count=unread_count)


@app.route("/card/<int:card_id>")
@login_required
def view_card(card_id):
    if session.get("role") != "admin":
        if session.get("card_id") != card_id:
            flash("You do not have permission to view that card.", "error")
            return redirect(url_for("dashboard"))
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM id_cards WHERE id=?", (card_id,))
    row = cur.fetchone()
    if not row:
        flash("ID Card not found.", "error")
        conn.close()
        return redirect(url_for("dashboard"))
    card = fix_card_dates(row)
    cur.execute("""
        SELECT id, username, comment_text, created_at, is_read
        FROM comments WHERE card_id=? ORDER BY created_at DESC
    """, (card_id,))
    comments = fix_comment_dates(cur.fetchall())
    if session.get("role") == "admin":
        cur.execute(
            "UPDATE comments SET is_read=1 WHERE card_id=? AND is_read=0", (card_id,)
        )
        conn.commit()
    conn.close()
    return render_template("view_card.html", card=card, comments=comments)


@app.route("/card/new", methods=["GET", "POST"])
@admin_required
def new_card():
    designations, departments = get_dropdowns()

    if request.method == "POST":
        f    = request.form
        conn = get_db()
        cur  = conn.cursor()
        try:
            # ── Photo ─────────────────────────────────────────────────
            photo_data = None
            if "photo_file" in request.files:
                file = request.files["photo_file"]
                if file and file.filename != "":
                    raw  = file.read()
                    ext  = file.filename.rsplit(".", 1)[-1].lower()
                    mime = {
                        "jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "png": "image/png",  "gif": "image/gif",
                        "webp": "image/webp"
                    }.get(ext, "image/jpeg")
                    photo_data = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
            if not photo_data:
                photo_data = f.get("photo_url", "").strip() or None

            employee_id = f["employee_id"].strip()
            phone       = f["phone"].strip()

            # ── Designation: custom or dropdown ───────────────────────
            if f.get("designation_custom", "").strip():
                designation = f["designation_custom"].strip()
                # Auto-add new designation to table
                cur.execute(
                    "IF NOT EXISTS (SELECT 1 FROM designation WHERE name=?) "
                    "INSERT INTO designation (name) VALUES (?)",
                    (designation, designation)
                )
            else:
                designation = f.get("designation", "").strip()

            # ── Department: custom or dropdown ────────────────────────
            if f.get("department_custom", "").strip():
                department = f["department_custom"].strip()
                cur.execute(
                    "IF NOT EXISTS (SELECT 1 FROM department WHERE name=?) "
                    "INSERT INTO department (name) VALUES (?)",
                    (department, department)
                )
            else:
                department = f.get("department", "").strip()

            # Pre-flight phone duplicate check
            normalized_phone = normalize_phone(phone)
            if normalized_phone:
                cur.execute(
                    "SELECT id, card_id FROM users WHERE username=?",
                    (normalized_phone,)
                )
                conflict = cur.fetchone()
                if conflict and conflict[1] is not None:
                    raise ValueError(
                        f"Phone {normalized_phone} is already used by another employee."
                    )

            # Insert card
            cur.execute("""
                INSERT INTO id_cards
                    (employee_id, full_name, designation, department, email, phone,
                     blood_group, date_of_birth, join_date, address, photo_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                employee_id, f["full_name"], designation, department,
                f["email"], phone, f["blood_group"],
                f["date_of_birth"] or None, f["join_date"] or None,
                f["address"], photo_data
            ))
            conn.commit()

            cur.execute("SELECT id FROM id_cards WHERE employee_id=?", (employee_id,))
            new_card_id = cur.fetchone()[0]

            action, username = create_or_update_user_for_card(
                cur, employee_id, phone, new_card_id
            )
            conn.commit()

            if action == "skipped":
                flash("ID Card created. No phone — user login was not created.", "warning")
            else:
                flash(
                    f"ID Card created! Login → Username: {username} | Password: {employee_id}",
                    "success"
                )
            return redirect(url_for("dashboard"))

        except ValueError as ve:
            conn.rollback()
            flash(f"Validation error: {ve}", "error")
        except Exception as e:
            conn.rollback()
            flash(f"Error creating card: {e}", "error")
        finally:
            conn.close()

    return render_template(
        "card_form.html",
        card=None,
        action="new",
        designations=designations,
        departments=departments,
    )


@app.route("/card/<int:card_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_card(card_id):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM id_cards WHERE id=?", (card_id,))
    row = cur.fetchone()
    if not row:
        flash("ID Card not found.", "error")
        conn.close()
        return redirect(url_for("dashboard"))
    card = fix_card_dates(row)
    conn.close()

    designations, departments = get_dropdowns()

    if request.method == "POST":
        f    = request.form
        conn = get_db()
        cur  = conn.cursor()
        try:
            # ── Photo ─────────────────────────────────────────────────
            photo_data = card[11]
            if "photo_file" in request.files:
                file = request.files["photo_file"]
                if file and file.filename != "":
                    raw  = file.read()
                    ext  = file.filename.rsplit(".", 1)[-1].lower()
                    mime = {
                        "jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "png": "image/png",  "gif": "image/gif",
                        "webp": "image/webp"
                    }.get(ext, "image/jpeg")
                    photo_data = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
                else:
                    url_field = f.get("photo_url", "").strip()
                    if url_field and not url_field.startswith("data:"):
                        photo_data = url_field

            employee_id = f["employee_id"].strip()
            phone       = f["phone"].strip()

            # ── Designation: custom or dropdown ───────────────────────
            if f.get("designation_custom", "").strip():
                designation = f["designation_custom"].strip()
                cur.execute(
                    "IF NOT EXISTS (SELECT 1 FROM designation WHERE name=?) "
                    "INSERT INTO designation (name) VALUES (?)",
                    (designation, designation)
                )
            else:
                designation = f.get("designation", "").strip()

            # ── Department: custom or dropdown ────────────────────────
            if f.get("department_custom", "").strip():
                department = f["department_custom"].strip()
                cur.execute(
                    "IF NOT EXISTS (SELECT 1 FROM department WHERE name=?) "
                    "INSERT INTO department (name) VALUES (?)",
                    (department, department)
                )
            else:
                department = f.get("department", "").strip()

            # Pre-flight phone duplicate check
            normalized_phone = normalize_phone(phone)
            if normalized_phone:
                cur.execute(
                    "SELECT id, card_id FROM users WHERE username=?",
                    (normalized_phone,)
                )
                conflict = cur.fetchone()
                if conflict and conflict[1] is not None and conflict[1] != card_id:
                    raise ValueError(
                        f"Phone {normalized_phone} is already used by another employee."
                    )

            # Update card
            cur.execute("""
                UPDATE id_cards SET
                    employee_id=?, full_name=?, designation=?, department=?,
                    email=?, phone=?, blood_group=?, date_of_birth=?,
                    join_date=?, address=?, photo_url=?, is_active=?,
                    updated_at=GETDATE()
                WHERE id=?
            """, (
                employee_id, f["full_name"], designation, department,
                f["email"], phone, f["blood_group"],
                f["date_of_birth"] or None, f["join_date"] or None,
                f["address"], photo_data,
                1 if f.get("is_active") else 0,
                card_id
            ))
            conn.commit()

            action, username = create_or_update_user_for_card(
                cur, employee_id, phone, card_id
            )
            conn.commit()

            if action == "skipped":
                flash("ID Card updated. No phone — user login was not changed.", "warning")
            else:
                flash(
                    f"ID Card updated! Login → Username: {username} | Password: {employee_id}",
                    "success"
                )
            conn.close()
            return redirect(url_for("view_card", card_id=card_id))

        except ValueError as ve:
            conn.rollback()
            flash(f"Validation error: {ve}", "error")
        except Exception as e:
            conn.rollback()
            flash(f"Error updating card: {e}", "error")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return render_template(
        "card_form.html",
        card=card,
        action="edit",
        designations=designations,
        departments=departments,
    )


@app.route("/card/<int:card_id>/delete", methods=["POST"])
@admin_required
def delete_card(card_id):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("UPDATE users SET card_id=NULL WHERE card_id=?", (card_id,))
    cur.execute("DELETE FROM comments WHERE card_id=?", (card_id,))
    cur.execute("DELETE FROM id_cards WHERE id=?", (card_id,))
    conn.commit()
    conn.close()
    flash("ID Card deleted.", "info")
    return redirect(url_for("dashboard"))


@app.route("/card/<int:card_id>/comment", methods=["POST"])
@login_required
def add_comment(card_id):
    if session.get("role") != "admin" and session.get("card_id") != card_id:
        flash("You do not have permission to comment on that card.", "error")
        return redirect(url_for("dashboard"))
    text = request.form.get("comment_text", "").strip()
    if not text:
        flash("Comment cannot be empty.", "error")
        return redirect(url_for("view_card", card_id=card_id))
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "INSERT INTO comments (card_id, user_id, username, comment_text) VALUES (?,?,?,?)",
        (card_id, session["user_id"], session["username"], text)
    )
    conn.commit()
    conn.close()
    flash("Comment added successfully!", "success")
    return redirect(url_for("view_card", card_id=card_id))


@app.route("/admin/comments")
@admin_required
def admin_comments():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT c.id, c.username, c.comment_text, c.created_at, c.is_read,
               ic.full_name, ic.employee_id, ic.id AS card_id
        FROM comments c
        JOIN id_cards ic ON c.card_id = ic.id
        ORDER BY c.is_read ASC, c.created_at DESC
    """)
    comments = fix_comment_dates(cur.fetchall())
    cur.execute("UPDATE comments SET is_read=1 WHERE is_read=0")
    conn.commit()
    conn.close()
    return render_template("admin_comments.html", comments=comments)


@app.route("/admin/users")
@admin_required
def admin_users():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT id, username, role, created_at FROM users ORDER BY role, username"
    )
    users = cur.fetchall()
    conn.close()
    return render_template("admin_users.html", users=users)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)