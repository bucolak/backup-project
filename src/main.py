import shutil, os

def copy_file(source,destination):
    for a,b,c in os.walk(source):
        for d in c:
            fullpath = os.path.join(a, d)
            goreli = os.path.relpath(fullpath, source)
            dst = os.path.join(destination, goreli)
            os.makedirs(os.path.dirname(dst), exist_ok = True)
            shutil.copy(fullpath, dst)
    print("copied")

KAYNAK = "C:/Users/W10/Desktop/backup-project/source"
HEDEF = "C:/Users/W10/Desktop/backup-project/destination"
copy_file(KAYNAK, HEDEF)