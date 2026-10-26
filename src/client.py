import requests, hashlib, os

response = requests.get("http://192.168.89.128:8000/")
print(response.status_code)
print(response.json())

with open("test.txt", "rb") as dosya:
    response1 = requests.post(
        "http://192.168.89.128:8000/upload",
        files={"dosya": dosya}
    )
print(response1.status_code)
print(response1.json()["message"])
print(response1.json()["sha256"])
print(response1.json()["size"])

sha256 = hashlib.sha256()
with open("test.txt", "rb") as outgoing_file: 
        while True:
            parca = outgoing_file.read(4096)

            if not parca:
                break
            sha256.update(parca)
if(response1.json()["sha256"] != sha256.hexdigest()):
    print("Hash checked: Failed!")
else:
    print("Hash checked: Succeeded!")

size = os.path.getsize("test.txt")
if(size != response1.json()["size"]):
    print("Size checked: Failed!")
else:
    print("Size checked: Succeeded!")