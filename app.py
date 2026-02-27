from flask import Flask, jsonify, render_template, request, session, redirect, url_for, send_from_directory, abort
import json, os, datetime, uuid, hashlib, threading, time, logging, sys, traceback, hmac, base64, secrets, mimetypes
from functools import wraps
from werkzeug.utils import secure_filename

# ===== CONFIG =====
MB_POLLING_INTERVAL = 15
DOWNLOAD_LINK_SECRET = os.environ.get("DOWNLOAD_SECRET", secrets.token_hex(32))
DOWNLOAD_LINK_TTL = 3600  # 1 hour signed download links

# ===== UPLOAD CONFIG =====
UPLOAD_DIR     = os.environ.get("UPLOAD_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"))
MAX_IMAGE_SIZE = int(os.environ.get("MAX_IMAGE_MB",  "10"))  * 1024 * 1024
MAX_FILE_SIZE  = int(os.environ.get("MAX_FILE_MB",  "200")) * 1024 * 1024
ALLOWED_IMAGES = {"png","jpg","jpeg","gif","webp","svg"}
ALLOWED_FILES  = {"zip","rar","7z","tar","gz","pdf","txt","md","json",
                  "exe","apk","dmg","pkg","deb","mp4","mp3","wav",
                  "py","js","ts","html","css","php","docx","xlsx","pptx"}

CLOUDINARY_ENABLED = False
try:
    import cloudinary, cloudinary.uploader
    if os.environ.get("CLOUDINARY_URL"):
        cloudinary.config(cloudinary_url=os.environ["CLOUDINARY_URL"])
        CLOUDINARY_ENABLED = True
except ImportError:
    pass

def ensure_upload_dirs():
    for sub in ("images", "files"):
        os.makedirs(os.path.join(UPLOAD_DIR, sub), exist_ok=True)

ensure_upload_dirs()

mb_instance = None
mb_lock = threading.Lock()
mb_enabled = False

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ===== RAILWAY PERSISTENT STORAGE =====
# Trên Railway: thêm Volume mount tại /data, set DATA_DIR=/data trong env vars
# Nếu không có volume, dữ liệu sẽ mất khi redeploy!
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_FILE  = os.path.join(DATA_DIR, "database.json")
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shop")

# ===== RATE LIMITER (in-memory) =====
_rate_store = {}
_rate_lock = threading.Lock()

def rate_limit(key_prefix, max_calls, window_seconds):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
            key = f"{key_prefix}:{ip}"
            now = time.time()
            with _rate_lock:
                calls = [t for t in _rate_store.get(key, []) if now - t < window_seconds]
                if len(calls) >= max_calls:
                    retry_after = int(window_seconds - (now - calls[0]))
                    return jsonify({"ok": False, "msg": f"Quá nhiều yêu cầu. Thử lại sau {retry_after}s."}), 429
                calls.append(now)
                _rate_store[key] = calls
            return f(*args, **kwargs)
        return wrapped
    return decorator

# ===== DB =====
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def load_db():
    default_db = {
        "products": [], "users": [], "orders": [], "topup_requests": [],
        "processed_txids": [], "reviews": [], "notifications": [], "referral_rewards": [],
        "categories": ["Bot Zalo", "Bot Facebook", "Bot Discord", "API", "Source Code", "Tool"],
        "coupons": [],
        "admins": [{"username": "admin", "password": hash_pw("admin123")}],
        "mb_settings": {"username":"","password":"","account_number":"","bank_name":"MB Bank","account_holder":"","enabled":False},
        "settings": {"referral_reward": 10000, "referral_topup_bonus_pct": 5,
                     "invoice_shop_name": "Xelloxx Shop", "invoice_address": "", "invoice_phone": ""}
    }
    if not os.path.exists(DB_FILE):
        return default_db
    try:
        with open(DB_FILE, "r", encoding="utf8") as f:
            data = json.load(f)
        if not data or not isinstance(data, dict) or "products" not in data:
            return default_db
        for k, v in default_db.items():
            if k not in data:
                data[k] = v
        return data
    except:
        return default_db

def save_db(db):
    with open(DB_FILE, "w", encoding="utf8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

if not os.path.exists(DB_FILE):
    save_db(load_db())

# ===== SECURE DOWNLOAD TOKENS =====
def generate_download_token(order_id: str, user: str) -> str:
    expires = int(time.time()) + DOWNLOAD_LINK_TTL
    payload = f"{order_id}:{user}:{expires}"
    sig = hmac.new(DOWNLOAD_LINK_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()

def verify_download_token(token: str):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        parts = raw.split(":")
        if len(parts) != 4:
            return None
        order_id, user, expires_str, sig = parts
        if time.time() > int(expires_str):
            return None
        payload = f"{order_id}:{user}:{expires_str}"
        expected = hmac.new(DOWNLOAD_LINK_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return {"order_id": order_id, "user": user}
    except:
        return None

# ===== NOTIFICATIONS =====
def push_notification(db, username, title, body, notif_type="info"):
    notif = {"id": str(uuid.uuid4()), "username": username, "title": title, "body": body,
             "type": notif_type, "read": False,
             "created_at": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")}
    db.setdefault("notifications", []).append(notif)
    # Keep max 200 per user
    user_notifs = [n for n in db["notifications"] if n["username"] == username]
    if len(user_notifs) > 200:
        old = sorted(user_notifs, key=lambda x: x["created_at"])[:len(user_notifs)-200]
        old_ids = {n["id"] for n in old}
        db["notifications"] = [n for n in db["notifications"] if n["id"] not in old_ids]

# ===== AUTH DECORATORS =====
def require_admin(f):
    @wraps(f)
    def d(*a, **kw):
        if "admin" not in session:
            return jsonify({"ok": False, "msg": "Unauthorized"}), 401
        return f(*a, **kw)
    return d

def require_user(f):
    @wraps(f)
    def d(*a, **kw):
        if "user" not in session:
            return jsonify({"ok": False, "msg": "Chưa đăng nhập"}), 401
        return f(*a, **kw)
    return d

# ===== MB BANK =====
def get_mb_settings():
    return load_db().get("mb_settings", {})

def get_transaction_history(mb_inst, account_number, from_date, to_date):
    fd = datetime.datetime.strptime(from_date, "%d/%m/%Y") if isinstance(from_date, str) else from_date
    td = datetime.datetime.strptime(to_date, "%d/%m/%Y") if isinstance(to_date, str) else to_date
    result = mb_inst.getTransactionAccountHistory(accountNo=account_number, from_date=fd, to_date=td)
    return result, "getTransactionAccountHistory"

def extract_tx_list(result):
    if result is None: return []
    if isinstance(result, list): return result
    for attr in ['transactionHistoryList','transactions','data','items','result']:
        val = getattr(result, attr, None) if not isinstance(result, dict) else result.get(attr)
        if isinstance(val, list): return val
    return []

def get_tx_field(tx, *names, default=None):
    for name in names:
        val = tx.get(name) if isinstance(tx, dict) else getattr(tx, name, None)
        if val is not None: return val
    return default

def try_init_mb(debug=False):
    global mb_instance, mb_enabled
    logs = []
    def log(msg, level="INFO"):
        logs.append(f"[{level}] {msg}")
        getattr(logger, level.lower(), logger.info)(msg)
    s = get_mb_settings()
    if not s.get("enabled"): log("MB Bank chưa bật.","WARNING"); mb_enabled=False; return (False,logs) if debug else False
    if not s.get("username"): log("Thiếu username.","ERROR"); mb_enabled=False; return (False,logs) if debug else False
    if not s.get("password"): log("Thiếu password.","ERROR"); mb_enabled=False; return (False,logs) if debug else False
    try:
        import mbbank
        log(f"mbbank OK. Version: {getattr(mbbank,'__version__','?')}")
    except ImportError as e:
        log(f"Không có mbbank: {e}","ERROR"); mb_enabled=False; return (False,logs) if debug else False
    try:
        with mb_lock:
            mb_instance = mbbank.MBBank(username=s["username"], password=s["password"])
        mb_enabled = True; log("Init MBBank OK!")
        return (True,logs) if debug else True
    except Exception as e:
        log(f"Lỗi: {e}","ERROR"); mb_enabled=False; mb_instance=None
        return (False,logs) if debug else False

def poll_mb_transactions():
    global mb_instance, mb_enabled
    consecutive_errors = 0
    while True:
        time.sleep(MB_POLLING_INTERVAL)
        settings = get_mb_settings()
        if not settings.get("enabled"): consecutive_errors=0; continue
        if not mb_enabled or mb_instance is None:
            if not try_init_mb():
                consecutive_errors += 1
                if consecutive_errors > 5: time.sleep(300)
                continue
        try:
            account_number = settings.get("account_number","")
            if not account_number: continue
            today = datetime.datetime.now().strftime("%d/%m/%Y")
            with mb_lock:
                result, _ = get_transaction_history(mb_instance, account_number, today, today)
            tx_list = extract_tx_list(result)
            if not tx_list: continue
            db = load_db()
            processed = set(db.get("processed_txids",[]))
            shop_settings = db.get("settings",{})
            updated = False
            for tx in tx_list:
                ref_no  = get_tx_field(tx,'refNo','ref','transactionId','id',default='')
                tx_date = get_tx_field(tx,'transactionDate','date','createdAt',default='')
                credit  = float(get_tx_field(tx,'creditAmount','credit','amount',default=0) or 0)
                desc    = str(get_tx_field(tx,'description','remark','note',default='') or '').upper()
                tx_id   = str(ref_no).strip() or (str(tx_date)+str(credit))
                if tx_id in processed: continue
                if credit <= 0: processed.add(tx_id); continue
                matched = next((r for r in db.get("topup_requests",[]) if r.get("status")=="pending" and r.get("code","").upper() in desc), None)
                if matched:
                    xu = int(credit)
                    for u in db["users"]:
                        if u["username"] == matched["username"]:
                            u["balance"] = u.get("balance",0) + xu
                            # Referral topup bonus
                            ref_by = u.get("referred_by","")
                            bp = shop_settings.get("referral_topup_bonus_pct",5)
                            if ref_by and bp > 0:
                                bonus = int(xu * bp / 100)
                                for ru in db["users"]:
                                    if ru["username"] == ref_by:
                                        ru["balance"] = ru.get("balance",0) + bonus
                                        push_notification(db, ref_by, "💰 Thưởng giới thiệu",
                                            f"{matched['username']} nạp tiền. Bạn nhận {bonus:,} xu ({bp}%)!","success")
                                        db.setdefault("referral_rewards",[]).append({"id":str(uuid.uuid4()),
                                            "referrer":ref_by,"referred":matched["username"],"type":"topup_bonus",
                                            "amount":bonus,"created_at":datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")})
                                        break
                            break
                    matched.update({"status":"completed","amount_received":int(credit),"xu_added":xu,
                        "completed_at":datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),"tx_ref":tx_id})
                    push_notification(db, matched["username"], "✅ Nạp tiền thành công",
                        f"Đã nhận {xu:,} xu vào tài khoản.","success")
                    updated = True
                processed.add(tx_id)
            if updated or len(processed) != len(db.get("processed_txids",[])):
                db["processed_txids"] = list(processed)[-2000:]
                save_db(db)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Polling error: {e}")
            if consecutive_errors >= 3:
                mb_enabled=False; mb_instance=None

threading.Thread(target=poll_mb_transactions, daemon=True).start()

# ===== PUBLIC PAGES =====
def _placeholder(): pass
@app.route("/")
def index(): return render_template("index.html")
@app.route("/product/<pid>")
def product_detail(pid): return render_template("product.html", product_id=pid)
def _placeholder(): pass
@app.route("/login")
def login_page(): return render_template("login.html")
def _placeholder(): pass
@app.route("/register")
def register_page(): return render_template("register.html")
def _placeholder(): pass
@app.route("/checkout")
def checkout_page(): return render_template("checkout.html")
def _placeholder(): pass
@app.route("/topup")
def topup_page(): return render_template("topup.html")
def _placeholder(): pass
@app.route("/orders")
def orders_page(): return render_template("orders.html")
def _placeholder(): pass
@app.route("/cart")
def cart_page(): return render_template("cart.html")
def _placeholder(): pass
@app.route("/referral")
def referral_page(): return render_template("referral.html")
def _placeholder(): pass
@app.route("/notifications")
def notifications_page(): return render_template("notifications.html")

# ===== PRODUCTS API =====
@app.route("/api/products")
def get_products():
    db = load_db()
    cat=request.args.get("category",""); search=request.args.get("search","").lower(); ptype=request.args.get("type","")
    products = db["products"]
    if cat: products=[p for p in products if p.get("category")==cat]
    if search: products=[p for p in products if search in p["name"].lower() or search in p.get("description","").lower()]
    if ptype: products=[p for p in products if p.get("type")==ptype]
    reviews=db.get("reviews",[])
    for p in products:
        pr=[r for r in reviews if r["product_id"]==p["id"] and r.get("approved",True)]
        p["rating_avg"]=round(sum(r["rating"] for r in pr)/len(pr),1) if pr else 0
        p["rating_count"]=len(pr)
    return jsonify(products)

@app.route("/api/products/<pid>")
def get_product(pid):
    db = load_db()
    p = next((x for x in db["products"] if x["id"]==pid), None)
    if not p: return jsonify({"error":"Not found"}),404
    p["views"]=p.get("views",0)+1
    reviews=[r for r in db.get("reviews",[]) if r["product_id"]==pid and r.get("approved",True)]
    p["rating_avg"]=round(sum(r["rating"] for r in reviews)/len(reviews),1) if reviews else 0
    p["rating_count"]=len(reviews)
    save_db(db)
    return jsonify(p)

@app.route("/api/categories")
def get_categories():
    return jsonify(load_db()["categories"])

@app.route("/api/stats")
def get_stats():
    db=load_db()
    return jsonify({"products":len(db["products"]),"users":len(db["users"]),"orders":len(db["orders"])})

# ===== AUTH =====
@app.route("/api/register", methods=["POST"])
@rate_limit("register", 5, 300)
def register():
    db=load_db(); data=request.json
    if any(u["username"]==data["username"] for u in db["users"]):
        return jsonify({"ok":False,"msg":"Tên đăng nhập đã tồn tại"})
    if any(u["email"]==data["email"] for u in db["users"]):
        return jsonify({"ok":False,"msg":"Email đã được sử dụng"})
    ref_code=data.get("referral_code","").strip().upper()
    referred_by=""
    if ref_code:
        referrer=next((u for u in db["users"] if u.get("referral_code","").upper()==ref_code),None)
        if not referrer: return jsonify({"ok":False,"msg":"Mã giới thiệu không hợp lệ"})
        referred_by=referrer["username"]
    new_ref=data["username"].upper()[:6]+str(uuid.uuid4())[:4].upper()
    new_user={"id":str(uuid.uuid4()),"username":data["username"],"email":data["email"],
              "password":hash_pw(data["password"]),"balance":0,"referral_code":new_ref,
              "referred_by":referred_by,"cart":[],"created_at":datetime.datetime.now().strftime("%d/%m/%Y")}
    db["users"].append(new_user)
    if referred_by:
        reward=db.get("settings",{}).get("referral_reward",10000)
        for ru in db["users"]:
            if ru["username"]==referred_by:
                ru["balance"]=ru.get("balance",0)+reward
                push_notification(db,referred_by,"🎁 Thưởng giới thiệu",
                    f"{data['username']} đã đăng ký qua link của bạn. Nhận {reward:,} xu!","success")
                db.setdefault("referral_rewards",[]).append({"id":str(uuid.uuid4()),
                    "referrer":referred_by,"referred":data["username"],"type":"register_bonus",
                    "amount":reward,"created_at":datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")})
                break
    save_db(db)
    return jsonify({"ok":True})

@app.route("/api/login", methods=["POST"])
@rate_limit("login", 10, 300)
def login():
    db=load_db(); data=request.json
    user=next((u for u in db["users"] if u["username"]==data["username"] and u["password"]==hash_pw(data["password"])),None)
    if user:
        session["user"]=user["username"]
        return jsonify({"ok":True,"user":user["username"]})
    return jsonify({"ok":False,"msg":"Sai tên đăng nhập hoặc mật khẩu"})

@app.route("/api/logout")
def logout():
    session.clear(); return jsonify({"ok":True})

@app.route("/api/me")
def me():
    if "user" not in session: return jsonify({"ok":False})
    db=load_db()
    user=next((u for u in db["users"] if u["username"]==session["user"]),None)
    if not user: return jsonify({"ok":False})
    unread=len([n for n in db.get("notifications",[]) if n["username"]==session["user"] and not n.get("read")])
    return jsonify({"ok":True,"username":user["username"],"balance":user.get("balance",0),
                    "referral_code":user.get("referral_code",""),"unread_notifications":unread})

@app.route("/api/my-orders")
@require_user
def my_orders():
    db=load_db()
    orders=sorted([o for o in db.get("orders",[]) if o.get("user")==session["user"]],
                  key=lambda x:x.get("created_at",""),reverse=True)
    return jsonify({"ok":True,"orders":orders[:50]})

# ===== SECURE DOWNLOAD =====
@app.route("/api/download/<order_id>")
@require_user
def get_download_link(order_id):
    db=load_db()
    order=next((o for o in db.get("orders",[]) if o["id"]==order_id and o["user"]==session["user"]),None)
    if not order: return jsonify({"ok":False,"msg":"Đơn hàng không tồn tại"}),404
    token=generate_download_token(order_id,session["user"])
    return jsonify({"ok":True,"download_url":f"/download/{token}","expires_in":DOWNLOAD_LINK_TTL})

@app.route("/download/<token>")
def secure_download(token):
    info=verify_download_token(token)
    if not info: abort(403)
    if session.get("user")!=info["user"]:
        return redirect(f"/login?next=/download/{token}")
    db=load_db()
    order=next((o for o in db.get("orders",[]) if o["id"]==info["order_id"] and o["user"]==info["user"]),None)
    if not order: abort(404)
    file_url=order.get("file_url","")
    if not file_url: return jsonify({"error":"Không có file"}),404
    if file_url.startswith("http"): return redirect(file_url)
    return send_from_directory("files", file_url)

# ===== CART =====
@app.route("/api/cart")
@require_user
def get_cart():
    db=load_db()
    user=next((u for u in db["users"] if u["username"]==session["user"]),None)
    cart=user.get("cart",[]) if user else []
    result=[]
    for item in cart:
        p=next((x for x in db["products"] if x["id"]==item["product_id"]),None)
        if p: result.append({**item,"product":p})
    return jsonify({"ok":True,"cart":result})

@app.route("/api/cart/add", methods=["POST"])
@require_user
@rate_limit("cart_add",30,60)
def add_to_cart():
    db=load_db(); pid=request.json.get("product_id")
    if not next((p for p in db["products"] if p["id"]==pid),None):
        return jsonify({"ok":False,"msg":"Sản phẩm không tồn tại"}),404
    for u in db["users"]:
        if u["username"]==session["user"]:
            cart=u.setdefault("cart",[])
            if not any(i["product_id"]==pid for i in cart):
                cart.append({"product_id":pid,"added_at":datetime.datetime.now().strftime("%d/%m/%Y %H:%M")})
            break
    save_db(db); return jsonify({"ok":True})

@app.route("/api/cart/remove", methods=["POST"])
@require_user
def remove_from_cart():
    db=load_db(); pid=request.json.get("product_id")
    for u in db["users"]:
        if u["username"]==session["user"]:
            u["cart"]=[i for i in u.get("cart",[]) if i["product_id"]!=pid]; break
    save_db(db); return jsonify({"ok":True})

@app.route("/api/cart/clear", methods=["POST"])
@require_user
def clear_cart():
    db=load_db()
    for u in db["users"]:
        if u["username"]==session["user"]:
            u["cart"]=[]; break
    save_db(db); return jsonify({"ok":True})

# ===== TOPUP =====
@app.route("/api/topup/info")
def topup_info():
    s=load_db().get("mb_settings",{})
    return jsonify({"bank_name":s.get("bank_name","MB Bank"),"account_number":s.get("account_number",""),
                    "account_holder":s.get("account_holder",""),"enabled":s.get("enabled",False)})

@app.route("/api/topup/create", methods=["POST"])
@require_user
@rate_limit("topup_create",5,300)
def topup_create():
    db=load_db(); data=request.json; amount=int(data.get("amount",0))
    if amount<10000: return jsonify({"ok":False,"msg":"Tối thiểu 10,000đ"})
    if amount>50000000: return jsonify({"ok":False,"msg":"Tối đa 50,000,000đ"})
    code=f"NAP{session['user'].upper()[:6]}{str(uuid.uuid4())[:6].upper()}"
    req={"id":str(uuid.uuid4()),"username":session["user"],"amount":amount,"code":code,"status":"pending",
         "created_at":datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
         "expires_at":(datetime.datetime.now()+datetime.timedelta(minutes=30)).strftime("%d/%m/%Y %H:%M:%S")}
    db.setdefault("topup_requests",[]).append(req); save_db(db)
    return jsonify({"ok":True,"request":req})

@app.route("/api/topup/status/<req_id>")
@require_user
def topup_status(req_id):
    db=load_db()
    req=next((r for r in db.get("topup_requests",[]) if r["id"]==req_id and r["username"]==session["user"]),None)
    if not req: return jsonify({"ok":False,"msg":"Không tìm thấy"})
    user=next((u for u in db["users"] if u["username"]==session["user"]),None)
    return jsonify({"ok":True,"status":req["status"],"amount":req.get("amount"),
                    "xu_added":req.get("xu_added",0),"balance":user.get("balance",0) if user else 0})

@app.route("/api/topup/history")
@require_user
def topup_history():
    db=load_db()
    h=sorted([r for r in db.get("topup_requests",[]) if r["username"]==session["user"]],
             key=lambda x:x.get("created_at",""),reverse=True)
    return jsonify(h[:20])

# ===== CHECKOUT =====
@app.route("/api/checkout", methods=["POST"])
@require_user
@rate_limit("checkout",10,60)
def do_checkout():
    db=load_db(); data=request.json; pid=data.get("product_id"); coupon=data.get("coupon","").strip().upper()
    product=next((p for p in db["products"] if p["id"]==pid),None)
    if not product: return jsonify({"ok":False,"msg":"Sản phẩm không tồn tại"}),404
    if product.get("stock",-1)==0: return jsonify({"ok":False,"msg":"Sản phẩm đã hết hàng"})
    user=next((u for u in db["users"] if u["username"]==session["user"]),None)
    price=product["sale_price"] if product.get("sale_price",0)>0 else product["price"]
    discount=0; coupon_used=None
    if coupon:
        co=next((c for c in db.get("coupons",[]) if c["code"].upper()==coupon and c.get("active",True)),None)
        if not co: return jsonify({"ok":False,"msg":"Mã giảm giá không hợp lệ"})
        ul=co.get("max_uses",0)-co.get("used_count",0)
        if co.get("max_uses",0)>0 and ul<=0: return jsonify({"ok":False,"msg":"Mã đã hết lượt"})
        if price<co.get("min_price",0): return jsonify({"ok":False,"msg":f"Đơn tối thiểu {co['min_price']:,}đ"})
        if co["type"]=="percent":
            discount=int(price*co["value"]/100)
            if co.get("max_discount",0)>0: discount=min(discount,co["max_discount"])
        else: discount=co["value"]
        coupon_used=co
    final=max(0,price-discount)
    if user["balance"]<final: return jsonify({"ok":False,"msg":"Số dư không đủ"})
    for u in db["users"]:
        if u["username"]==session["user"]:
            u["balance"]-=final
            u["cart"]=[i for i in u.get("cart",[]) if i["product_id"]!=pid]
            break
    if product.get("stock",-1)>0:
        for p in db["products"]:
            if p["id"]==pid: p["stock"]=max(0,p.get("stock",1)-1)
    order={"id":str(uuid.uuid4()),"user":session["user"],"product_id":pid,"product_name":product["name"],
           "amount":final,"original_price":price,"coupon":coupon if coupon_used else "","discount":discount,
           "file_url":product.get("file_url",""),"status":"completed",
           "created_at":datetime.datetime.now().strftime("%d/%m/%Y %H:%M")}
    db["orders"].append(order)
    if coupon_used:
        for c in db["coupons"]:
            if c["code"]==coupon_used["code"]: c["used_count"]=c.get("used_count",0)+1
    push_notification(db,session["user"],"🛍️ Mua hàng thành công",
        f"Đã mua '{product['name']}' với giá {final:,} xu.","success")
    save_db(db)
    token=generate_download_token(order["id"],session["user"]) if order.get("file_url") else ""
    return jsonify({"ok":True,"order":order,"download_url":f"/download/{token}" if token else ""})

@app.route("/api/checkout/cart", methods=["POST"])
@require_user
@rate_limit("checkout_cart",5,60)
def checkout_cart():
    db=load_db(); data=request.json; coupon=data.get("coupon","").strip().upper()
    user=next((u for u in db["users"] if u["username"]==session["user"]),None)
    cart=user.get("cart",[])
    if not cart: return jsonify({"ok":False,"msg":"Giỏ hàng trống"})
    items=[]
    for item in cart:
        p=next((x for x in db["products"] if x["id"]==item["product_id"]),None)
        if p and p.get("stock",-1)!=0:
            items.append((p,p["sale_price"] if p.get("sale_price",0)>0 else p["price"]))
    if not items: return jsonify({"ok":False,"msg":"Không có sản phẩm hợp lệ"})
    total=sum(pr for _,pr in items)
    discount=0; coupon_used=None
    if coupon:
        co=next((c for c in db.get("coupons",[]) if c["code"].upper()==coupon and c.get("active",True)),None)
        if co:
            ul=co.get("max_uses",0)-co.get("used_count",0)
            if not(co.get("max_uses",0)>0 and ul<=0):
                discount=int(total*co["value"]/100) if co["type"]=="percent" else co["value"]
                if co.get("max_discount",0)>0: discount=min(discount,co["max_discount"])
                coupon_used=co
    final=max(0,total-discount)
    if user["balance"]<final: return jsonify({"ok":False,"msg":f"Cần {final:,} xu, có {user['balance']:,} xu"})
    for u in db["users"]:
        if u["username"]==session["user"]:
            u["balance"]-=final; u["cart"]=[]; break
    orders_created=[]
    item_count=len(items)
    for p,price in items:
        if p.get("stock",-1)>0:
            for prod in db["products"]:
                if prod["id"]==p["id"]: prod["stock"]=max(0,prod.get("stock",1)-1)
        ord_discount=discount//item_count
        order={"id":str(uuid.uuid4()),"user":session["user"],"product_id":p["id"],"product_name":p["name"],
               "amount":price-ord_discount,"original_price":price,"coupon":coupon if coupon_used else "",
               "discount":ord_discount,"file_url":p.get("file_url",""),"status":"completed",
               "created_at":datetime.datetime.now().strftime("%d/%m/%Y %H:%M")}
        db["orders"].append(order)
        token=generate_download_token(order["id"],session["user"]) if order.get("file_url") else ""
        orders_created.append({**order,"download_url":f"/download/{token}" if token else ""})
    if coupon_used:
        for c in db["coupons"]:
            if c["code"]==coupon_used["code"]: c["used_count"]=c.get("used_count",0)+1
    push_notification(db,session["user"],"🛒 Thanh toán giỏ hàng",f"Mua {len(orders_created)} sản phẩm. Tổng {final:,} xu.","success")
    save_db(db)
    return jsonify({"ok":True,"orders":orders_created,"total":final})

# ===== REVIEWS =====
@app.route("/api/reviews/<pid>")
def get_reviews(pid):
    db=load_db()
    reviews=sorted([r for r in db.get("reviews",[]) if r["product_id"]==pid and r.get("approved",True)],
                   key=lambda x:x.get("created_at",""),reverse=True)
    return jsonify(reviews)

@app.route("/api/reviews", methods=["POST"])
@require_user
@rate_limit("review",5,3600)
def add_review():
    db=load_db(); data=request.json; pid=data.get("product_id")
    rating=int(data.get("rating",5)); content=data.get("content","").strip()
    if not 1<=rating<=5: return jsonify({"ok":False,"msg":"Điểm từ 1-5"})
    if len(content)<5: return jsonify({"ok":False,"msg":"Nội dung quá ngắn"})
    if len(content)>1000: return jsonify({"ok":False,"msg":"Tối đa 1000 ký tự"})
    if not any(o["user"]==session["user"] and o["product_id"]==pid for o in db.get("orders",[])):
        return jsonify({"ok":False,"msg":"Bạn cần mua sản phẩm trước khi đánh giá"})
    if next((r for r in db.get("reviews",[]) if r["product_id"]==pid and r["username"]==session["user"]),None):
        return jsonify({"ok":False,"msg":"Bạn đã đánh giá sản phẩm này rồi"})
    review={"id":str(uuid.uuid4()),"product_id":pid,"username":session["user"],"rating":rating,
            "content":content,"approved":True,"created_at":datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")}
    db.setdefault("reviews",[]).append(review); save_db(db)
    return jsonify({"ok":True,"review":review})

# ===== NOTIFICATIONS =====
@app.route("/api/notifications")
@require_user
def get_notifications():
    db=load_db()
    ns=sorted([n for n in db.get("notifications",[]) if n["username"]==session["user"]],
              key=lambda x:x.get("created_at",""),reverse=True)
    return jsonify({"ok":True,"notifications":ns[:50]})

@app.route("/api/notifications/read", methods=["POST"])
@require_user
def mark_read():
    db=load_db(); nid=request.json.get("id")
    for n in db.get("notifications",[]):
        if n["username"]==session["user"] and (nid is None or n["id"]==nid):
            n["read"]=True
    save_db(db); return jsonify({"ok":True})

@app.route("/api/notifications/unread-count")
@require_user
def unread_count():
    db=load_db()
    count=len([n for n in db.get("notifications",[]) if n["username"]==session["user"] and not n.get("read")])
    return jsonify({"ok":True,"count":count})

# ===== REFERRAL =====
@app.route("/api/referral/info")
@require_user
def referral_info():
    db=load_db()
    user=next((u for u in db["users"] if u["username"]==session["user"]),None)
    rewards=[r for r in db.get("referral_rewards",[]) if r["referrer"]==session["user"]]
    base_url=request.host_url.rstrip("/")
    s=db.get("settings",{})
    return jsonify({"ok":True,"referral_code":user.get("referral_code",""),
                    "referral_link":f"{base_url}/register?ref={user.get('referral_code','')}",
                    "total_referred":len(set(r["referred"] for r in rewards)),
                    "total_earned":sum(r["amount"] for r in rewards),"rewards":rewards[-20:],
                    "register_reward":s.get("referral_reward",10000),
                    "topup_bonus_pct":s.get("referral_topup_bonus_pct",5)})

# ===== INVOICE =====
@app.route("/api/invoice/<order_id>")
@require_user
def get_invoice(order_id):
    db=load_db()
    order=next((o for o in db.get("orders",[]) if o["id"]==order_id and o["user"]==session["user"]),None)
    if not order: return jsonify({"ok":False,"msg":"Đơn hàng không tồn tại"}),404
    user=next((u for u in db["users"] if u["username"]==session["user"]),None)
    s=db.get("settings",{})
    return jsonify({"ok":True,"invoice":{"invoice_no":f"INV-{order['id'][:8].upper()}","order":order,
        "user":{"username":user["username"],"email":user.get("email","")},"shop_name":s.get("invoice_shop_name","Xelloxx Shop"),
        "shop_address":s.get("invoice_address",""),"shop_phone":s.get("invoice_phone",""),
        "issued_at":datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")}})

# ===== COUPON CHECK =====
@app.route("/api/coupon/check", methods=["POST"])
@rate_limit("coupon",20,300)
def coupon_check():
    db=load_db(); data=request.json; code=data.get("code","").strip().upper(); price=int(data.get("price",0))
    if not code: return jsonify({"ok":False,"msg":"Nhập mã giảm giá"})
    co=next((c for c in db.get("coupons",[]) if c["code"].upper()==code and c.get("active",True)),None)
    if not co: return jsonify({"ok":False,"msg":"Mã không hợp lệ hoặc đã vô hiệu hóa"})
    ul=co.get("max_uses",0)-co.get("used_count",0)
    if co.get("max_uses",0)>0 and ul<=0: return jsonify({"ok":False,"msg":"Mã đã hết lượt"})
    if price>0 and price<co.get("min_price",0): return jsonify({"ok":False,"msg":f"Đơn tối thiểu {co['min_price']:,}đ"})
    if co["type"]=="percent":
        discount=int(price*co["value"]/100) if price>0 else 0
        if co.get("max_discount",0)>0: discount=min(discount,co["max_discount"])
        desc=f"Giảm {co['value']}%"+(f" (tối đa {co['max_discount']:,}đ)" if co.get("max_discount",0)>0 else "")
    else:
        discount=co["value"]; desc=f"Giảm {discount:,}đ"
    return jsonify({"ok":True,"discount":discount,"description":desc,"uses_left":ul if co.get("max_uses",0)>0 else -1})


# ===== FILE UPLOAD HELPERS =====

def _ext(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

def _safe_name(filename, prefix=""):
    ext = _ext(filename)
    return f"{prefix}{uuid.uuid4().hex}.{ext}"

def upload_to_cloudinary(file_data, folder, resource_type="auto"):
    result = cloudinary.uploader.upload(
        file_data,
        folder=f"xelloxx/{folder}",
        resource_type=resource_type
    )
    return result.get("secure_url", "")

def save_local(file_storage, subdir):
    """Save werkzeug FileStorage to UPLOAD_DIR/subdir/. Returns public URL path."""
    fname = _safe_name(file_storage.filename)
    dest  = os.path.join(UPLOAD_DIR, subdir, fname)
    file_storage.save(dest)
    return f"/uploads/{subdir}/{fname}"

# ===== UPLOAD ROUTES =====

@app.route("/uploads/<subdir>/<filename>")
def serve_upload(subdir, filename):
    """Serve locally stored uploads."""
    safe_sub  = secure_filename(subdir)
    safe_file = secure_filename(filename)
    if safe_sub not in ("images", "files"):
        abort(404)
    return send_from_directory(os.path.join(UPLOAD_DIR, safe_sub), safe_file)

@app.route("/api/admin/upload/image", methods=["POST"])
@require_admin
def admin_upload_image():
    """Upload product image. Returns public URL."""
    if "file" not in request.files:
        return jsonify({"ok": False, "msg": "Không có file"}), 400
    f   = request.files["file"]
    ext = _ext(f.filename)
    if ext not in ALLOWED_IMAGES:
        return jsonify({"ok": False, "msg": f"Chỉ hỗ trợ: {', '.join(sorted(ALLOWED_IMAGES))}"}), 400
    # Read & check size
    data = f.read()
    if len(data) > MAX_IMAGE_SIZE:
        return jsonify({"ok": False, "msg": f"Ảnh tối đa {MAX_IMAGE_SIZE//1024//1024} MB"}), 413
    f.seek(0)
    try:
        if CLOUDINARY_ENABLED:
            url = upload_to_cloudinary(data, "images", resource_type="image")
        else:
            url = save_local(f, "images")
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        logger.error(f"Image upload error: {e}")
        return jsonify({"ok": False, "msg": f"Lỗi upload: {str(e)}"}), 500

@app.route("/api/admin/upload/file", methods=["POST"])
@require_admin
def admin_upload_file():
    """Upload downloadable product file. Returns public URL."""
    if "file" not in request.files:
        return jsonify({"ok": False, "msg": "Không có file"}), 400
    f   = request.files["file"]
    ext = _ext(f.filename)
    if ext not in ALLOWED_FILES:
        return jsonify({"ok": False, "msg": f"Loại file không được hỗ trợ"}), 400
    # Stream to disk without reading all into memory
    fname = _safe_name(f.filename)
    dest  = os.path.join(UPLOAD_DIR, "files", fname)
    size  = 0
    try:
        with open(dest, "wb") as out:
            chunk_size = 64 * 1024  # 64 KB chunks
            while True:
                chunk = f.stream.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    out.close()
                    os.remove(dest)
                    return jsonify({"ok": False, "msg": f"File tối đa {MAX_FILE_SIZE//1024//1024} MB"}), 413
                out.write(chunk)
        url = f"/uploads/files/{fname}"
        file_info = {
            "name": secure_filename(f.filename),
            "size": size,
            "size_mb": round(size / 1024 / 1024, 2),
            "ext": ext,
            "url": url
        }
        return jsonify({"ok": True, **file_info})
    except Exception as e:
        if os.path.exists(dest):
            os.remove(dest)
        logger.error(f"File upload error: {e}")
        return jsonify({"ok": False, "msg": f"Lỗi upload: {str(e)}"}), 500

@app.route("/api/admin/uploads", methods=["GET"])
@require_admin
def admin_list_uploads():
    """List all uploaded files with metadata."""
    result = {"images": [], "files": []}
    for subdir in ("images", "files"):
        path = os.path.join(UPLOAD_DIR, subdir)
        if not os.path.exists(path):
            continue
        for fname in sorted(os.listdir(path), reverse=True):
            fpath = os.path.join(path, fname)
            if not os.path.isfile(fpath):
                continue
            stat   = os.stat(fpath)
            result[subdir].append({
                "filename": fname,
                "url": f"/uploads/{subdir}/{fname}",
                "size": stat.st_size,
                "size_mb": round(stat.st_size / 1024 / 1024, 2),
                "created_at": datetime.datetime.fromtimestamp(stat.st_ctime).strftime("%d/%m/%Y %H:%M"),
                "ext": _ext(fname)
            })
    return jsonify({"ok": True, **result})

@app.route("/api/admin/uploads/<subdir>/<filename>", methods=["DELETE"])
@require_admin
def admin_delete_upload(subdir, filename):
    """Delete an uploaded file."""
    if subdir not in ("images", "files"):
        return jsonify({"ok": False, "msg": "Invalid dir"}), 400
    safe_file = secure_filename(filename)
    path = os.path.join(UPLOAD_DIR, subdir, safe_file)
    if not os.path.exists(path):
        return jsonify({"ok": False, "msg": "File không tồn tại"}), 404
    os.remove(path)
    return jsonify({"ok": True})

# ===== ADMIN PAGES =====
def _placeholder(): pass
@app.route("/admin")
def admin_redirect(): return redirect("/admin/dashboard")
@app.route("/admin/dashboard")
@app.route("/admin/products")
@app.route("/admin/orders")
@app.route("/admin/users")
@app.route("/admin/settings")
@app.route("/admin/coupons")
@app.route("/admin/reviews")
@app.route("/admin/notifications")
@app.route("/admin/referrals")
def admin_panel():
    return render_template("admin.html")
def _placeholder(): pass
@app.route("/admin/login")
def admin_login_page(): return render_template("admin_login.html")

@app.route("/api/admin/login", methods=["POST"])
@rate_limit("admin_login",5,300)
def admin_login():
    db=load_db(); data=request.json
    admin=next((a for a in db["admins"] if a["username"]==data["username"] and a["password"]==hash_pw(data["password"])),None)
    if admin: session["admin"]=admin["username"]; return jsonify({"ok":True})
    return jsonify({"ok":False,"msg":"Sai thông tin đăng nhập"})

@app.route("/api/admin/logout")
def admin_logout():
    session.pop("admin",None); return jsonify({"ok":True})

@app.route("/api/admin/check")
def admin_check():
    return jsonify({"ok":"admin" in session,"username":session.get("admin")})

@app.route("/api/admin/stats")
@require_admin
def admin_stats():
    db=load_db()
    return jsonify({"products":len(db["products"]),"users":len(db["users"]),"orders":len(db["orders"]),
                    "revenue":sum(o.get("amount",0) for o in db["orders"]),
                    "reviews":len(db.get("reviews",[])),
                    "pending_topups":len([r for r in db.get("topup_requests",[]) if r["status"]=="pending"]),
                    "referral_rewards_paid":sum(r["amount"] for r in db.get("referral_rewards",[]))})

@app.route("/api/admin/products", methods=["GET"])
@require_admin
def admin_products():
    return jsonify(load_db()["products"])

@app.route("/api/admin/products", methods=["POST"])
@require_admin
def admin_add_product():
    db=load_db(); data=request.json
    product={"id":str(uuid.uuid4()),"name":data["name"],"description":data.get("description",""),
             "price":int(data.get("price",0)),"sale_price":int(data.get("sale_price",0)),
             "category":data.get("category",""),"type":data.get("type","sell"),
             "image":data.get("image",""),"file_url":data.get("file_url",""),"views":0,
             "created_at":datetime.datetime.now().strftime("%d/%m/%Y"),"featured":bool(data.get("featured",False)),
             "stock":int(data.get("stock",-1))}
    db["products"].append(product); save_db(db); return jsonify({"ok":True,"product":product})

@app.route("/api/admin/products/<pid>", methods=["PUT"])
@require_admin
def admin_update_product(pid):
    db=load_db(); data=request.json
    for i,p in enumerate(db["products"]):
        if p["id"]==pid:
            db["products"][i].update({"name":data.get("name",p["name"]),"description":data.get("description",p["description"]),
                "price":int(data.get("price",p["price"])),"sale_price":int(data.get("sale_price",p["sale_price"])),
                "category":data.get("category",p["category"]),"type":data.get("type",p["type"]),
                "image":data.get("image",p["image"]),"file_url":data.get("file_url",p["file_url"]),
                "featured":bool(data.get("featured",p["featured"])),"stock":int(data.get("stock",p["stock"]))})
            save_db(db); return jsonify({"ok":True})
    return jsonify({"ok":False,"msg":"Not found"}),404

@app.route("/api/admin/products/<pid>", methods=["DELETE"])
@require_admin
def admin_delete_product(pid):
    db=load_db(); db["products"]=[p for p in db["products"] if p["id"]!=pid]; save_db(db); return jsonify({"ok":True})

@app.route("/api/admin/users")
@require_admin
def admin_users():
    db=load_db()
    return jsonify([{k:v for k,v in u.items() if k!="password"} for u in db["users"]])

@app.route("/api/admin/orders")
@require_admin
def admin_orders():
    return jsonify(load_db()["orders"])

@app.route("/api/admin/topup-requests")
@require_admin
def admin_topup_requests():
    db=load_db()
    reqs=sorted(db.get("topup_requests",[]),key=lambda x:x.get("created_at",""),reverse=True)
    return jsonify(reqs)

@app.route("/api/admin/mb-settings", methods=["GET"])
@require_admin
def admin_get_mb_settings():
    db=load_db(); s=db.get("mb_settings",{})
    safe={k:v for k,v in s.items() if k!="password"}; safe["password_set"]=bool(s.get("password",""))
    return jsonify(safe)

@app.route("/api/admin/mb-settings", methods=["POST"])
@require_admin
def admin_save_mb_settings():
    global mb_instance, mb_enabled
    db=load_db(); data=request.json
    db.setdefault("mb_settings",{}).update({"username":data.get("username",""),"account_number":data.get("account_number",""),
        "account_holder":data.get("account_holder",""),"bank_name":data.get("bank_name","MB Bank"),"enabled":bool(data.get("enabled",False))})
    if data.get("password"): db["mb_settings"]["password"]=data["password"]
    save_db(db)
    with mb_lock: mb_instance=None; mb_enabled=False
    return jsonify({"ok":True})

@app.route("/api/admin/mb-test")
@require_admin
def admin_mb_test():
    success,logs=try_init_mb(debug=True)
    if not success: return jsonify({"ok":False,"msg":"Không thể khởi tạo MB Bank","debug_logs":logs})
    try:
        with mb_lock: bal=mb_instance.getBalance()
        logs.append(f"[INFO] getBalance() OK: {bal}")
    except Exception as e:
        logs.append(f"[ERROR] {e}"); return jsonify({"ok":False,"msg":f"Lỗi getBalance: {e}","debug_logs":logs})
    return jsonify({"ok":True,"msg":"Kết nối thành công!","debug_logs":logs})

@app.route("/api/admin/coupons", methods=["GET"])
@require_admin
def admin_get_coupons():
    return jsonify(load_db().get("coupons",[]))

@app.route("/api/admin/coupons", methods=["POST"])
@require_admin
def admin_add_coupon():
    db=load_db(); data=request.json; code=data.get("code","").strip().upper()
    if not code: return jsonify({"ok":False,"msg":"Nhập mã giảm giá"})
    if any(c["code"]==code for c in db.get("coupons",[])): return jsonify({"ok":False,"msg":"Mã đã tồn tại"})
    coupon={"id":str(uuid.uuid4()),"code":code,"type":data.get("type","fixed"),"value":int(data.get("value",0)),
            "max_uses":int(data.get("max_uses",0)),"used_count":0,"min_price":int(data.get("min_price",0)),
            "max_discount":int(data.get("max_discount",0)),"description":data.get("description",""),
            "active":True,"created_at":datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")}
    db.setdefault("coupons",[]).append(coupon); save_db(db); return jsonify({"ok":True,"coupon":coupon})

@app.route("/api/admin/coupons/<cid>", methods=["PUT"])
@require_admin
def admin_update_coupon(cid):
    db=load_db(); data=request.json
    for i,c in enumerate(db.get("coupons",[])):
        if c["id"]==cid:
            db["coupons"][i].update({"code":data.get("code",c["code"]).strip().upper(),"type":data.get("type",c["type"]),
                "value":int(data.get("value",c["value"])),"max_uses":int(data.get("max_uses",c["max_uses"])),
                "min_price":int(data.get("min_price",c.get("min_price",0))),
                "max_discount":int(data.get("max_discount",c.get("max_discount",0))),
                "description":data.get("description",c.get("description","")),"active":bool(data.get("active",c.get("active",True)))})
            save_db(db); return jsonify({"ok":True})
    return jsonify({"ok":False,"msg":"Not found"}),404

@app.route("/api/admin/coupons/<cid>", methods=["DELETE"])
@require_admin
def admin_delete_coupon(cid):
    db=load_db(); db["coupons"]=[c for c in db.get("coupons",[]) if c["id"]!=cid]; save_db(db); return jsonify({"ok":True})

@app.route("/api/admin/coupons/<cid>/toggle", methods=["POST"])
@require_admin
def admin_toggle_coupon(cid):
    db=load_db()
    for c in db.get("coupons",[]):
        if c["id"]==cid: c["active"]=not c.get("active",True); save_db(db); return jsonify({"ok":True,"active":c["active"]})
    return jsonify({"ok":False,"msg":"Not found"}),404

@app.route("/api/admin/reviews", methods=["GET"])
@require_admin
def admin_get_reviews():
    db=load_db()
    return jsonify(sorted(db.get("reviews",[]),key=lambda x:x.get("created_at",""),reverse=True))

@app.route("/api/admin/reviews/<rid>/approve", methods=["POST"])
@require_admin
def admin_approve_review(rid):
    db=load_db()
    for r in db.get("reviews",[]):
        if r["id"]==rid: r["approved"]=True; save_db(db); return jsonify({"ok":True})
    return jsonify({"ok":False,"msg":"Not found"}),404

@app.route("/api/admin/reviews/<rid>", methods=["DELETE"])
@require_admin
def admin_delete_review(rid):
    db=load_db(); db["reviews"]=[r for r in db.get("reviews",[]) if r["id"]!=rid]; save_db(db); return jsonify({"ok":True})

@app.route("/api/admin/notify", methods=["POST"])
@require_admin
def admin_send_notification():
    db=load_db(); data=request.json; title=data.get("title",""); body=data.get("body","")
    target=data.get("target","all"); notif_type=data.get("type","info")
    if not title or not body: return jsonify({"ok":False,"msg":"Cần tiêu đề và nội dung"})
    sent=0
    if target=="all":
        for u in db["users"]: push_notification(db,u["username"],title,body,notif_type); sent+=1
    else:
        if not next((u for u in db["users"] if u["username"]==target),None):
            return jsonify({"ok":False,"msg":"Không tìm thấy user"})
        push_notification(db,target,title,body,notif_type); sent=1
    save_db(db); return jsonify({"ok":True,"sent":sent})

@app.route("/api/admin/referrals")
@require_admin
def admin_get_referrals():
    db=load_db()
    return jsonify(sorted(db.get("referral_rewards",[]),key=lambda x:x.get("created_at",""),reverse=True))

@app.route("/api/admin/settings", methods=["GET"])
@require_admin
def admin_get_settings():
    return jsonify(load_db().get("settings",{}))

@app.route("/api/admin/settings", methods=["POST"])
@require_admin
def admin_save_settings():
    db=load_db(); data=request.json
    db.setdefault("settings",{}).update({"referral_reward":int(data.get("referral_reward",10000)),
        "referral_topup_bonus_pct":int(data.get("referral_topup_bonus_pct",5)),
        "invoice_shop_name":data.get("invoice_shop_name","Xelloxx Shop"),
        "invoice_address":data.get("invoice_address",""),"invoice_phone":data.get("invoice_phone","")})
    save_db(db); return jsonify({"ok":True})

@app.route("/api/admin/categories", methods=["POST"])
@require_admin
def admin_add_category():
    db=load_db(); cat=request.json.get("name")
    if cat and cat not in db["categories"]: db["categories"].append(cat); save_db(db)
    return jsonify({"ok":True,"categories":db["categories"]})

@app.route("/api/admin/categories/<cat>", methods=["DELETE"])
@require_admin
def admin_delete_category(cat):
    db=load_db(); db["categories"]=[c for c in db["categories"] if c!=cat]; save_db(db)
    return jsonify({"ok":True,"categories":db["categories"]})

@app.route("/api/admin/topup-requests/<req_id>/approve", methods=["POST"])
@require_admin
def admin_approve_topup(req_id):
    db=load_db()
    req=next((r for r in db.get("topup_requests",[]) if r["id"]==req_id),None)
    if not req: return jsonify({"ok":False,"msg":"Không tìm thấy"}),404
    if req["status"]!="pending": return jsonify({"ok":False,"msg":f"Trạng thái: {req['status']}"}),400
    amount=int((request.json or {}).get("amount",req.get("amount",0)))
    if amount<=0: return jsonify({"ok":False,"msg":"Số tiền không hợp lệ"}),400
    for user in db["users"]:
        if user["username"]==req["username"]: user["balance"]=user.get("balance",0)+amount; break
    else: return jsonify({"ok":False,"msg":f"Không tìm thấy user"}),404
    req.update({"status":"completed","amount_received":amount,"xu_added":amount,
                "completed_at":datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),"approved_by":"admin"})
    push_notification(db,req["username"],"✅ Nạp tiền thành công",f"Đã nhận {amount:,} xu vào tài khoản.","success")
    save_db(db); return jsonify({"ok":True,"msg":f"Đã nạp {amount:,} xu cho {req['username']}"})

@app.route("/api/admin/topup-requests/<req_id>/reject", methods=["POST"])
@require_admin
def admin_reject_topup(req_id):
    db=load_db()
    req=next((r for r in db.get("topup_requests",[]) if r["id"]==req_id),None)
    if not req: return jsonify({"ok":False,"msg":"Không tìm thấy"}),404
    if req["status"]!="pending": return jsonify({"ok":False,"msg":f"Trạng thái: {req['status']}"}),400
    req.update({"status":"rejected","rejected_at":datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),"rejected_by":"admin"})
    push_notification(db,req["username"],"❌ Yêu cầu nạp tiền bị từ chối","Yêu cầu đã bị từ chối. Liên hệ admin.","error")
    save_db(db); return jsonify({"ok":True,"msg":"Đã hủy yêu cầu"})

@app.route("/api/admin/users/<uid>/balance", methods=["POST"])
@require_admin
def admin_adjust_balance(uid):
    db=load_db(); data=request.json; amount=int(data.get("amount",0)); note=data.get("note","Admin điều chỉnh")
    user=next((u for u in db["users"] if u["id"]==uid),None)
    if not user: return jsonify({"ok":False,"msg":"Không tìm thấy"}),404
    old=user.get("balance",0); user["balance"]=max(0,old+amount)
    if amount!=0:
        push_notification(db,user["username"],"💳 Số dư điều chỉnh",
            f"Số dư: {old:,} → {user['balance']:,} xu. Lý do: {note}","info" if amount>0 else "warning")
    save_db(db); return jsonify({"ok":True,"msg":f"{user['username']}: {old:,} -> {user['balance']:,} xu"})

if __name__=="__main__":
    port=int(os.environ.get("PORT",8002))
    app.run(host="0.0.0.0",port=port,debug=False)
