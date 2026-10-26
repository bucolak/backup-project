import os, shutil

KAYNAK = "C:/Users/W10/Desktop/backup-project/source"
HEDEF = "C:/Users/W10/Desktop/backup-project/destination"

for a,b,c in os.walk(KAYNAK) :
    for d in c:
        tamyol = os.path.join(a, d)
        print(tamyol)
        goreli = os.path.relpath(tamyol, KAYNAK)
        print(goreli)
        hedef = os.path.join(HEDEF, goreli)
        print(hedef)
        last = os.path.dirname(hedef)
        os.makedirs(last, exist_ok = True)
        shutil.copy(tamyol, last)