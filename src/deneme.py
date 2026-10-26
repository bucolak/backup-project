import os

KAYNAK = "C:/Users/W10/Desktop/backup-project/source"
HEDEF = "C:/Users/W10/Desktop/backup-project/destination"

tam_yol = os.path.join(KAYNAK, "belgeler", "not.txt")
print(tam_yol)

goreli = os.path.relpath(tam_yol, KAYNAK)
print(goreli)

print(os.path.join(HEDEF, goreli))