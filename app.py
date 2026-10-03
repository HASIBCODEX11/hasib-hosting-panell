import threading
import os, sys, shlex, sqlite3, zipfile, subprocess, signal, shutil, psutil, time, datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit

# Global process tracker
running_procs = {}
start_times = {}

# Railway Volume (RAILWAY_VOLUME_MOUNT_PATH) ba DATA_DIR thakle sekhane data save hobe
DATA_DIR = os.environ.get('DATA_DIR') or os.environ.get('RAILWAY_VOLUME_MOUNT_PATH') or os.getcwd()
DB_PATH = os.path.join(DATA_DIR, 'storage', 'nehost.db')

# threading mode: eventlet lage na (Railway-te eventlet crash korto)
socketio = SocketIO(async_mode='threading')

def get_db():
    db_path = DB_PATH
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = get_db()
    # User Table
    db.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        fname TEXT, lname TEXT, username TEXT, email TEXT, password TEXT, pfp TEXT DEFAULT 'default.png',
        role TEXT DEFAULT 'free', 
        status TEXT DEFAULT 'active',
        server_limit INTEGER DEFAULT 1,
        notifications TEXT DEFAULT ''
    )''')
    # Server Table
    db.execute('''CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        user_id INTEGER, name TEXT, folder TEXT, 
        status TEXT, startup TEXT, pid INTEGER,
        server_status TEXT DEFAULT 'active'
    )''')
    # Support Ticket Table
    db.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, subject TEXT, message TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Admin Settings Table
    db.execute('''CREATE TABLE IF NOT EXISTS admin_settings (
        id INTEGER PRIMARY KEY, 
        username TEXT, password TEXT,
        popup_title TEXT, popup_msg TEXT, popup_img TEXT, show_popup INTEGER DEFAULT 0
    )''')
    
    # purono DB te column na thakle auto add
    for table, col, ddl in [
        ('users', 'notifications', "TEXT DEFAULT ''"), ('users', 'status', "TEXT DEFAULT 'active'"),
        ('users', 'role', "TEXT DEFAULT 'free'"), ('users', 'server_limit', 'INTEGER DEFAULT 1'),
        ('users', 'pfp', "TEXT DEFAULT 'default.png'"), ('servers', 'server_status', "TEXT DEFAULT 'active'"),
        ('servers', 'pid', 'INTEGER'), ('servers', 'startup', 'TEXT'),
        ('admin_settings', 'popup_title', 'TEXT'), ('admin_settings', 'popup_msg', 'TEXT'),
        ('admin_settings', 'popup_img', 'TEXT'), ('admin_settings', 'show_popup', 'INTEGER DEFAULT 0'),
    ]:
        cols = [r[1] for r in db.execute(f'PRAGMA table_info({table})').fetchall()]
        if col not in cols:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}')

    # Admin login: ADMIN_EMAIL + ADMIN_PASSWORD env set thakle protibar start-e sync hobe
    env_user = os.environ.get('ADMIN_EMAIL', '').strip()
    env_pass = os.environ.get('ADMIN_PASSWORD', '')
    if not db.execute('SELECT 1 FROM admin_settings WHERE id=1').fetchone():
        db.execute('INSERT INTO admin_settings (id, username, password) VALUES (1, ?, ?)',
                   (env_user or 'hasib143@gmail.com', env_pass or 'hasib'))
    elif env_user and env_pass:
        db.execute('UPDATE admin_settings SET username=?, password=? WHERE id=1', (env_user, env_pass))
    
    db.commit()
    db.close()

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'SHAPPNO_ultra_pro_max_99')
    app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(days=30)
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['BASE_STORAGE'] = os.path.join(DATA_DIR, 'storage', 'instances')
    app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'static/uploads')
    
    if not os.path.exists(app.config['BASE_STORAGE']):
        os.makedirs(app.config['BASE_STORAGE'])
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
        
    init_db()
    socketio.init_app(app)

    def get_precise_uptime(start_timestamp):
        if not start_timestamp: return "Offline"
        diff = int(time.time() - start_timestamp)
        months, rem = divmod(diff, 2592000)
        days, rem = divmod(rem, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        
        parts = []
        if months > 0: parts.append(f"{months}mo")
        if days > 0: parts.append(f"{days}d")
        if hours > 0: parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)
    
    @app.route('/')
    def home():
      return render_template('index.html')

    @app.route('/health')
    def health():
        return jsonify({'status': 'ok'})

    # --- SIGNUP ROUTE (server_limit = 2) ---
    @app.route('/signup', methods=['GET', 'POST'])
    def signup():
        if request.method == 'POST':
            fname = request.form.get('fname')
            lname = request.form.get('lname')
            username = (request.form.get('username') or '').strip()
            email = (request.form.get('email') or '').strip().lower()
            pwd = request.form.get('password')
            cpwd = request.form.get('confirm_password')
            pfp = request.files.get('pfp')

            if pwd != cpwd:
                return jsonify({'status': 'error', 'msg': 'Passwords do not match!'}), 400

            db = get_db()
            existing_user = db.execute('SELECT id FROM users WHERE lower(email)=lower(?) OR lower(username)=lower(?)', (email, username)).fetchone()
            if existing_user:
                db.close()
                return jsonify({'status': 'error', 'msg': 'Email or Username already taken!'}), 400

            pfp_name = 'default.png'
            if pfp:
                pfp_name = secure_filename(pfp.filename)
                pfp.save(os.path.join(app.config['UPLOAD_FOLDER'], pfp_name))

            # Free users now get 2 server limit
            db.execute('''INSERT INTO users 
                (fname, lname, username, email, password, pfp, server_limit, role, status) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (fname, lname, username, email, pwd, pfp_name, 10, 'free', 'active'))
            
            db.commit()
            db.close()
            return jsonify({'status': 'success', 'url': url_for('login')})
        
        return render_template('web/signup.html')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            email = (request.form.get('email') or '').strip().lower()
            pwd = request.form.get('password')
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE (lower(email)=lower(?) OR lower(username)=lower(?)) AND password=?', (email, email, pwd)).fetchone()
            db.close()
            
            if user:
                if user['status'] == 'banned':
                    return jsonify({'status': 'banned', 'msg': 'Your account is suspended!'}), 403
                session.permanent = True
                session['user_id'] = user['id']
                return jsonify({'status': 'success', 'url': url_for('dashboard')}), 200
            else:
                return jsonify({'status': 'error', 'msg': 'Invalid credentials!'}), 401
        return render_template('web/login.html')

    @app.route('/dashboard')
    def dashboard():
        if 'user_id' not in session: return redirect(url_for('login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        db.close()
        if not user or user['status'] != 'active':
            session.clear()
            return redirect(url_for('login'))
        return render_template('web/dashboard.html', user=user)

    @app.route('/profile/update', methods=['POST'])
    def update_profile():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        uid = session['user_id']
        fname = request.form.get('fname')
        lname = request.form.get('lname')
        pwd = request.form.get('password')
        db = get_db()
        if pwd:
            db.execute('UPDATE users SET fname=?, lname=?, password=? WHERE id=?', (fname, lname, pwd, uid))
        else:
            db.execute('UPDATE users SET fname=?, lname=? WHERE id=?', (fname, lname, uid))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/ticket/create', methods=['POST'])
    def create_ticket():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('INSERT INTO tickets (user_id, subject, message) VALUES (?,?,?)', (session['user_id'], d['subject'], d['message']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/api/announcement')
    def get_announcement():
        db = get_db()
        conf = db.execute('SELECT popup_title, popup_msg, popup_img, show_popup FROM admin_settings WHERE id=1').fetchone()
        db.close()
        return jsonify(dict(conf))

    @app.route('/admin-login', methods=['GET', 'POST'])
    def admin_login():
        if request.method == 'POST':
            user = (request.form.get('username') or '').strip()
            pwd = request.form.get('password') or ''
            db = get_db()
            # email case-insensitive + password-er age/pore space ignore (mobile keyboard fix)
            admin = db.execute(
                'SELECT * FROM admin_settings WHERE lower(trim(username))=lower(?) AND password IN (?, ?)',
                (user, pwd, pwd.strip())).fetchone()
            db.close()
            if admin:
                session.permanent = True
                session['admin_logged'] = True
                return redirect(url_for('admin_panel'))
            return render_template('web/admin_login.html', error=True), 401
        return render_template('web/admin_login.html')

    @app.route('/admin/panel')
    def admin_panel():
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        return render_template('web/admin_panel.html')

    @app.route('/admin/stats')
    def admin_stats():
        if not session.get('admin_logged'): return jsonify({})
        db = get_db()
        users = db.execute('SELECT * FROM users').fetchall()
        user_list = []
        total_cpu = psutil.cpu_percent()
        total_ram = psutil.virtual_memory().percent
        for u in users:
            srvs = db.execute('SELECT * FROM servers WHERE user_id=?', (u['id'],)).fetchall()
            active_srvs = 0
            for s in srvs:
                is_on = False
                if s['pid'] and psutil.pid_exists(s['pid']):
                    try:
                        proc = psutil.Process(s['pid'])
                        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                            is_on = True
                    except: pass
                elif s['folder'] in running_procs and running_procs[s['folder']].poll() is None:
                    is_on = True
                if is_on: active_srvs += 1
            user_list.append({
                'id': u['id'], 'fname': u['fname'], 'email': u['email'], 
                'srv_count': len(srvs), 'active_srvs': active_srvs,
                'status': u['status'], 'role': u['role'], 'server_limit': u['server_limit']
            })
        db.close()
        return jsonify({'users': user_list, 'sys_cpu': f"{total_cpu}%", 'sys_ram': f"{total_ram}%"})

    @app.route('/admin/user/update', methods=['POST'])
    def update_user():
        if not session.get('admin_logged'): return jsonify({'status':'error'})
        d = request.json
        db = get_db()
        db.execute('UPDATE users SET role=?, status=?, server_limit=? WHERE id=?', (d['role'], d['status'], d['limit'], d['user_id']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/set-popup', methods=['POST'])
    def set_popup():
        if not session.get('admin_logged'): return jsonify({'status':'error'})
        title, msg, show = request.form.get('title'), request.form.get('msg'), request.form.get('show')
        img = request.files.get('image')
        db = get_db()
        old_data = db.execute('SELECT popup_img FROM admin_settings WHERE id=1').fetchone()
        img_name = old_data['popup_img'] if old_data else None
        if img:
            img_name = secure_filename(img.filename)
            img.save(os.path.join(app.config['UPLOAD_FOLDER'], img_name))
        db.execute('UPDATE admin_settings SET popup_title=?, popup_msg=?, popup_img=?, show_popup=? WHERE id=1', (title, msg, img_name, 1 if show == 'true' else 0))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/send-warning', methods=['POST'])
    def send_warning():
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('UPDATE users SET notifications=? WHERE id=?', (d['message'], d['user_id']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/login-as/<int:uid>')
    def login_as(uid):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        session['user_id'] = uid
        return redirect(url_for('dashboard'))

    @app.route('/admin/manage-user/<int:uid>')
    def admin_manage_user_servers(uid):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (uid,)).fetchall()
        db.close()
        servers = []
        for r in rows:
            f = r['folder']
            online = (f in running_procs and running_procs[f].poll() is None) or (r['pid'] and psutil.pid_exists(r['pid']))
            servers.append({'id': r['id'], 'name': r['name'], 'folder': f, 'online': online, 'status': r['server_status']})
        return render_template('web/admin_manage_user.html', user=user, servers=servers)

    @app.route('/admin/suspend-server/<int:sid>', methods=['POST'])
    def admin_suspend_server(sid):
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        status = request.json.get('status')
        db = get_db()
        db.execute('UPDATE servers SET server_status=? WHERE id=?', (status, sid))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/delete-server/<int:sid>', methods=['POST'])
    def admin_delete_server(sid):
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        db = get_db()
        srv = db.execute('SELECT folder FROM servers WHERE id=?', (sid,)).fetchone()
        if srv:
            folder = srv['folder']
            if folder in running_procs:
                try: os.killpg(os.getpgid(running_procs[folder].pid), signal.SIGKILL)
                except: pass
                del running_procs[folder]
            db.execute('DELETE FROM servers WHERE id=?', (sid,))
            db.commit()
            path = os.path.join(app.config['BASE_STORAGE'], folder)
            if os.path.exists(path): shutil.rmtree(path)
            db.close()
            return jsonify({'status': 'deleted'})
        db.close()
        return jsonify({'status': 'error', 'msg': 'Server not found'})

    @app.route('/admin/create-user', methods=['POST'])
    def admin_create_user():
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        limit = d.get('limit', 1)
        db.execute('INSERT INTO users (fname, email, password, server_limit) VALUES (?,?,?,?)', (d['name'], d['email'], d['pass'], limit))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/delete-user/<int:uid>', methods=['POST'])
    def delete_user(uid):
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        db = get_db()
        srvs = db.execute('SELECT folder, pid FROM servers WHERE user_id=?', (uid,)).fetchall()
        for s in srvs:
            t_pid = running_procs[s['folder']].pid if s['folder'] in running_procs else s['pid']
            if t_pid and (s['folder'] in running_procs or is_our_proc(t_pid)): kill_group(t_pid)
            running_procs.pop(s['folder'], None)
            path = os.path.join(app.config['BASE_STORAGE'], s['folder'])
            if os.path.exists(path): shutil.rmtree(path)
        db.execute('DELETE FROM servers WHERE user_id=?', (uid,))
        db.execute('DELETE FROM users WHERE id=?', (uid,))
        db.commit()
        db.close()
        return jsonify({'status': 'deleted'})
        
    @app.route('/admin/files/<folder>')
    def admin_browse_files(folder):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        return render_template('web/dashboard.html', user={'fname': 'Admin'}, is_admin_view=True, admin_folder=folder)

    # ---------- access + safe-path helpers ----------
    def can_access(folder):
        db = get_db()
        row = db.execute('SELECT user_id FROM servers WHERE folder=?', (folder,)).fetchone()
        db.close()
        if not row: return False
        if session.get('admin_logged'): return True
        return 'user_id' in session and row['user_id'] == session['user_id']

    def folder_access(f):
        @wraps(f)
        def wrapper(folder, *a, **kw):
            if not can_access(folder):
                return jsonify({'status': 'error', 'msg': 'Access denied'}), 403
            return f(folder, *a, **kw)
        return wrapper

    def safe_path(folder, sub='', name=None):
        """Folder-er bhitore thakle path dibe, na hole None (path traversal block)."""
        root = os.path.realpath(os.path.join(app.config['BASE_STORAGE'], folder))
        parts = [root, sub or '']
        if name is not None: parts.append(name)
        target = os.path.realpath(os.path.join(*parts))
        if target != root and not target.startswith(root + os.sep): return None
        return target

    def get_data():
        return request.get_json(silent=True) or {}

    def is_our_proc(pid):
        try:
            p = psutil.Process(pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE and 'while true' in ' '.join(p.cmdline())
        except Exception:
            return False

    def kill_group(pid):
        try: os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception: pass

    # ---------- file manager ----------
    @app.route('/files/list/<folder>')
    @folder_access
    def flist(folder):
        sub_path = request.args.get('path', '')
        full_path = safe_path(folder, sub_path)
        if not full_path or not os.path.isdir(full_path): return jsonify([])
        items = []
        for f in sorted(os.listdir(full_path)):
            if f == 'console.log': continue
            p = os.path.join(full_path, f)
            items.append({'name': f, 'is_dir': os.path.isdir(p), 'is_zip': f.lower().endswith('.zip'), 'rel_path': os.path.join(sub_path, f)})
        return jsonify(items)

    # dashboard /files/read/<folder>?name=..&path=.. call kore
    @app.route('/files/read/<folder>')
    @app.route('/files/content/<folder>/<name>')
    @folder_access
    def fcontent(folder, name=None):
        name = name or request.args.get('name', '')
        p = safe_path(folder, request.args.get('path', ''), name) if name else None
        if not p or not os.path.isfile(p): return jsonify({'content': 'Error reading file'})
        try:
            with open(p, 'r', encoding='utf-8', errors='ignore') as f: return jsonify({'content': f.read(2000000)})
        except Exception:
            return jsonify({'content': 'Error reading file'})

    # dashboard /files/save/<folder> (name body-te) call kore
    @app.route('/files/save/<folder>', methods=['POST'], defaults={'name': None})
    @app.route('/files/save/<folder>/<name>', methods=['POST'])
    @folder_access
    def fsave(folder, name=None):
        d = get_data()
        name = name or d.get('name')
        sub_path = d.get('path') or request.args.get('path', '')
        content = d.get('content')
        p = safe_path(folder, sub_path, name) if name else None
        if not p or content is None: return jsonify({'status': 'error'})
        try:
            with open(p, 'w', encoding='utf-8', newline='') as f: f.write(content)
            return jsonify({'status': 'saved'})
        except Exception:
            return jsonify({'status': 'error'})

    @app.route('/files/delete-bulk/<folder>', methods=['POST'])
    @folder_access
    def delete_bulk(folder):
        d = get_data()
        sub_path, names = d.get('path', ''), d.get('names', [])
        base = safe_path(folder, sub_path)
        if not base or not os.path.isdir(base): return jsonify({'status': 'error'})
        if not names: names = [f for f in os.listdir(base) if f != 'console.log']
        for name in names:
            if name == 'console.log': continue
            p = safe_path(folder, sub_path, name)
            if not p or p == base: continue
            try:
                if os.path.isdir(p): shutil.rmtree(p)
                elif os.path.exists(p): os.remove(p)
            except Exception: pass
        return jsonify({'status': 'ok'})

    @app.route('/files/create-file/<folder>', methods=['POST'])
    @folder_access
    def create_file(folder):
        d = get_data()
        name = secure_filename(d.get('name') or '')
        p = safe_path(folder, d.get('path', ''), name) if name else None
        if not p: return jsonify({'status': 'error', 'msg': 'Invalid name'})
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f: f.write('')
        return jsonify({'status': 'success'})

    @app.route('/files/create-folder/<folder>', methods=['POST'])
    @folder_access
    def create_folder(folder):
        d = get_data()
        name = secure_filename(d.get('name') or '')
        p = safe_path(folder, d.get('path', ''), name) if name else None
        if not p: return jsonify({'status': 'error', 'msg': 'Invalid name'})
        os.makedirs(p, exist_ok=True)
        return jsonify({'status': 'success'})

    @app.route('/files/upload/<folder>', methods=['POST'])
    @folder_access
    def upload_file(folder):
        file = request.files.get('file')
        name = secure_filename(file.filename) if file else ''
        dest = safe_path(folder, request.form.get('path', ''))
        if not file or not name or not dest: return jsonify({'status': 'error', 'msg': 'Invalid upload'})
        os.makedirs(dest, exist_ok=True)
        file.save(os.path.join(dest, name))
        return jsonify({'status': 'success'})

    @app.route('/files/rename/<folder>', methods=['POST'])
    @folder_access
    def rename_file(folder):
        d = get_data()
        sub_path = d.get('path', '')
        new_name = os.path.basename(d.get('new') or '')
        src = safe_path(folder, sub_path, d.get('old') or '')
        dst = safe_path(folder, sub_path, new_name) if new_name else None
        if not src or not dst or not os.path.exists(src): return jsonify({'status': 'error', 'msg': 'Invalid name'})
        if os.path.exists(dst): return jsonify({'status': 'error', 'msg': 'Name already exists'})
        os.rename(src, dst)
        return jsonify({'status': 'success'})

    @app.route('/files/download/<folder>/<name>')
    @folder_access
    def download_file(folder, name):
        p = safe_path(folder, request.args.get('path', ''), name)
        if not p or not os.path.isfile(p): return "Not found", 404
        return send_file(p, as_attachment=True)

    @app.route('/files/zip-bulk/<folder>', methods=['POST'])
    @folder_access
    def zip_bulk(folder):
        d = get_data()
        names, sub_path = d.get('names', []), d.get('path', '')
        base = safe_path(folder, sub_path)
        if not base or not os.path.isdir(base): return jsonify({'status': 'error'})
        if not names: names = [f for f in os.listdir(base) if f != 'console.log']
        zip_name = f"archive_{int(time.time())}.zip"
        zip_path = os.path.join(base, zip_name)
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for n in names:
                p = safe_path(folder, sub_path, n)
                if not p or n in (zip_name, 'console.log'): continue
                if os.path.isdir(p):
                    for root, dirs, files in os.walk(p):
                        for file in files:
                            full_p = os.path.join(root, file)
                            z.write(full_p, os.path.relpath(full_p, base))
                elif os.path.exists(p): z.write(p, n)
        return jsonify({'status': 'success', 'zip': zip_name})

    @app.route('/files/unzip/<folder>', methods=['POST'])
    @folder_access
    def unzip_file(folder):
        d = get_data()
        sub_path = d.get('path', '')
        base = safe_path(folder, sub_path)
        zip_path = safe_path(folder, sub_path, d.get('name') or '')
        root = safe_path(folder)
        if base and zip_path and os.path.isfile(zip_path) and zipfile.is_zipfile(zip_path):
            try:
                with zipfile.ZipFile(zip_path, 'r') as z:
                    for m in z.namelist():  # zip-slip protection
                        t = os.path.realpath(os.path.join(base, m))
                        if t != root and not t.startswith(root + os.sep):
                            return jsonify({'status': 'error', 'msg': 'Unsafe zip file'})
                    z.extractall(base)
                return jsonify({'status': 'success'})
            except Exception as e:
                return jsonify({'status': 'error', 'msg': str(e)})
        return jsonify({'status': 'error', 'msg': 'Invalid zip file'})

    # ---------- server control ----------
    @app.route('/server/action/<folder>/<act>', methods=['POST'])
    @folder_access
    def server_action(folder, act):
        db = get_db()
        row = db.execute('SELECT server_status, pid, startup FROM servers WHERE folder=?', (folder,)).fetchone()
        if row['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'This server is suspended by Admin.'})

        path = os.path.join(app.config['BASE_STORAGE'], folder)
        os.makedirs(path, exist_ok=True)
        log_file_path = os.path.join(path, 'console.log')
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if act == 'install':
            db.close()
            if os.path.exists(os.path.join(path, 'requirements.txt')):
                f_log = open(log_file_path, 'a')
                f_log.write(f"\n[{now}] 📦 Package Installation Started...\n")
                f_log.flush()
                # panel jei python-e cholche sei python-er pip (Railway-te 'pip' PATH-e na-o thakte pare)
                subprocess.Popen([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'requirements.txt'],
                                 cwd=path, stdout=f_log, stderr=f_log)
                return jsonify({'status': 'installing'})
            return jsonify({'status': 'error', 'msg': 'requirements.txt missing'})

        if act in ['start', 'restart']:
            startup_file = (row['startup'] or 'main.py').strip() or 'main.py'
            if not os.path.exists(os.path.join(path, startup_file.split()[0])):
                db.close()
                return jsonify({'status': 'error', 'msg': f"{startup_file.split()[0]} not found. Upload it or set the correct startup file."})
            t_pid = running_procs[folder].pid if folder in running_procs else row['pid']
            if t_pid and (folder in running_procs or is_our_proc(t_pid)): kill_group(t_pid)
            running_procs.pop(folder, None)
            f_log = open(log_file_path, 'a')
            f_log.write(f"\n[{now}] 🚀 Instance {act.upper()}ED Successfully\n")
            f_log.flush()
            # Anti-crash loop; -u = unbuffered, jate console log live dekha jay
            loop_cmd = f"while true; do {shlex.quote(sys.executable)} -u {startup_file}; echo 'Crashed! Restarting in 5s...'; sleep 5; done"
            proc = subprocess.Popen(['bash', '-c', loop_cmd], cwd=path, stdin=subprocess.PIPE, stdout=f_log, stderr=f_log,
                                    start_new_session=True)
            running_procs[folder], start_times[folder] = proc, time.time()
            db.execute('UPDATE servers SET pid=? WHERE folder=?', (proc.pid, folder))
            db.commit()
            db.close()
            return jsonify({'status': 'started'})

        if act == 'stop':
            t_pid = running_procs[folder].pid if folder in running_procs else row['pid']
            if t_pid and (folder in running_procs or is_our_proc(t_pid)): kill_group(t_pid)
            running_procs.pop(folder, None)
            start_times.pop(folder, None)
            db.execute('UPDATE servers SET pid=NULL WHERE folder=?', (folder,))
            db.commit()
            db.close()
            with open(log_file_path, 'a') as f: f.write(f"\n[{now}] 🛑 Instance STOPPED\n")
            return jsonify({'status': 'stopped'})
        db.close()
        return jsonify({'status': 'ok'})

    # dashboard console-er command box (/server/command) - age route-i chilo na
    @app.route('/server/command/<folder>', methods=['POST'])
    @folder_access
    def server_command(folder):
        cmd = get_data().get('command', '')
        proc = running_procs.get(folder)
        if not cmd or not proc or proc.poll() is not None or not proc.stdin:
            return jsonify({'status': 'error', 'msg': 'Server is not running'})
        try:
            proc.stdin.write((cmd + '\n').encode('utf-8'))
            proc.stdin.flush()
            with open(os.path.join(app.config['BASE_STORAGE'], folder, 'console.log'), 'a') as f: f.write(f"> {cmd}\n")
            return jsonify({'status': 'success'})
        except Exception:
            return jsonify({'status': 'error', 'msg': 'Could not send command'})

    @app.route('/server/log/<folder>')
    @folder_access
    def server_log(folder):
        path = os.path.join(app.config['BASE_STORAGE'], folder, 'console.log')
        if os.path.exists(path):
            with open(path, 'rb') as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 5000))
                return jsonify({'log': f.read().decode('utf-8', 'replace')})
        return jsonify({'log': 'Waiting for logs...'})

    @app.route('/server/set-startup/<folder>', methods=['POST'])
    @folder_access
    def set_startup(folder):
        cmd = request.json.get('file')
        db = get_db()
        db.execute('UPDATE servers SET startup=? WHERE folder=?', (cmd, folder))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/server/delete/<folder>', methods=['POST'])
    def delete_server(folder):
        if 'user_id' not in session:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        db = get_db()
        srv = db.execute('SELECT user_id, server_status, pid FROM servers WHERE folder=?', (folder,)).fetchone()
        if not srv:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Server not found'})
        if srv['user_id'] != session['user_id']:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Access denied'})
        if srv['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Suspended servers cannot be deleted!'})

        t_pid = running_procs[folder].pid if folder in running_procs else (srv['pid'] if srv else None)
        if t_pid:
            try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
            except: pass
        if folder in running_procs: del running_procs[folder]
        db.execute('DELETE FROM servers WHERE folder=?', (folder,))
        db.commit()
        db.close()
        path = os.path.join(app.config['BASE_STORAGE'], folder)
        if os.path.exists(path): shutil.rmtree(path)
        return jsonify({'status': 'deleted'})

    @app.route('/servers')
    def list_servers():
        if 'user_id' not in session: return jsonify({'servers': []})
        db = get_db()
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (session['user_id'],)).fetchall()
        db.close()
        srvs = []
        for r in rows:
            f, saved_pid = r['folder'], r['pid']
            online = False
            if saved_pid and psutil.pid_exists(saved_pid):
                try:
                    p = psutil.Process(saved_pid)
                    if p.is_running() and p.status() != psutil.STATUS_ZOMBIE: online = True
                except: pass
            elif f in running_procs and running_procs[f].poll() is None: online = True
            uptime = get_precise_uptime(start_times.get(f)) if online and f in start_times else ("Online" if online else "Offline")
            cpu, ram = "0%", "0MB"
            if online:
                try:
                    p_pid = running_procs[f].pid if f in running_procs else saved_pid
                    process = psutil.Process(p_pid)
                    cpu, ram = f"{process.cpu_percent(interval=None)}%", f"{process.memory_info().rss / (1024 * 1024):.1f}MB"
                except: pass
            srvs.append({'name': r['name'], 'folder': f, 'online': online, 'startup': r['startup'], 'uptime': uptime, 'cpu': cpu, 'ram': ram, 'status': r['server_status']})
        return jsonify({'servers': srvs})

    # --- 1. API সিস্টেম ফিক্স: /add (Free API & Dashboard Compatible) ---
    @app.route('/add', methods=['POST'])
    def add_srv():
        # Session অথবা JSON থেকে user_id নিবে (API এর জন্য)
        user_id = session.get('user_id')
        if not user_id:
            user_id = (request.get_json(silent=True) or {}).get('user_id')
        if not user_id: 
            user_id = 1 # API এর মাধ্যমে ডাইরেক্ট হিট করলে ডিফল্ট Admin (ID 1) এর আন্ডারে সেভ হবে
        
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        if not user:
            db.close()
            return jsonify({'status': 'error', 'msg': 'User not found!'})

        count = db.execute('SELECT COUNT(*) as count FROM servers WHERE user_id=?', (user_id,)).fetchone()['count']
        
        if user['role'] != 'admin' and count >= user['server_limit']:
            db.close()
            return jsonify({'status': 'error', 'msg': f"Limit Reached! Max: {user['server_limit']}"})
        
        name = (request.get_json(silent=True) or {}).get('name') or request.form.get('name')
        if not name:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Server name is required!'})

        folder = secure_filename(name).lower() + "_" + str(int(time.time()))
        db.execute('INSERT INTO servers (user_id, name, folder, status, startup) VALUES (?,?,?,?,?)', (user_id, name, folder, 'Offline', 'main.py'))
        db.commit()
        db.close()
        os.makedirs(os.path.join(app.config['BASE_STORAGE'], folder), exist_ok=True)
        return jsonify({'status': 'success', 'folder': folder})

    # --- 1. API সিস্টেম ফিক্স: /add_premium_api ---
    @app.route('/add_premium_api', methods=['POST'])
    def add_premium_api():
        # Session অথবা JSON থেকে user_id নিবে
        user_id = session.get('user_id')
        if not user_id:
            user_id = (request.get_json(silent=True) or {}).get('user_id')
        if not user_id: 
            user_id = 1 
            
        name = (request.get_json(silent=True) or {}).get('name') or request.form.get('name')
        if not name:
            return jsonify({'status': 'error', 'msg': 'Server name is required!'})

        db = get_db()
        folder = secure_filename(name).lower() + "_premium_" + str(int(time.time()))
        
        # Premium বট সরাসরি limit বাইপাস করে active status এ অ্যাড হবে
        db.execute('INSERT INTO servers (user_id, name, folder, status, startup, server_status) VALUES (?,?,?,?,?,?)', 
                   (user_id, name, folder, 'Offline', 'main.py', 'active'))
        db.commit()
        db.close()
        os.makedirs(os.path.join(app.config['BASE_STORAGE'], folder), exist_ok=True)
        return jsonify({'status': 'success', 'folder': folder, 'msg': 'Premium server added via API!'})

    return app

app = create_app()

def _loop_alive(pid):
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE and 'while true' in ' '.join(p.cmdline())
    except Exception:
        return False

# --- Railway Auto-Recovery: redeploy/restart hole running server gulo abar chalu hobe ---
def railway_auto_recover():
    print("Railway Recovery System Active. Waiting for DB...")
    time.sleep(5)
    try:
        db = get_db()
        active_srvs = db.execute("SELECT * FROM servers WHERE pid IS NOT NULL AND server_status='active'").fetchall()
        for srv in active_srvs:
            folder = srv['folder']
            if (folder in running_procs and running_procs[folder].poll() is None) or _loop_alive(srv['pid']):
                continue  # already cholche, duplicate start korbo na
            startup_file = (srv['startup'] or 'main.py').strip() or 'main.py'
            path = os.path.join(app.config['BASE_STORAGE'], folder)
            if os.path.exists(path):
                f_log = open(os.path.join(path, 'console.log'), 'a')
                f_log.write("\n[RECOVERED] System restarted. Resuming script with Anti-Crash...\n")
                f_log.flush()
                loop_cmd = f"while true; do {shlex.quote(sys.executable)} -u {startup_file}; echo 'Crashed! Restarting in 5s...'; sleep 5; done"
                proc = subprocess.Popen(['bash', '-c', loop_cmd], cwd=path, stdin=subprocess.PIPE, stdout=f_log, stderr=f_log,
                                        start_new_session=True)
                running_procs[folder] = proc
                start_times[folder] = time.time()
                db.execute('UPDATE servers SET pid=? WHERE folder=?', (proc.pid, folder))
        db.commit()
        db.close()
        print("Railway Auto-Recovery Completed Successfully.")
    except Exception as e:
        print(f"Recovery Error: {e}")

# gunicorn (Railway) e `python app.py` na cholleo recovery cholbe
_recovery_started = False
def start_recovery_once():
    global _recovery_started
    if _recovery_started: return
    _recovery_started = True
    threading.Thread(target=railway_auto_recover, daemon=True).start()

start_recovery_once()

if __name__ == "__main__":
    # local run: python app.py  (Railway-te Procfile-er gunicorn use hoy)
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
