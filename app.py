import time
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory, Blueprint, jsonify, Response
from line_api import LineAPI
from functools import wraps
from pymongo import MongoClient, ASCENDING
import pandas as pd
import os
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import ipaddress
from bson.objectid import ObjectId
import threading
import time
import uuid
import csv
import io
import requests
import json as pyjson
from collections import defaultdict

MONGO_URI = os.getenv("MONGO_URI", "mongodb://admin:060843Za@147.50.240.76:27017/")
DB_NAME = os.getenv("DB_NAME", "Lineautomation")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "lineoa-automationsoft-key")
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.permanent_session_lifetime = timedelta(hours=2)  # 2 ชั่วโมง

client = MongoClient(MONGO_URI)
mongo_db = client[DB_NAME]

send_progress = {
    "current": 0, "total": 0, "fail": 0, "done": False
}
send_cancelled = False

# --- Helper สำหรับ ดึง user_id ---
def add_oa_to_user(username, oa_dict):
    mongo_db.users.update_one(
        {"username": username},
        {"$push": {"oa_list": oa_dict}}
    )

def delete_oa_from_user(username, oa_id):
    mongo_db.users.update_one(
        {"username": username},
        {"$pull": {"oa_list": {"id": oa_id}}}
    )

def get_user_oa_list(username):
    user = mongo_db.users.find_one({"username": username})
    return user.get("oa_list", []) if user else []

def get_oa_by_id(username, oa_id):
    user = mongo_db.users.find_one({"username": username})
    if user and "oa_list" in user:
        for oa in user["oa_list"]:
            if str(oa.get("id")) == str(oa_id):
                return oa
    return None

def get_oa_id_from_mid(mid, target_oa_id=None):
    # 1. ค้นหา mid ปกติ
    result = mongo_db.users.find_one(
        {"oa_list.mid": mid},
        {"oa_list.$": 1}
    )
    if result and "oa_list" in result and result["oa_list"]:
        return result["oa_list"][0]["id"]

    # 2. หา OA ที่ยังไม่มี mid (auto mapping ทุกกรณี)
    users = list(mongo_db.users.find({}))
    candidates = []
    for user in users:
        for idx, oa in enumerate(user.get("oa_list", [])):
            if not oa.get("mid"):
                candidates.append((user, idx, oa))

    # ถ้ามี target_oa_id ให้เลือกอันที่ oa_id ตรงกัน
    if target_oa_id:
        for user, idx, oa in candidates:
            if str(oa.get("id")) == str(target_oa_id):
                mongo_db.users.update_one(
                    {"_id": user["_id"]},
                    {"$set": {f"oa_list.{idx}.mid": mid}}
                )
                print(f"[AUTO] Mapping mid {mid} → oa_id {oa['id']} (from target_oa_id)")
                return oa["id"]

    # ถ้าไม่มี target_oa_id หรือไม่ตรง, เลือกอันแรกที่เจอ
    if candidates:
        user, idx, oa = candidates[0]
        mongo_db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {f"oa_list.{idx}.mid": mid}}
        )
        print(f"[AUTO] Force-mapping mid {mid} → oa_id {oa['id']}")
        return oa["id"]
    else:
        print("[ERROR] ไม่พบ OA ที่ยังไม่มี mid")
    return None

# --- Flex Message ---
def get_user_templates(username):
    user = get_user_from_db(username)
    return user.get("flex_templates", []) if user else []

def add_template(username, name, flex_content, alt_text, created_at=None):
    if created_at is None:
        from datetime import datetime
        created_at = datetime.now()
    user = mongo_db.users.find_one({"username": username})
    if not user:
        return False
    # ตรวจสอบว่าชื่อ template ซ้ำไหม
    for t in user.get("flex_templates", []):
        if t["name"] == name:
            return False  # ชื่อซ้ำ
    mongo_db.users.update_one(
        {"username": username},
        {"$push": {"flex_templates": {
            "name": name,
            "alt_text": alt_text,
            "json": flex_content,
            "created_at": created_at
        }}}
    )
    return True

def update_template(username, name, new_flex_content):
    mongo_db.users.update_one(
        {"username": username, "flex_templates.name": name},
        {"$set": {
            "flex_templates.$.json": new_flex_content,
            "flex_templates.$.modified_at": datetime.now()
        }}
    )

def delete_template(username, name):
    mongo_db.users.update_one(
        {"username": username},
        {"$pull": {"flex_templates": {"name": name}}}
    )

def get_template_by_name(username, name):
    user = mongo_db.users.find_one({"username": username})
    for t in user.get("flex_templates", []):
        if t["name"] == name:
            return t
    return None

# --- ระบบ USER_WEB ---
def get_user_from_db(username):
    return mongo_db.users.find_one({"username": username})

def save_user_to_db(username, password):
    password_hash = generate_password_hash(password)
    mongo_db.users.insert_one({"username": username, "password": password_hash, "oa_list": [], "flex_templates": []})

def get_user_role(username):
    user = mongo_db.users.find_one({'username': username})
    return user['role'] if user and 'role' in user else 'user'

# --- IP ---
def get_ipv4(ip):
    try:
        return str(ipaddress.IPv4Address(ip))
    except Exception:
        return ip

# --- ระบบวันหมดอายุ ---
def get_days_left(user):
    if not user or not user.get("expire_date"):
        return None
    expire = datetime.strptime(user["expire_date"], "%Y-%m-%d")
    today = datetime.now()
    return (expire - today).days

# --- อนุญาติไฟล์ ---
def allowed_file(filename):
    allowed_extensions = {'png', 'jpg', 'jpeg', 'gif'}
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in allowed_extensions

# --- ระบบเก็บข้อมูล USER_ID ---
def save_userid(user_id, oa_id):
    users = mongo_db.users
    # เพิ่มเฉพาะถ้ายังไม่มี user_id กับ oa_id นี้
    if not users.find_one({'user_id': user_id, 'oa_id': oa_id}):
        users.insert_one({'user_id': user_id, 'oa_id': oa_id})

def get_total_friends(oa_id):
    user = mongo_db.users.find_one({"oa_list.id": oa_id})
    if user:
        for oa in user["oa_list"]:
            if oa["id"] == oa_id:
                return len(oa.get("user_ids", []))
    return 0

def get_followers(oa_id):
    oa = get_current_oa_from_db(oa_id)
    if oa:
        return oa.get("user_ids", [])
    return []

def get_current_oa_from_db(oa_id):
    user = mongo_db.users.find_one({"oa_list.id": oa_id})
    if user:
        for oa in user["oa_list"]:
            if oa["id"] == oa_id:
                return oa
    return None

def get_api_oa_from_db(user_id):
    user = mongo_db.users.find_one({
        "oa_list.user_ids": user_id
    })
    if user and user.get("oa_list"):
        for oa in user["oa_list"]:
            if "user_ids" in oa and user_id in oa["user_ids"]:
                return {
                    "oa_id": oa.get("id"),
                    "username": user.get("username")
                }
    return None

def add_user_id_to_oa(user, oa_id, user_id):
    """
    user = ข้อมูล user document ทั้ง object
    oa_id = OA id ที่ต้องการเพิ่ม user_id (string)
    user_id = userId ที่จะเพิ่มเข้า array
    """
    users = mongo_db.users

    # Find the OA object ใน oa_list
    for idx, oa in enumerate(user.get("oa_list", [])):
        if oa.get("id") == oa_id:
            # ตรวจสอบว่ามี user_ids หรือยัง ถ้าไม่มีสร้างใหม่
            if "user_ids" not in oa:
                oa["user_ids"] = []
            # เพิ่ม user_id ถ้ายังไม่มี
            if user_id not in oa["user_ids"]:
                oa["user_ids"].append(user_id)
                # อัปเดต document กลับลง database
                users.update_one(
                    {"_id": user["_id"], f"oa_list.id": oa_id},
                    {"$set": {f"oa_list.{idx}.user_ids": oa["user_ids"]}}
                )
            break

def save_userid_to_oa(oa_id, user_id):
    oa_id = str(oa_id)  # สำคัญมาก!
    user = mongo_db.users.find_one({"oa_list.id": oa_id})
    if user:
        updated = False
        for oa in user["oa_list"]:
            if str(oa["id"]) == oa_id:
                if "user_ids" not in oa:
                    oa["user_ids"] = []
                if user_id not in oa["user_ids"]:
                    oa["user_ids"].append(user_id)
                    updated = True
        if updated:
            mongo_db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"oa_list": user["oa_list"]}}
            )

def clear_user_ids_of_oa(oa_id):
    # หา user document ที่มี oa_id นี้
    user = mongo_db.users.find_one({"oa_list.id": oa_id})
    if user:
        found = False
        for oa in user["oa_list"]:
            if oa["id"] == oa_id:
                removed_count = len(oa.get("user_ids", []))
                oa["user_ids"] = []
                found = True
        if found:
            mongo_db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"oa_list": user["oa_list"]}}
            )
            return removed_count
    return None

def map_oa_mid(oa_id, mid):
    user = mongo_db.users.find_one({"oa_list.id": oa_id})
    if user:
        for idx, oa in enumerate(user.get("oa_list", [])):
            if oa.get("id") == oa_id:
                mongo_db.users.update_one(
                    {"_id": user["_id"]},
                    {"$set": {f"oa_list.{idx}.mid": mid}}
                )
                print(f"OA {oa_id} mapping mid {mid} สำเร็จ")
                return True
    return False

# --- ระบบเก็บกัน Spam ---
        # LOG #
def log_message_send(
    message_id,
    user_id,
    oa_id,
    status,
    msg_type,
    detail,
    error_msg=None,
    sent_at=None,
    scheduled_time=None
):
    log = {
        "message_id": message_id,
        "user_id": user_id,
        "oa_id": oa_id,
        "sent_at": sent_at or datetime.now(),
        "status": status,       # pending / success / fail / error
        "type": msg_type,       # เช่น "text", "flex", "image"
        "detail": detail
    }
    if error_msg:
        log["error_msg"] = error_msg
    if scheduled_time:
        log["detail"]["scheduled_time"] = scheduled_time

    mongo_db.users.update_one(
        {"oa_list.id": oa_id},
        {"$push": {"oa_list.$.send_logs": log}}
    )

        # กันส่งข้อความซ้ำ #
def already_sent_recently(user_id, oa_id, msg_type, detail, hours=6):
    time_threshold = datetime.now() - timedelta(hours=hours)
    # หา log ใน oa_list ของ oa_id ที่ user_id ตรง, type ตรง, detail ตรง, เวลายังไม่เกิน
    user = mongo_db.users.find_one({"oa_list.id": oa_id}, {"oa_list.$": 1})
    if user and "oa_list" in user and user["oa_list"]:
        logs = user["oa_list"][0].get("send_logs", [])
        for log in logs:
            if (
                log.get("user_id") == user_id
                and log.get("type") == msg_type
                and log.get("detail") == detail    # ต้องเป๊ะ (dict เทียบแบบ deep)
                and log.get("sent_at") >= time_threshold
            ):
                return True
    return False

def cleanup_send_logs():
    # เวลาปัจจุบัน
    now = datetime.now()
    expire_time = now - timedelta(days=7)
    
    # ดึง users ทั้งหมด
    users = mongo_db.users.find()
    for user in users:
        update_needed = False
        new_oa_list = []
        for oa in user.get("oa_list", []):
            logs = oa.get("send_logs", [])
            # คัด log ที่ sent_at ยังไม่เก่าเกิน 7 วัน
            new_logs = [log for log in logs if log.get("sent_at") and log["sent_at"] > expire_time]
            if len(new_logs) != len(logs):
                update_needed = True
            oa["send_logs"] = new_logs
            new_oa_list.append(oa)
        # ถ้ามีการเปลี่ยนแปลง log ให้ update document ในฐานข้อมูล
        if update_needed:
            mongo_db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"oa_list": new_oa_list}}
            )

cleanup_send_logs()

# --- ระบบตั้งเวลาส่ง ---
def scheduled_message_worker():
    BATCH_SIZE = 500
    DELAY_SEC = 3
    while True:
        now = datetime.now()
        users = list(mongo_db.users.find({}))
        for user in users:
            for oa in user.get("oa_list", []):
                oa_id = oa["id"]
                oa_token = oa.get("access_token")
                send_logs = oa.get("send_logs", [])
                # 1. หา log ที่ pending และถึงเวลาแล้ว
                for log in send_logs:
                    status = log.get("status", "")
                    detail = log.get("detail", {})
                    scheduled_time = detail.get("scheduled_time")
                    if status != "pending" or not scheduled_time:
                        continue
                    if isinstance(scheduled_time, str):
                        try:
                            scheduled_time = datetime.fromisoformat(scheduled_time)
                        except Exception:
                            continue
                    if scheduled_time > now:
                        continue  # ยังไม่ถึงเวลา

                    # 2. "จอง" log ไว้ก่อน เปลี่ยน status = "sending"
                    update_res = mongo_db.users.update_one(
                        {
                            "_id": user["_id"],
                            "oa_list.id": oa_id,
                            "oa_list.send_logs.message_id": log.get("message_id"),
                            "oa_list.send_logs.status": "pending"
                        },
                        {
                            "$set": {
                                "oa_list.$[oa].send_logs.$[lg].status": "sending"
                            }
                        },
                        array_filters=[
                            {"oa.id": oa_id},
                            {"lg.message_id": log.get("message_id")}
                        ]
                    )
                    if update_res.modified_count == 0:
                        continue  # อาจถูก worker อื่นจองไปแล้ว

                    # 3. ทำการส่ง (safe)
                    error_msg = None
                    send_status = "success"
                    try:
                        api = LineAPI(oa_token)
                        user_id = log.get("user_id")
                        msg_type = log.get("type") or log.get("msg_type")
                        # FLEX
                        if msg_type == "flex":
                            alt_text = detail.get("altText", "Flex Message")
                            flex_json = detail.get("json")
                            if user_id == "broadcast":
                                user_ids = get_followers(oa_id)
                                for i in range(0, len(user_ids), BATCH_SIZE):
                                    batch = user_ids[i:i+BATCH_SIZE]
                                    try:
                                        success = api.send_multicast_flex(batch, flex_json, alt_text)
                                        if not success:
                                            send_status = "fail"
                                            error_msg = "API ไม่ตอบ success"
                                    except Exception as ex:
                                        send_status = "fail"
                                        error_msg = str(ex)
                                    time.sleep(DELAY_SEC)
                            else:
                                try:
                                    success = api.send_flex(user_id, flex_json, alt_text)
                                    if not success:
                                        send_status = "fail"
                                        error_msg = "API ไม่ตอบ success"
                                except Exception as ex:
                                    send_status = "fail"
                                    error_msg = str(ex)
                        # MULTI
                        elif msg_type == "multi":
                            messages = detail.get("messages", [])
                            if user_id == "broadcast":
                                user_ids = get_followers(oa_id)
                                for i in range(0, len(user_ids), BATCH_SIZE):
                                    batch = user_ids[i:i+BATCH_SIZE]
                                    for msg in messages:
                                        try:
                                            if msg["type"] == "text":
                                                success = api.send_multicast(batch, msg["text"], None)
                                            elif msg["type"] == "image":
                                                success = api.send_multicast(batch, "", msg["image_url"])
                                            if not success:
                                                send_status = "fail"
                                                error_msg = "API ไม่ตอบ success"
                                        except Exception as ex:
                                            send_status = "fail"
                                            error_msg = str(ex)
                                        time.sleep(DELAY_SEC)
                            else:
                                for msg in messages:
                                    try:
                                        if msg["type"] == "text":
                                            success = api.send_message(user_id, msg["text"], None)
                                        elif msg["type"] == "image":
                                            success = api.send_message(user_id, "", msg["image_url"])
                                        if not success:
                                            send_status = "fail"
                                            error_msg = "API ไม่ตอบ success"
                                    except Exception as ex:
                                        send_status = "fail"
                                        error_msg = str(ex)
                                    time.sleep(DELAY_SEC)
                        # SINGLE MESSAGE
                        else:
                            text = detail.get("text", "")
                            image_url = detail.get("image_url")
                            if user_id == "broadcast":
                                user_ids = get_followers(oa_id)
                                for i in range(0, len(user_ids), BATCH_SIZE):
                                    batch = user_ids[i:i+BATCH_SIZE]
                                    try:
                                        success = api.send_multicast(batch, text, image_url)
                                        if not success:
                                            send_status = "fail"
                                            error_msg = "API ไม่ตอบ success"
                                    except Exception as ex:
                                        send_status = "fail"
                                        error_msg = str(ex)
                                    time.sleep(DELAY_SEC)
                            else:
                                try:
                                    success = api.send_message(user_id, text, image_url)
                                    if not success:
                                        send_status = "fail"
                                        error_msg = "API ไม่ตอบ success"
                                except Exception as ex:
                                    send_status = "fail"
                                    error_msg = str(ex)
                    except Exception as e:
                        send_status = "fail"
                        error_msg = str(e)

                    # 4. อัปเดตผลลัพธ์
                    mongo_db.users.update_one(
                        {
                            "_id": user["_id"],
                            "oa_list.id": oa_id,
                            "oa_list.send_logs.message_id": log.get("message_id")
                        },
                        {
                            "$set": {
                                "oa_list.$[oa].send_logs.$[lg].status": send_status,
                                "oa_list.$[oa].send_logs.$[lg].sent_at": datetime.now(),
                                "oa_list.$[oa].send_logs.$[lg].error_msg": error_msg
                            }
                        },
                        array_filters=[
                            {"oa.id": oa_id},
                            {"lg.message_id": log.get("message_id")}
                        ]
                    )
        time.sleep(30)

# เรียกใช้งาน worker 1 ตัว (ตอนรัน Flask)
threading.Thread(target=scheduled_message_worker, daemon=True).start()

# --- ระบบเครดิต---
def add_credit(username, amount):
    mongo_db.users.update_one(
        {"username": username},
        {"$inc": {"credit": amount}}
    )

def set_credit(username, value):
    mongo_db.users.update_one({"username": username}, {"$set": {"credit": value}})

def get_credit(username):
    user = mongo_db.users.find_one({"username": username})
    return user.get("credit", 0) if user else 0

# --- ระบบแจ้งเตือนเติมเงิน ---
def upload_to_imgbb(file, api_key="38fff7fc24bb2d14a0729f5f50e6f17f"):
    url = "https://api.imgbb.com/1/upload"
    payload = {"key": api_key}
    files = {"image": (file.filename, file.stream, file.mimetype)}
    response = requests.post(url, data=payload, files=files)
    if response.status_code == 200:
        data = response.json()
        return data["data"]["url"]
    else:
        print("[IMGBB ERROR]", response.text)
        return None

TELEGRAM_BOT_TOKEN = "8007609460:AAHryP3dcvUmEVbaXEabARmtkr7d8YZhiKg"
TELEGRAM_ADMIN_CHAT_ID = "7497889170"

def notify_telegram_admin_topup(slip):
    image_url = slip['image']
    caption = (
        f"💰 แจ้งเตือนเติมเงิน\n"
        f"👤 User: {slip['username']}\n"
        f"💵 จำนวน: {slip['amount']} บาท"
    )
    approve_data = f"approve_topup:{str(slip['_id'])}"
    reject_data = f"reject_topup:{str(slip['_id'])}"
    inline_keyboard = [
        [
            {"text": "✅ อนุมัติ", "callback_data": approve_data},
            {"text": "❌ ปฏิเสธ", "callback_data": reject_data}
        ]
    ]

    payload = {
        "chat_id": TELEGRAM_ADMIN_CHAT_ID,
        "photo": image_url,
        "caption": caption,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": inline_keyboard
        }
    }

    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
        json=payload  # !! ใช้ json ไม่ใช่ data
    )
    print(f"Telegram notify response: {resp.status_code} {resp.text}")
    return resp

def set_status_with_disabled_button(chat_id, message_id, status, caption):
    if status == "approved":
        status_text = "\n\n✅ สถานะ: อนุมัติแล้ว"
        new_keyboard = [[{"text": "✅ อนุมัติแล้ว", "callback_data": "noop"}]]
    elif status == "rejected":
        status_text = "\n\n❌ สถานะ: ปฏิเสธแล้ว"
        new_keyboard = [[{"text": "❌ ปฏิเสธแล้ว", "callback_data": "noop"}]]
    else:
        status_text = ""
        new_keyboard = []

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageCaption"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": (caption or "") + status_text,
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": new_keyboard}
    })
    print('editMessageCaption:', resp.status_code, resp.text)

@app.route('/webhook/telegram', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    if "callback_query" in update:
        callback = update["callback_query"]
        data = callback["data"]
        chat_id = callback["message"]["chat"]["id"]
        message_id = callback["message"]["message_id"]

        if data.startswith("approve_topup:"):
            slip_id = data.split(":")[1]
            slip = mongo_db.topup_slips.find_one({"_id": ObjectId(slip_id)})
            if slip and slip.get("status") == "pending":
                mongo_db.users.update_one(
                    {"username": slip["username"]},
                    {"$inc": {"credit": int(slip["amount"])}}
                )
                mongo_db.topup_slips.update_one(
                    {"_id": ObjectId(slip_id)},
                    {"$set": {"status": "approved", "approved_at": datetime.now()}}
                )
                answer = "✅ อนุมัติสลิปเรียบร้อย"
                set_status_with_disabled_button(
                    chat_id,
                    message_id,
                    "approved",
                    callback["message"].get("caption", "")
                )
            else:
                answer = "ไม่พบสลิปนี้หรือถูกอนุมัติ/ปฏิเสธไปแล้ว"

        elif data.startswith("reject_topup:"):
            slip_id = data.split(":")[1]
            mongo_db.topup_slips.update_one(
                {"_id": ObjectId(slip_id)},
                {"$set": {"status": "rejected", "rejected_at": datetime.now()}}
            )
            answer = "❌ ปฏิเสธสลิปนี้แล้ว"
            set_status_with_disabled_button(
                chat_id,
                message_id,
                "rejected",
                callback["message"].get("caption", "")
            )

        elif data == "noop":
            # กดปุ่มที่ไม่มี action (เช่น "อนุมัติแล้ว" หรือ "ปฏิเสธแล้ว")
            answer = "สถานะนี้ไม่สามารถกดซ้ำได้"

        else:
            answer = "เกิดข้อผิดพลาด ไม่ทราบคำสั่ง"

        # ตอบกลับ callback (ขึ้น popup ในแชท)
        reply_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        requests.post(reply_url, json={
            "callback_query_id": callback["id"],
            "text": answer, "show_alert": True
        })

    return jsonify({"ok": True})

# --- REQUIRE ---
def require_web_login(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "user_login" not in session:
            flash("กรุณาเข้าสู่ระบบก่อน")
            return redirect(url_for("login_user"))
        return func(*args, **kwargs)
    return wrapper

def require_oa(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "current_oa" not in session:
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapper

def require_admin(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        username = session.get("user_login")
        user = get_user_from_db(username)
        if not user or user.get("role") != "admin":
            flash("คุณไม่มีสิทธิ์เข้าถึงหน้านี้")
            return redirect(url_for("dashboard"))
        return func(*args, **kwargs)
    return wrapper

# --- LOGIN USER ---
@app.route("/login", methods=["GET", "POST"])
def login_user():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = get_user_from_db(username)
        if user and check_password_hash(user.get("password", ""), password):
            # เช็ควันหมดอายุ
            expire_str = user.get("expire_date")
            if expire_str:
                try:
                    expire_date = datetime.strptime(expire_str, "%Y-%m-%d").date()
                    if datetime.now().date() > expire_date:
                        flash("บัญชีนี้หมดอายุแล้ว กรุณาติดต่อผู้ดูแล")
                        return render_template("loginweb.html")
                except Exception as e:
                    flash("วันหมดอายุไม่ถูกต้อง ติดต่อแอดมิน")
                    return render_template("loginweb.html")
            # ถ้าไม่หมดอายุ
            session["user_login"] = username
            session.permanent = True
            session.pop("current_oa", None)

            ip = request.headers.get("X-Forwarded-For", request.remote_addr)
            ip = ip.split(',')[0].strip()
            ip = get_ipv4(ip)
            mongo_db.users.update_one(
                {"username": username},
                {"$set": {"last_ip": ip}}
            )
            flash("เข้าสู่ระบบสำเร็จ")
            return redirect(url_for("switch_oa"))
        else:
            flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
    return render_template("loginweb.html")

@app.route("/logout")
def logout_user():
    session.pop("user_login", None)
    # เพิ่มบรรทัดนี้เพื่อลบ OA session เดิมที่ค้างไว้
    session.pop("current_oa", None)
    flash("ออกจากระบบแล้ว")
    return redirect(url_for("login_user"))

@app.context_processor
def inject_get_user_role():
    def get_user_role(username):
        user = mongo_db.users.find_one({'username': username})
        return user.get('role', 'user') if user else 'user'
    return dict(get_user_role=get_user_role)

@app.route('/change_password', methods=['GET', 'POST'])
def change_password():
    if "user_login" not in session:
        flash("กรุณาเข้าสู่ระบบก่อน")
        return redirect(url_for("login_user"))
    user = mongo_db.users.find_one({"username": session["user_login"]})
    if request.method == "POST":
        old_password = request.form.get("old_password")
        new_password = request.form.get("new_password")
        # เช็ค password เดิมก่อน
        if not check_password_hash(user["password"], old_password):
            flash("รหัสผ่านเดิมไม่ถูกต้อง")
            return redirect(url_for("change_password"))
        mongo_db.users.update_one(
            {"username": session["user_login"]},
            {"$set": {"password": generate_password_hash(new_password)}}
        )
        flash("เปลี่ยนรหัสผ่านสำเร็จ")
        return redirect(url_for("dashboard"))
    return render_template("change_password.html")

# --- PROFILE ---
@app.context_processor
def inject_user_profile():
    def get_user_profile(username):
        user = mongo_db.users.find_one({"username": username})
        if not user:
            return None
        if "expire_date" in user:
            user["expire_date"] = str(user["expire_date"])
        return user
    from datetime import datetime
    return dict(get_user_profile=get_user_profile, now=datetime.now)

@app.context_processor
def inject_today():
    from datetime import datetime
    return {"today": datetime.now().strftime('%Y-%m-%d')}

@app.context_processor
def inject_days_left():
    user = None
    days_left = None
    if "user_login" in session:
        user = get_user_from_db(session["user_login"])
        days_left = get_days_left(user)
    return dict(days_left=days_left)

@app.context_processor
def inject_credit():
    credit = None
    if "user_login" in session:
        user = mongo_db.users.find_one({"username": session["user_login"]})
        if user:
            credit = user.get("credit", 0)
    return dict(user_credit=credit)

@app.context_processor
def inject_oa():
    oa = None
    if "current_oa" in session:
        oa = session["current_oa"]
    return {"oa": oa}

# --- RENEW ต่ออายุ ---
@app.route("/renew", methods=["GET", "POST"])
def renew():
    if "user_login" not in session:
        flash("กรุณาเข้าสู่ระบบ")
        return redirect(url_for("login_user"))
    username = session["user_login"]
    user = mongo_db.users.find_one({"username": username})
    credit = user.get("credit", 0)
    expire_date = user.get("expire_date", datetime.now().strftime("%Y-%m-%d"))
    today = datetime.now().date()
    expire = datetime.strptime(expire_date, "%Y-%m-%d").date()
    left_days = (expire - today).days if expire > today else 0

    # แพคเกจ/ราคา
    package_info = {
        "30": {"days": 30, "price": 1000},
        "365": {"days": 365, "price": 10000, "price_normal": 12000}
    }

    if request.method == "POST":
        pkg = request.form.get("package")
        if pkg not in package_info:
            flash("กรุณาเลือกแพคเกจต่ออายุ")
        else:
            price = package_info[pkg]["price"]
            renew_days = package_info[pkg]["days"]
            if credit < price:
                flash("เครดิตไม่พอ กรุณาเติมเครดิต")
            else:
                # ต่ออายุ
                new_expire = max(expire, today) + timedelta(days=renew_days)
                mongo_db.users.update_one(
                    {"username": username},
                    {"$set": {"expire_date": new_expire.strftime("%Y-%m-%d")},
                     "$inc": {"credit": -price}}
                )
                flash(f"ต่ออายุ {renew_days} วันสำเร็จ! (เครดิต -{price})")
                return redirect(url_for("dashboard"))
    
    return render_template(
        "renew.html",
        credit=credit,
        expire_date=expire_date,
        left_days=left_days
    )

# --- ADMIN PANEL ---
@app.route("/admin", methods=["GET", "POST"])
@require_admin
def admin_panel():
    users = list(mongo_db.users.find({}))
    return render_template("admin_panel.html", users=users)

@app.route("/admin/add_user", methods=["GET", "POST"])
@require_admin
def admin_add_user():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        role = request.form["role"]
        expire_days = int(request.form["expire_days"])
        expire_date = (datetime.now() + timedelta(days=expire_days)).strftime("%Y-%m-%d")
        if get_user_from_db(username):
            flash("มี username นี้แล้ว")
            return redirect(url_for("admin_add_user"))
        password_hash = generate_password_hash(password)
        mongo_db.users.insert_one({
            "username": username,
            "password": password_hash,
            "role": role,
            "expire_date": expire_date
        })
        flash("สร้างยูสเซอร์ใหม่สำเร็จ")
        return redirect(url_for("admin_panel"))
    return render_template("admin_add_user.html")

@app.route("/admin/edit_user/<username>", methods=["GET", "POST"])
@require_admin
def admin_edit_user(username):
    user = mongo_db.users.find_one({"username": username})
    if not user:
        flash("ไม่พบผู้ใช้")
        return redirect(url_for("admin_panel"))
    if request.method == "POST":
        new_password = request.form.get("password", "")
        role = request.form.get("role", user.get("role", "user"))
        expire_days = request.form.get("expire_days")
        updates = {}

        # กำหนด expire_date ใหม่ ถ้ามีการกรอก
        if expire_days:
            try:
                expire_date = (datetime.now() + timedelta(days=int(expire_days))).strftime("%Y-%m-%d")
                updates["expire_date"] = expire_date
            except ValueError:
                flash("กรุณากรอกจำนวนวันให้ถูกต้อง")
                return redirect(request.url)
        else:
            updates["expire_date"] = user.get("expire_date")

        # ถ้ามีการกรอกรหัสผ่านใหม่ ให้เปลี่ยน
        if new_password:
            updates["password"] = generate_password_hash(new_password)
        # อัปเดต role ถ้ามีฟิลด์นี้
        if role:
            updates["role"] = role

        mongo_db.users.update_one({"username": username}, {"$set": updates})
        flash("บันทึกการเปลี่ยนแปลงสำเร็จ")
        return redirect(url_for("admin_panel"))

    return render_template("admin_edit_user.html", user=user)

@app.route("/admin/delete_user/<username>", methods=["POST"])
@require_admin
def admin_delete_user(username):
    mongo_db.users.delete_one({"username": username})
    flash("ลบผู้ใช้เรียบร้อยแล้ว")
    return redirect(url_for("admin_panel"))

@app.route("/admin/add_credit/<username>", methods=["POST"])
@require_admin
def admin_add_credit(username):
    amount = int(request.form["amount"])
    add_credit(username, amount)
    flash(f"เติมเครดิตให้ {username} สำเร็จ +{amount} เครดิต")
    return redirect(url_for("admin_edit_user", username=username))

@app.route('/admin/topup_slips')
@require_admin
def admin_topup_slips():
    slips = list(mongo_db.topup_slips.find().sort("created_at", -1))
    return render_template("admin_topup_slips.html", slips=slips)

@app.route('/admin/topup_slip/<slip_id>/approve', methods=['POST'])
@require_admin
def approve_topup_slip(slip_id):
    slip = mongo_db.topup_slips.find_one({"_id": ObjectId(slip_id)})
    if slip and slip["status"] == "pending":
        # เพิ่มเครดิตให้ user
        mongo_db.users.update_one(
            {"username": slip["username"]},
            {"$inc": {"credit": int(slip["amount"])}}
        )
        mongo_db.topup_slips.update_one(
            {"_id": ObjectId(slip_id)},
            {"$set": {"status": "approved", "approved_at": datetime.now()}}
        )
        flash("อนุมัติเติมเงินเรียบร้อยแล้ว")
    return redirect(url_for("admin_topup_slips"))

@app.route('/admin/topup_slip/<slip_id>/reject', methods=['POST'])
@require_admin
def reject_topup_slip(slip_id):
    comment = request.form.get('admin_comment', '')
    mongo_db.topup_slips.update_one(
        {"_id": ObjectId(slip_id)},
        {"$set": {"status": "rejected", "admin_comment": comment}}
    )
    flash("ปฏิเสธสลิปนี้แล้ว")
    return redirect(url_for("admin_topup_slips"))

# --- TOPUP เติมเงิน ---
@app.route('/topup', methods=['GET', 'POST'])
@require_web_login
def topup():
    if request.method == 'POST':
        amount = int(request.form['amount'])
        file = request.files['slip']
        if file and allowed_file(file.filename):
            imgbb_url = upload_to_imgbb(file)
            if not imgbb_url:
                flash("อัปโหลดรูปสลิปไม่สำเร็จ กรุณาลองใหม่")
                return redirect(url_for("topup"))
            slip = {
                "username": session['user_login'],
                "amount": amount,
                "type": "slip",
                "image": imgbb_url,
                "qr_ref": "123456",
                "status": "pending",
                "created_at": datetime.now()
            }
            result = mongo_db.topup_slips.insert_one(slip)
            slip['_id'] = result.inserted_id
            notify_telegram_admin_topup(slip)
            flash("อัปโหลดสลิปสำเร็จ กรุณารอแอดมินตรวจสอบ")
            return redirect(url_for("topup_history"))
        else:
            flash("กรุณาแนบไฟล์สลิปที่ถูกต้อง")
    return render_template("topup_slip.html")

@app.route('/topup-history')
def topup_history():
    if not session.get("user_login"):
        return redirect(url_for("login"))
    username = session["user_login"]
    # สมมุติว่า topup_slips เป็น collection ที่เก็บทั้งสลิปและ QR/วิธีอื่นๆ
    # filter เฉพาะของ user นั้นๆ และเรียงล่าสุดไว้บน
    slips = list(mongo_db.topup_slips.find({"username": username}).sort("created_at", -1))
    return render_template("topup_history.html", slips=slips)

@app.route('/topup_approve/<slip_id>', methods=['GET', 'POST'])
@require_admin
def topup_approve(slip_id):
    slip = mongo_db.topup_slips.find_one({"_id": ObjectId(slip_id)})
    if not slip:
        return "ไม่พบข้อมูลสลิปนี้", 404
    if request.method == 'POST':
        mongo_db.topup_slips.update_one(
            {"_id": ObjectId(slip_id)},
            {"$set": {"status": "approved", "approved_at": datetime.now()}}
        )
        # (option) แจ้ง LINE OA
        flash("อนุมัติสลิปสำเร็จ!")
        return redirect(url_for('admin_dashboard'))
    return render_template("topup_approve.html", slip=slip)

@app.route('/topup_reject/<slip_id>', methods=['GET', 'POST'])
@require_admin
def topup_reject(slip_id):
    slip = mongo_db.topup_slips.find_one({"_id": ObjectId(slip_id)})
    if not slip:
        return "ไม่พบข้อมูลสลิปนี้", 404
    if request.method == 'POST':
        mongo_db.topup_slips.update_one(
            {"_id": ObjectId(slip_id)},
            {"$set": {"status": "rejected", "rejected_at": datetime.now()}}
        )
        flash("ปฏิเสธสลิปแล้ว!")
        return redirect(url_for('admin_dashboard'))
    return render_template("topup_reject.html", slip=slip)

# --- OA SELECTOR ---
@app.route("/", methods=["GET", "POST"])
@require_web_login
def login():
    oa_list = get_user_oa_list(session["user_login"])
    if request.method == "POST":
        selected_id = request.form.get("oa_id")
        oa = get_oa_by_id(session["user_login"], selected_id)
        if oa:
            oa.pop('_id', None)
            session["current_oa"] = oa
            return redirect(url_for("dashboard"))
        else:
            flash("เลือก OA ไม่สำเร็จ")
    return render_template("login.html", oa_list=oa_list)

@app.route("/callback", methods=["POST"])
def callback():
    body = request.get_json(silent=True)  # รับ JSON
    if not body:
        return "Invalid", 400

    oa_id = body.get("oa_id")
    username = body.get("username")
    events = body.get("events", [])

    for event in events:
        if event.get("type") in ("follow", "message"):
            source = event.get("source", {})
            user_id = source.get("userId")
            if user_id and username and oa_id:
                save_userid_to_oa(oa_id, user_id)
    return "OK"

# --- OA MANAGE ---
@app.route("/add_oa", methods=["GET", "POST"])
@require_web_login
def add_oa():
    if request.method == "POST":
        name = request.form.get("name")
        channel_access_token = request.form.get("access_token")
        channel_secret = request.form.get("secret")
        oa_id = str(int(time.time() * 1000))
        new_oa = {
            "id": oa_id,
            "name": name,
            "access_token": channel_access_token,
            "secret": channel_secret
        }
        add_oa_to_user(session["user_login"], new_oa)
        flash("เพิ่ม OA สำเร็จแล้ว")
        return redirect(url_for("login"))
    oa_list = get_user_oa_list(session["user_login"])
    return render_template("add_oa.html", oa_list=oa_list)

@app.route("/delete_oa/<oa_id>", methods=["POST"])
@require_web_login
def delete_oa_route(oa_id):
    delete_oa_from_user(session["user_login"], oa_id)
    flash("ลบบัญชี OA เรียบร้อยแล้ว")
    return redirect(url_for("add_oa"))

@app.route("/dashboard")
@require_web_login
@require_oa
def dashboard():
    oa_id = session["current_oa"]["id"]
    oa = get_current_oa_from_db(oa_id)
    total_friends = get_total_friends(oa_id)
    return render_template("dashboard.html", total_friends=total_friends, oa=oa, today=datetime.now().strftime('%Y-%m-%d'))

@app.route("/switch_oa", methods=["GET", "POST"])
@require_web_login
def switch_oa():
    # ลบ session OA ที่เคยล็อกอินไว้ (ก่อนหน้านี้) ทิ้งก่อนเสมอ
    session.pop("current_oa", None)
    
    username = session.get("user_login")  # หรือ "username" แล้วแต่ระบบคุณ
    oa_list = get_user_oa_list(username)

    if request.method == "POST":
        selected_oa_id = request.form.get("oa_id")
        selected_oa = next((oa for oa in oa_list if str(oa.get("id")) == selected_oa_id), None)
        if selected_oa:
            session["current_oa"] = selected_oa
        return redirect(url_for("dashboard"))  # หรือหน้าหลัก

    return render_template("switch_oa.html", oa_list=oa_list)

# --- WEBHOOK ---
@app.route("/line/webhook", methods=["POST"])
def add_line_userid():
    data = request.get_json()
    print("==== [DEBUG] webhook payload ====")
    print(data)
    user_id = data.get("user_id")
    oa_id = data.get("oa_id")
    username = data.get("username")

    # กรณี manual จาก App Script/ระบบอื่น
    if user_id and oa_id and username:
        print(f"[DEBUG] Manual add user_id={user_id} to oa_id={oa_id} by username={username}")
        save_userid_to_oa(oa_id, user_id)
        return jsonify({"status": "ok", "type": "manual"}), 200

    # กรณีมาจาก LINE Webhook ทั่วไป
    if "events" in data and "destination" in data:
        oa_mid = data["destination"]
        print("oa_mid from webhook:", oa_mid)
        mapped_oa_id = get_oa_id_from_mid(oa_mid, target_oa_id=oa_id)
        print("mapped_oa_id:", mapped_oa_id)
        found = False
        missing_mapping = False
        for event in data["events"]:
            user_id = event.get("source", {}).get("userId")
            print("user_id from event:", user_id)
            if user_id and mapped_oa_id:
                save_userid_to_oa(mapped_oa_id, user_id)
                found = True
            elif user_id and not mapped_oa_id:
                missing_mapping = True

        if found:
            return jsonify({"status": "ok", "type": "webhook"}), 200
        elif missing_mapping:
            print("[ERROR] OA MID นี้ยังไม่ได้ mapping กับ oa_id ในระบบ กรุณาให้ admin เพิ่ม mid ให้ oa_list ด้วยตัวเอง")
            return jsonify({
                "status": "error",
                "msg": "oa_mid ยังไม่ได้ mapping กับ oa_id กรุณาเพิ่ม mid ในฐานข้อมูลก่อนใช้งาน"
            }), 200
        else:
            print("[ERROR] No userId or cannot map oa_id")
            return jsonify({"status": "error", "msg": "no userId or cannot map oa_id"}), 200

    print("[ERROR] missing or invalid data")
    return jsonify({"status": "error", "msg": "missing or invalid data"}), 200

@app.route("/api/get_oa_id", methods=["POST"])
def api_get_oa_id():
    data = request.get_json()
    user_id = data.get("user_id")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    result = get_api_oa_from_db(user_id)
    if result:
        return jsonify({"oa_id": result["oa_id"], "username": result["username"]})

    return jsonify({"oa_id": None, "username": None}), 404

# --- ส่งข้อความ/รูปภาพ ---
@app.route("/send", methods=["GET", "POST"])
@require_web_login
@require_oa
def send_msg():
    oa = session["current_oa"]
    user = session.get("user_login")   # <-- ใช้ user แทน global
    api = LineAPI(oa["access_token"])
    user_ids = get_followers(oa["id"])
    uploaded_image_url = None

    if request.method == "POST":
        message_id = "msg_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        print("user_ids", user_ids)
        if not user_ids:
            flash("ไม่มีผู้ติดตามในระบบ (user_ids ว่าง) กรุณานำเข้ารายชื่อหรือเช็ค Access Token")
            return redirect(url_for("send_msg"))

        # --- ดึงข้อความและรูปหลายบล็อกจาก form ---
        messages = []
        for key in request.form:
            if key.startswith("messages[") and key.endswith("][text]"):
                value = request.form[key].strip()
                if value:
                    messages.append({"type": "text", "text": value})
        for key in request.files:
            if key.startswith("messages[") and key.endswith("][image]"):
                file = request.files[key]
                if file and file.filename:
                    image_url = upload_to_imgbb(file)
                    messages.append({"type": "image", "image_url": image_url})

        if not messages:
            flash("ต้องกรอกข้อความหรือเลือกรูปอย่างน้อย 1 อย่าง")
            return redirect(url_for("send_msg"))

        target = request.form.get("target")
        send_time_option = request.form.get("send_time_option", "now")
        scheduled_time = request.form.get("scheduled_time")

        # --- กรณีตั้งเวลาส่ง ---
        if send_time_option == "schedule" and scheduled_time:
            log_message_send(
                message_id=message_id,
                user_id=target,
                oa_id=oa["id"],
                status="pending",
                msg_type="multi",
                detail={
                    "messages": messages,
                    "scheduled_time": datetime.fromisoformat(scheduled_time)
                },
                scheduled_time=datetime.fromisoformat(scheduled_time)
            )
            flash("บันทึกคิวข้อความเรียบร้อย จะส่งอัตโนมัติเมื่อถึงเวลาที่กำหนด")
            return redirect(url_for("send_msg"))

        # --- ฟังก์ชันส่งข้อความ, อัปเดต progress แบบ per-user ---
        def send_all_msgs(to_user_ids):
            total_sent, total_failed, skipped = 0, 0, 0
            DELAY_SEC = 5  # กัน rate limit LINE (ถ้า user เยอะมากอาจเพิ่มเป็น 1.5-2 วิ)
            send_progress_by_user[user] = {
                "current": 0,
                "total": len(to_user_ids),
                "fail": 0,
                "done": False,
                "user_id": len(user_ids)
            }
            send_cancelled = False

            for user_id in to_user_ids:
                if send_cancelled:
                    break
                skip_this = False
                for msg in messages:
                    detail = {
                        "type": msg["type"],
                        "text": msg.get("text"),
                        "image_url": msg.get("image_url")
                    }
                    if already_sent_recently(user_id, oa["id"], msg["type"], detail, hours=6):
                        skipped += 1
                        skip_this = True
                        break
                if skip_this:
                    continue
                try:
                    for msg in messages:
                        if msg["type"] == "text":
                            success = api.send_message(user_id, msg["text"], None)
                        elif msg["type"] == "image":
                            success = api.send_message(user_id, "", msg["image_url"])
                        else:
                            success = False  # กัน type อื่นๆ
                        if success:
                            total_sent += 1
                            send_progress_by_user[user]["current"] += 1
                            log_message_send(message_id, user_id, oa["id"], "success", msg["type"], msg)
                        else:
                            total_failed += 1
                            send_progress_by_user[user]["fail"] += 1
                            log_message_send(message_id, user_id, oa["id"], "fail", msg["type"], msg)
                    time.sleep(DELAY_SEC)
                except Exception as e:
                    total_failed += 1
                    send_progress_by_user[user]["fail"] += 1
                    log_message_send(message_id, user_id, oa["id"], "fail", "multi", {"messages": messages})
            send_progress_by_user[user]["done"] = True
            flash(f"ส่งข้อความ Broadcast (push ทีละคน) สำเร็จ: {total_sent} คน, ข้าม {skipped} คน, ล้มเหลว {total_failed} คน")

        if target == "broadcast":
            send_all_msgs(user_ids)
        else:
            user_id = target
            sent_flag = False
            send_progress_by_user[user] = {"current": 0, "total": 1, "fail": 0, "done": False}
            for msg in messages:
                detail = {"type": msg["type"], "text": msg.get("text"), "image_url": msg.get("image_url")}
                if already_sent_recently(user_id, oa["id"], msg["type"], detail, hours=6):
                    flash(f"ข้าม: ส่งไปหา {user_id} แล้วภายใน 6 ชั่วโมง")
                else:
                    if msg["type"] == "text":
                        result = api.send_message(user_id, msg["text"], None)
                    elif msg["type"] == "image":
                        result = api.send_message(user_id, "", msg["image_url"])
                    log_message_send(message_id, user_id, oa["id"], "success" if result else "fail", msg["type"], msg)
                    sent_flag = True
                    send_progress_by_user[user]["current"] += 1 if result else 0
                    send_progress_by_user[user]["fail"] += 0 if result else 1
            send_progress_by_user[user]["done"] = True
            if sent_flag:
                flash(f"ส่งข้อความถึง {user_id}: สำเร็จ")
            else:
                flash(f"ไม่มีข้อความใหม่ที่จะส่งถึง {user_id}")

        return redirect(url_for("send_msg"))

    auto_message_id = "msg_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    return render_template(
        "send_message.html",
        user_ids=user_ids,
        oa=oa,
        uploaded_image_url=uploaded_image_url,
        today=datetime.now().strftime('%Y-%m-%d'),
        auto_message_id=auto_message_id
    )

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    print("[DEBUG] upload request", request.remote_addr, dict(request.headers))
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# --- FLEX MESSAGE ---
@app.route("/send_flex", methods=["GET", "POST"])
@require_web_login
@require_oa
def send_flex_msg():
    import json as pyjson
    global send_progress, send_cancelled
    oa = session["current_oa"]
    api = LineAPI(oa["access_token"])
    user_ids = get_followers(oa["id"])
    templates = get_user_templates(session["user_login"])

    if request.method == "POST":
        message_id = "msg_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

        # --- อ่าน Flex หลายอันจาก form ---
        flexes = []
        # โครงสร้าง request.form: flexes[flex-block-1][template], flexes[flex-block-1][json], ...
        for key in request.form:
            if key.startswith("flexes[") and key.endswith("][template]"):
                block_id = key[len("flexes["):-len("][template]")]
                template_name = request.form[key]
                json_key = f"flexes[{block_id}][json]"
                flex_json = request.form.get(json_key, "")
                if not template_name or not flex_json.strip():
                    continue
                # หา alt_text จาก template
                selected = next((t for t in templates if t["name"] == template_name), None)
                alt_text = template_name
                if selected and "alt_text" in selected:
                    alt_text = selected["alt_text"]
                try:
                    flex_content = pyjson.loads(flex_json)
                except Exception as e:
                    flash(f"Flex #{block_id} รูปแบบริชเสเสจไม่ถูกต้อง: {e}")
                    return redirect(url_for("send_flex_msg"))
                flexes.append({
                    "altText": alt_text,
                    "json": flex_content,
                    "template": template_name
                })

        if not flexes:
            flash("ต้องเพิ่มบล็อกริชเมสเสจอย่างน้อย 1 อัน")
            return redirect(url_for("send_flex_msg"))

        # -- option อื่น ๆ --
        target = request.form.get("target")
        send_time_option = request.form.get("send_time_option", "now")
        scheduled_time = request.form.get("scheduled_time")

        # --- กรณีตั้งเวลาส่ง ---
        if send_time_option == "schedule" and scheduled_time:
            # Save ทุก Flex ไว้ใน pending-queue
            for fx in flexes:
                log_message_send(
                    message_id=message_id,
                    user_id=target,
                    oa_id=oa["id"],
                    status="pending",
                    msg_type="flex",
                    detail={
                        "json": fx["json"],
                        "altText": fx["altText"],
                        "scheduled_time": datetime.fromisoformat(scheduled_time)
                    },
                    scheduled_time=datetime.fromisoformat(scheduled_time)
                )
            flash("บันทึกคิวริชเมสเสจทั้งหมดเรียบร้อย จะส่งอัตโนมัติเมื่อถึงเวลาที่กำหนด")
            return redirect(url_for("send_flex_msg"))

        # ------- ส่งทันที ---------
        def send_all_flex_msgs(to_user_ids):
            BATCH_SIZE = 1  # ทีละ 1 คน
            DELAY_SEC = 3   # ปรับเป็น 1 วิ เพื่อลดโอกาส rate limit (เลือกได้)
            user = session.get("user_login")
            global send_progress, send_cancelled, send_progress_by_user
            total_flex = len(flexes)
            total_users = len(to_user_ids)
            send_progress = {"current": 0, "total": total_users * total_flex, "fail": 0, "done": False, "user_id": len(user_ids)}
            send_cancelled = False
            send_progress_by_user[user] = send_progress.copy()
            total_sent, total_failed, skipped = 0, 0, 0

            for flex_index, fx in enumerate(flexes):
                if send_cancelled:
                    break
                msg_type = "flex"
                detail = {
                    "altText": fx["altText"],
                    "json": fx["json"]
                }
                for user_id in to_user_ids:
                    if send_cancelled:
                        break
                    if already_sent_recently(user_id, oa["id"], msg_type, detail, hours=6):
                        skipped += 1
                        continue
                    try:
                        success = api.send_flex(user_id, fx["json"], fx["altText"])
                        if success:
                            total_sent += 1
                            send_progress["current"] += 1
                            log_message_send(message_id, user_id, oa["id"], "success", msg_type, detail)
                        else:
                            total_failed += 1
                            send_progress["fail"] += 1
                            log_message_send(message_id, user_id, oa["id"], "fail", msg_type, detail)
                        send_progress_by_user[user] = send_progress.copy()
                    except Exception as e:
                        total_failed += 1
                        send_progress["fail"] += 1
                        log_message_send(message_id, user_id, oa["id"], "fail", msg_type, detail)
                        send_progress_by_user[user] = send_progress.copy()
                    time.sleep(DELAY_SEC)
            send_progress["done"] = True
            send_progress_by_user[user] = send_progress.copy()
            flash(f"ส่งริชเมสเสจทั้งหมดสำเร็จ: {total_sent} คน, ข้าม {skipped} คน, ล้มเหลว {total_failed} คน")

        if target == "broadcast":
            send_all_flex_msgs(user_ids)
        else:
            user_id = target
            sent_flag = False
            for fx in flexes:
                msg_type = "flex"
                detail = {
                    "altText": fx["altText"],
                    "json": fx["json"]
                }
                if already_sent_recently(user_id, oa["id"], msg_type, detail, hours=6):
                    flash(f"ข้าม: ส่งริชเมสเสจ [{fx['template']}] ไปหา {user_id} แล้วภายใน 6 ชั่วโมง")
                else:
                    result = api.send_flex(user_id, fx["json"], fx["altText"])

                    # --- ปรับปรุงตรงนี้ ---
                    is_success = (
                        isinstance(result, dict) and
                        not result.get("error") and
                        (result == {} or result.get("status") == "ok")
                    )
                    # print("DEBUG result:", result)   # << เปิด debug ตรงนี้ถ้ายังไม่มั่นใจ

                    log_message_send(
                        message_id, user_id, oa["id"],
                        "success" if is_success else "fail",
                        msg_type, detail
                    )
                    sent_flag = sent_flag or is_success  # มี success อย่างน้อย 1 อัน

                    # Flash error เฉพาะเคส fail
                    if not is_success:
                        err = result.get("error") if isinstance(result, dict) else str(result)
                        flash(f"❌ ส่งไม่สำเร็จ: {err}")

            if sent_flag:
                flash(f"ส่งริชเมสเสจถึง {user_id}: สำเร็จ")
            else:
                flash(f"ไม่มีริชเมสเสจใหม่ที่จะส่งถึง {user_id}")
        return redirect(url_for("send_flex_msg"))

    auto_message_id = "msg_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    return render_template(
        "send_flex.html",
        user_ids=user_ids,
        oa=oa,
        templates=templates,
        today=datetime.now().strftime('%Y-%m-%d'),
        auto_message_id=auto_message_id
    )

# --- หน้าประวัติการส่งข้อความ ---
@app.route('/message_history')
@require_web_login
def message_history():
    username = session["user_login"]
    oa_map = {}
    user = mongo_db.users.find_one({"username": username})
    all_logs = []
    if user and "oa_list" in user:
        for oa in user["oa_list"]:
            oa_id = oa["id"]
            oa_map[oa_id] = oa.get("name", oa_id)
            for log in oa.get("send_logs", []):
                log["_oa_id"] = oa_id
                all_logs.append(log)

    # Group by message_id
    grouped = defaultdict(list)
    for log in all_logs:
        grouped[log.get("message_id")].append(log)

    messages = []
    for msg_id, logs in grouped.items():
        main = logs[0].copy()
        all_user_ids = [l.get('user_id') for l in logs]
        main["send_count"] = len(set(all_user_ids))
        main["user_id_list"] = all_user_ids
        main["all_status"] = [l.get('status') for l in logs]
        # ---- Collect all_details ----
        unique_details = []
        seen = set()
        for l in logs:
            d = l.get("detail", {})
            msg_type = l.get("type") or d.get("type")
            # รองรับ multi, flex, text, image
            if msg_type == "multi" and d.get("messages"):
                for m in d["messages"]:
                    key = f"{m.get('type')}_{m.get('text','')}_{m.get('image_url','')}"
                    if key not in seen:
                        seen.add(key)
                        unique_details.append(m)
            elif msg_type == "flex":
                key = f"flex_{d.get('altText','')}_{str(d.get('json',''))}"
                if key not in seen:
                    seen.add(key)
                    unique_details.append({
                        "type": "flex",
                        "altText": d.get("altText", "-"),
                        "json": d.get("json", {})
                    })
            else:
                key = f"{msg_type}_{d.get('text','')}_{d.get('image_url','')}"
                if key not in seen:
                    seen.add(key)
                    dd = {
                        "type": msg_type,
                        "text": d.get("text"),
                        "image_url": d.get("image_url")
                    }
                    unique_details.append(dd)
        main["all_details"] = unique_details
        messages.append(main)

    messages = sorted(messages, key=lambda x: x.get("sent_at", datetime.min), reverse=True)
    return render_template("message_history.html", messages=messages, oa_map=oa_map)
# --- สร้าง FLEX MESSAGE ---
@app.route('/upload_imgbb', methods=['POST'])
def upload_imgbb():
    if 'image' not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    file = request.files['image']
    api_key = "38fff7fc24bb2d14a0729f5f50e6f17f"
    url = "https://api.imgbb.com/1/upload"
    payload = {"key": api_key}
    files = {"image": (file.filename, file.stream, file.mimetype)}
    response = requests.post(url, data=payload, files=files)
    if response.status_code == 200:
        data = response.json()
        return jsonify({"url": data["data"]["url"]})
    else:
        return jsonify({"error": "Failed to upload image"}), 500

@app.route("/flex_templates/list", methods=["GET"])
@require_web_login
def flex_templates_list():
    username = session["user_login"]
    templates = get_user_templates(username)
    return render_template("flex_templates_list.html", templates=templates)

@app.route("/flex_templates/create", methods=["GET", "POST"])
@require_web_login
def flex_templates_create():
    username = session["user_login"]
    if request.method == "POST":
        name = request.form.get("template_name")
        flex_json = request.form.get("flex_json")
        alt_text = request.form.get("alt_text") or name  # ถ้า alt_text ว่างให้ใช้ชื่อ template

        import json as pyjson
        try:
            flex_content = pyjson.loads(flex_json)
            # เพิ่ม created_at และ alt_text ใน template ใหม่
            success = add_template(username, name, flex_content, alt_text=alt_text, created_at=datetime.now())
            if success:
                flash("บันทึก Template เรียบร้อยแล้ว!")
                return redirect(url_for("flex_templates_list"))
            else:
                flash("มีชื่อ Template นี้แล้ว")
        except Exception as e:
            flash(f"ไม่สามารถบันทึก Flex JSON นี้ได้: {e}")
    # ฟอร์มสร้างใหม่ ไม่ต้องส่ง template (ค่า default)
    return render_template("flex_templates_create.html", template=None)

@app.route("/flex_templates/edit/<template_name>", methods=["GET", "POST"])
@require_web_login
def flex_templates_edit(template_name):
    username = session["user_login"]
    template = get_template_by_name(username, template_name)
    if not template:
        flash("ไม่พบ Template ที่ต้องการแก้ไข")
        return redirect(url_for("flex_templates_list"))
    if request.method == "POST":
        flex_json = request.form.get("flex_json")
        import json as pyjson
        try:
            flex_content = pyjson.loads(flex_json)
            update_template(username, template_name, flex_content)
            flash("อัปเดต Template เรียบร้อยแล้ว!")
            return redirect(url_for("flex_templates_list"))
        except Exception as e:
            flash(f"ไม่สามารถบันทึก Flex JSON นี้ได้: {e}")
    return render_template("flex_templates_edit.html", template=template)

@app.route("/delete_flex_template/<template_name>", methods=["POST"])
@require_web_login
def delete_flex_template(template_name):
    username = session["user_login"]
    user = mongo_db.users.find_one({"username": username})
    if not user:
        flash("ไม่พบผู้ใช้นี้")
        return redirect(url_for("flex_templates_list"))
    templates = user.get("flex_templates", [])
    new_templates = [t for t in templates if t["name"] != template_name]
    mongo_db.users.update_one(
        {"username": username},
        {"$set": {"flex_templates": new_templates}}
    )
    flash(f"ลบ Template '{template_name}' เรียบร้อยแล้ว")
    return redirect(url_for("flex_templates_list"))  # แก้ตรงนี้!

# --- Progress & Cancel ตอนส่งข้อความ ---
send_progress_by_user = {}

@app.route("/send_progress")
def send_progress_status():
    user = session.get("user_login")
    return jsonify(send_progress_by_user.get(user, {
        "current": 0, "total": 0, "fail": 0, "done": False, "user_id": 0
    }))

@app.route("/cancel_send", methods=["POST"])
def cancel_send():
    global send_cancelled
    send_cancelled = True
    return "", 204

# --- นำเข้ารายชื่อ Contact ---
@app.route("/import_users", methods=["POST"])
def import_users():
    file = request.files.get('file')
    if not file or file.filename == '':
        flash("กรุณาเลือกไฟล์")
        return redirect(url_for('dashboard'))

    oa_id = request.form.get("oa_id") or request.args.get("oa_id")

    if not oa_id:
        flash("ไม่พบข้อมูล OA")
        return redirect(url_for('dashboard'))

    try:
        # อ่านไฟล์ CSV/Excel
        if file.filename.endswith(".csv"):
            df = pd.read_csv(file)
        elif file.filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(file)
        else:
            flash("รองรับเฉพาะไฟล์ .csv, .xlsx, .xls")
            return redirect(url_for('dashboard'))

        col = "userId" if "userId" in df.columns else df.columns[0]
        user_ids = df[col].dropna().astype(str).tolist()

        for user_id in user_ids:
            save_userid_to_oa(oa_id, user_id)
        flash(f"นำเข้า {len(user_ids)} รายชื่อสำเร็จ")
        return redirect(url_for('dashboard', oa_id=oa_id))
    except Exception as e:
        flash("นำเข้าไฟล์ผิดพลาด: %s" % e)
        return redirect(url_for('dashboard', oa_id=oa_id))

@app.route('/export_oa_userids/<oa_id>')
@require_web_login
def export_oa_userids(oa_id):
    oa = get_current_oa_from_db(oa_id)
    if not oa:
        flash("ไม่พบ OA หรือไม่มีรายชื่อ")
        return redirect(url_for("dashboard"))  # หรือหน้าที่ต้องการ

    user_ids = oa.get("user_ids", [])
    # สร้างไฟล์ CSV ในหน่วยความจำ
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["user_id"])  # Header
    for uid in user_ids:
        cw.writerow([uid])

    # ส่งออกเป็นไฟล์ดาวน์โหลด
    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=oa_user_ids_{oa_id}.csv"
        }
    )

@app.route("/clear_users", methods=["POST"])
def clear_users():
    from flask import request, redirect, url_for, flash

    oa_id = request.form.get("oa_id")
    if not oa_id:
        flash("ไม่พบ OA_ID")
        return redirect(request.referrer)

    removed_count = clear_user_ids_of_oa(oa_id)
    if removed_count is not None:
        flash(f"ลบรายชื่อทั้งหมดของ OA นี้แล้ว ({removed_count} คน)")
    else:
        flash("ไม่พบข้อมูล OA นี้")
    return redirect(request.referrer or url_for('dashboard'))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
