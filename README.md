# 🚀 Xelloxx Shop — Hướng dẫn Deploy lên Railway

## 📋 Yêu cầu
- Tài khoản [Railway.com](https://railway.com) (free tier đủ dùng)
- Git đã cài đặt
- Python 3.11+

---

## ⚡ Deploy nhanh (5 phút)

### Bước 1 — Push code lên GitHub
```bash
git init
git add .
git commit -m "init: xelloxx shop"
git remote add origin https://github.com/USERNAME/REPO.git
git push -u origin main
```

### Bước 2 — Tạo project trên Railway
1. Vào [railway.com](https://railway.com) → **New Project**
2. Chọn **Deploy from GitHub repo**
3. Chọn repo vừa push → Railway sẽ tự build

### Bước 3 — Cấu hình Environment Variables
Vào tab **Variables** trong Railway và thêm các biến sau:

| Biến | Giá trị | Bắt buộc |
|------|---------|----------|
| `SECRET_KEY` | random string dài 64 ký tự | ✅ |
| `DOWNLOAD_SECRET` | random string dài 64 ký tự | ✅ |
| `DATA_DIR` | `/data` | ✅ (khi có Volume) |
| `MB_USERNAME` | SĐT đăng nhập MB Bank | ⚡ (nếu dùng MB) |
| `MB_PASSWORD` | Mật khẩu MB Bank | ⚡ (nếu dùng MB) |
| `MB_ACCOUNT` | Số tài khoản MB Bank | ⚡ (nếu dùng MB) |
| `PORT` | Railway tự set, không cần đặt | ❌ |

> **Tạo SECRET_KEY ngẫu nhiên:**
> ```python
> python3 -c "import secrets; print(secrets.token_hex(32))"
> ```

### Bước 4 — Thêm Persistent Volume (QUAN TRỌNG!)
Railway filesystem là **ephemeral** (dữ liệu mất khi redeploy).
Để giữ database và file upload:

1. Trong project Railway → **Add Service** → **Volume**
2. Mount path: `/data`
3. Thêm env var: `DATA_DIR=/data`

> ⚠️ Nếu không có Volume, mỗi lần deploy sẽ mất toàn bộ dữ liệu!

### Bước 5 — Sinh Domain
Railway tab **Settings** → **Domains** → **Generate Domain**

---

## 🏗️ Cấu trúc file Railway

```
shop/
├── app.py                  # Main Flask app
├── railway.toml            # Railway build & deploy config
├── nixpacks.toml           # Build system config
├── Procfile                # Process command
├── runtime.txt             # Python version
├── requirements.txt        # Python dependencies
├── .gitignore              # Git ignore rules
├── templates/              # HTML templates
│   ├── index.html
│   ├── admin.html
│   ├── cart.html
│   ├── referral.html
│   ├── notifications.html
│   ├── orders.html
│   ├── checkout.html
│   ├── topup.html
│   ├── login.html
│   └── register.html
└── uploads/                # Tự tạo khi chạy (cần Volume)
    ├── images/
    └── files/
```

---

## 🔧 Cấu hình nâng cao

### Đổi mật khẩu admin mặc định
Mặc định: `admin / admin123`
Vào `/admin/settings` sau khi deploy để đổi mật khẩu.

### Cấu hình MB Bank
1. Vào `/admin/settings` → tab **MB Bank**
2. Nhập SĐT, mật khẩu, số TK
3. Nhấn **Kích hoạt** và **Test kết nối**

Hoặc set env vars: `MB_USERNAME`, `MB_PASSWORD`, `MB_ACCOUNT`

### Tăng workers (traffic cao)
Sửa trong `railway.toml`:
```toml
startCommand = "gunicorn app:app --bind 0.0.0.0:$PORT --workers 4 --threads 4 --timeout 120"
```

---

## 🛡️ Checklist bảo mật trước khi go-live

- [ ] Đổi `SECRET_KEY` thành random string (không để mặc định)
- [ ] Đổi `DOWNLOAD_SECRET` thành random string
- [ ] Đổi mật khẩu admin mặc định (`admin123`)
- [ ] Thêm Volume `/data` để bảo toàn dữ liệu
- [ ] Set `DATA_DIR=/data` trong Variables
- [ ] Nếu dùng MB Bank: dùng tài khoản phụ riêng biệt

---

## 🐛 Troubleshooting

### Build thất bại
```
❌ "No module named mbbank"
```
→ Kiểm tra `requirements.txt` có `mbbank-lib` chưa, Railway sẽ tự cài.

### App crash sau deploy
```
❌ Database error
```
→ Volume chưa được mount. Kiểm tra `DATA_DIR` env var.

### MB Bank không kết nối
```
❌ getBalance() thất bại
```
→ Vào Admin → Settings → MB Bank → Test kết nối để xem log chi tiết.

---

## 📱 Tính năng đã có

| Tính năng | Mô tả |
|-----------|-------|
| 🛒 Giỏ hàng | Thêm, xóa, checkout nhiều sản phẩm |
| 🎁 Referral | Mã giới thiệu, thưởng xu đăng ký + % nạp tiền |
| 🎟️ Mã giảm giá | Fixed / percent, giới hạn lượt, tối thiểu đơn |
| ⭐ Đánh giá | Chỉ sau khi mua, rating 1-5 sao |
| 📢 Thông báo | Push realtime, broadcast admin |
| 🧾 Hóa đơn | Xuất hóa đơn từng đơn hàng |
| 🔒 Download an toàn | Token HMAC, hết hạn 1h, chống share |
| ⏱️ Rate limit | Chống spam login, register, checkout |
| 🏦 MB Bank | Auto polling giao dịch mỗi 15 giây |

---

## 💡 Tips Railway

- **Free tier:** 500 giờ/tháng, đủ cho shop nhỏ
- **Auto-deploy:** Mỗi lần `git push` Railway tự deploy lại
- **Logs:** Railway dashboard → tab **Logs** để xem real-time
- **Rollback:** Nhấn **Rollback** để quay về deploy trước nếu có lỗi
