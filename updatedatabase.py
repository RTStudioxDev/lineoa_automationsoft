from pymongo import MongoClient
from werkzeug.security import generate_password_hash

client = MongoClient("mongodb+srv://admin:060843Za@telegrambot.f91jjzo.mongodb.net/")
db = client["Lineautomation"]

# ข้อมูลที่ต้องการอัปเดต/เพิ่ม
username = "admin"
plain_password = "1234"
role = "admin"
expire_date = "2025-07-31"  # (เปลี่ยนวันได้)
last_ip = None  # ใส่ None หรือ IP สุดท้ายที่รู้, หากต้องการ

# อัปเดต/เพิ่ม user
db.users.update_one(
    {"username": username},
    {
        "$set": {
            "password": generate_password_hash(plain_password),
            "role": role,
            "expire_date": expire_date,
            "last_ip": last_ip,
            "credit": 0,
            "oa_list": [],
            "flex_templates": []
        }
    },
    upsert=True
)

print(f"อัปเดต user '{username}' สำเร็จ")
