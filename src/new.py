import hashlib

sha256 = hashlib.sha256()
sha256.update(b"Hello")
print(sha256.hexdigest())