import requests
r = requests.post('http://127.0.0.1:5000/api/get_oa_id', json={"user_id": "xxxx"})
print(r.status_code, r.text)