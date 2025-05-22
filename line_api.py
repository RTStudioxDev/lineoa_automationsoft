import requests

class LineAPI:
    def __init__(self, channel_access_token):
        self.channel_access_token = channel_access_token
        self.base_url = "https://api.line.me/v2/bot"

    def get_headers(self):
        return {
            "Authorization": f"Bearer {self.channel_access_token}",
            "Content-Type": "application/json"
        }

    def get_followers(self):
        url = f"{self.base_url}/followers/ids"
        user_ids = []
        next_token = ""
        while True:
            params = {}
            if next_token:
                params['start'] = next_token
            r = requests.get(url, headers=self.get_headers(), params=params)
            if r.status_code != 200:
                break
            data = r.json()
            user_ids += data.get("userIds", [])
            next_token = data.get("next", "")
            if not next_token:
                break
        return user_ids

    def get_profile(self, user_id):
        url = f"{self.base_url}/profile/{user_id}"
        r = requests.get(url, headers=self.get_headers())
        if r.status_code == 200:
            return r.json()
        return {}

    def send_message(self, user_id, text, image_url=None):
        url = "https://api.line.me/v2/bot/message/push"
        headers = {
            "Authorization": f"Bearer {self.channel_access_token}",
            "Content-Type": "application/json"
        }
        messages = [{"type": "text", "text": text}]
        if image_url:
            messages.append({
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url
            })
        body = {
            "to": user_id,
            "messages": messages
        }
        resp = requests.post(url, headers=headers, json=body)
        return resp.status_code == 200

    def send_broadcast(self, text, image_url=None):
        url = f"{self.base_url}/message/broadcast"
        messages = []
        if text:
            messages.append({"type": "text", "text": text})
        if image_url:
            messages.append({
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url
            })
        data = {
            "messages": messages
        }
        r = requests.post(url, headers=self.get_headers(), json=data)
        return r.json()

    def send_flex(self, user_id, flex_content, alt_text="ข้อความ Flex"):
        url = f"{self.base_url}/message/push"
        data = {
            "to": user_id,
            "messages": [{
                "type": "flex",
                "altText": alt_text,
                "contents": flex_content
            }]
        }
        try:
            response = requests.post(url, headers=self.get_headers(), json=data)
            print("LINE API response:", response.status_code, response.text)  # <--- เพิ่มบรรทัดนี้!
            response.raise_for_status()
            return response.json() if response.content else {"status": "ok"}  # LINE API push ไม่มี body มัก return {}
        except requests.RequestException as e:
            print("LINE API error:", e)
            return {"error": str(e), "response": getattr(e, 'response', None)}
        
    def broadcast_flex(self, flex_content, alt_text="ข้อความ Flex"):
        url = f"{self.base_url}/message/broadcast"
        data = {
            "messages": [{
                "type": "flex",
                "altText": alt_text,
                "contents": flex_content
            }]
        }
        r = requests.post(url, headers=self.get_headers(), json=data)
        return r.json()

    def send_multicast(self, user_id_list, text, image_url=None):
        url = f"{self.base_url}/message/multicast"
        messages = []
        if text:
            messages.append({"type": "text", "text": text})
        if image_url:
            messages.append({
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url
            })
        data = {
            "to": user_id_list[:500],  # ไม่เกิน 500
            "messages": messages
        }
        r = requests.post(url, headers=self.get_headers(), json=data)
        try:
            r.raise_for_status()
            # LINE จะตอบ status 200 ถ้าส่งสำเร็จ
            return True
        except Exception:
            return False

    def send_multicast_flex(self, user_id_list, flex_content, alt_text="ข้อความ Flex"):
        url = f"{self.base_url}/message/multicast"
        data = {
            "to": user_id_list[:500],
            "messages": [{
                "type": "flex",
                "altText": alt_text,
                "contents": flex_content
            }]
        }
        r = requests.post(url, headers=self.get_headers(), json=data)
        print("[DEBUG] user_id_list:", user_id_list)
        print("[DEBUG] URL:", url)
        print("[DEBUG] Headers:", self.get_headers())
        print("[DEBUG] Data:", data)
        print("[DEBUG] Response status:", r.status_code)
        print("[DEBUG] Response text:", r.text)
        try:
            r.raise_for_status()
            return True
        except Exception as e:
            print("[ERROR] Failed to send flex:", str(e))
            return False
