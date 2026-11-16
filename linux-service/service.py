import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import signal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
STATUS_PATH = os.path.join(BASE_DIR, "status.json")
HISTORY_PATH = os.path.join(BASE_DIR, "history.json")
TEMP_SUFFIX = ".bksvc.part"   # servisin geçici dosyalarının uzantısı; taramada atlanır
WORKERS = 4
CHUNK_SIZE = 1024 * 1024   # 1 MB
DEFAULT_RETRY = {"count": 3, "delay": 60}   # 3 kez, 60 saniye arayla
ACCESS_RECHECK = 60   # kaynak/hedef izin sorunu varsa kaç saniyede bir tekrar bakılacak
API_URL = "http://127.0.0.1:8001"   # API başka bir bilgisayardaysa onun adresi yazılmalı
API_CHECK_SECONDS = 30                    # API'ye kaç saniyede bir sorulacak
LOG_NAME_RE = re.compile(r"^log_(\d{4}-\d{2}-\d{2})\.jsonl$")

# config.json'daki "settings" bölümünde yazılmayan ayarlar için varsayılanlar
DEFAULT_SETTINGS = {
    "log_dir": "logs",            # log klasörü (göreli ise service.py'nin yanında)
    "log_retention_days": 30,     # kaç günlük log tutulacak (0 = hiç silme)
    "history_max": 1000,          # history.json'da tutulacak son biten dosya sayısı
    "min_free_mb": 100            # hedef diskte her zaman boş bırakılacak alan
}


# Windows'ta 260 karakter sınırını aşmak için uzun yol öneki (\\?\)
if os.name == "nt":
    def lp(path):
        path = os.path.abspath(path)
        if path.startswith("\\\\?\\"):
            return path
        if path.startswith("\\\\"):                 # ağ yolu: \\sunucu\paylasim
            return "\\\\?\\UNC\\" + path[2:]
        return "\\\\?\\" + path

    def unlp(path):
        if path.startswith("\\\\?\\UNC\\"):
            return "\\\\" + path[8:]
        if path.startswith("\\\\?\\"):
            return path[4:]
        return path
else:
    def lp(path):
        return os.path.abspath(path)

    def unlp(path):
        return path


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "time": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "message": record.getMessage()
        }
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


class DailyJsonHandler(logging.Handler):
    """Her gün için ayrı log dosyası (logs/log_2026-11-15.jsonl); eski günleri siler."""

    def __init__(self, folder, retention_days):
        super().__init__()
        self.folder = folder
        self.retention_days = retention_days
        self.day = None
        self.stream = None

    def _open(self, day):
        if self.stream:
            self.stream.close()
        os.makedirs(self.folder, exist_ok=True)
        self.stream = open(os.path.join(self.folder, "log_%s.jsonl" % day), "a", encoding="utf-8")
        self.day = day
        self._cleanup(day)

    def _cleanup(self, today):
        if not self.retention_days:
            return
        # Bugün dahil son retention_days günü tut, daha eskileri sil
        limit = date.fromisoformat(today) - timedelta(days=self.retention_days)
        for name in os.listdir(self.folder):
            match = LOG_NAME_RE.match(name)
            if match and date.fromisoformat(match.group(1)) <= limit:
                try:
                    os.remove(os.path.join(self.folder, name))
                except OSError:
                    pass

    def emit(self, record):
        try:
            day = datetime.fromtimestamp(record.created).date().isoformat()
            if day != self.day:
                self._open(day)   # gün değişti: yeni dosyaya geç
            self.stream.write(self.format(record) + "\n")
            self.stream.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        super().close()


log = logging.getLogger("BackupService")
log.setLevel(logging.INFO)
log.propagate = False

settings = dict(DEFAULT_SETTINGS)
log_settings = None    # şu an kullanılan (log klasörü, saklama günü)
min_free_bytes = DEFAULT_SETTINGS["min_free_mb"] * 1024 * 1024


def apply_log_settings():
    """Log klasörü ya da saklama süresi değiştiyse log dosyasını yeniden ayarlar."""
    global log_settings
    folder = settings["log_dir"]
    if not os.path.isabs(folder):
        folder = os.path.join(BASE_DIR, folder)
    wanted = (folder, settings["log_retention_days"])
    if wanted == log_settings:
        return

    handler = DailyJsonHandler(folder, settings["log_retention_days"])
    handler.setFormatter(JsonFormatter())
    for old in list(log.handlers):
        log.removeHandler(old)
        old.close()
    log.addHandler(handler)
    log_settings = wanted


apply_log_settings()   # config okunana kadar varsayılan ayarlarla logla

DAYS = ["pzt", "sal", "car", "per", "cum", "cmt", "paz"]
SCHEDULE_TYPES = ["daily", "weekly", "interval"]
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")   # HH:MM, örn. 03:00

jobs = []            # config'teki geçerli işler
config_mtime = None  # config'in en son okunduğu andaki değiştirilme zamanı
config_errors = []   # config'teki hatalı kayıtlar (status.json'da gösterilir)

last_runs = {}   # (source, target) -> işin son çalıştığı zaman

running = {}                      # (source, target) -> {"pending", "failed_paths", "retries_left"}
retries = {}                      # (source, target) -> {"paths", "at", "left"}: bekleyen yeniden denemeler
active = {}                       # (iş, dosya yolu) -> şu an kopyalanan dosyanın bilgisi
history = deque(maxlen=DEFAULT_SETTINGS["history_max"])   # biten dosyalar; dolunca en eskisi atılır
history_dirty = False             # history değişti mi, dosyaya yazılması gerekiyor mu?
last_errors = {}                  # (source, target) -> işin son hata mesajı
access_problems = {}              # (source, target) -> {"error", "next_check"}: izin sorunu olan işler
reserved = {}                     # disk -> şu an yazılan dosyalar için ayrılmış bayt
space_lock = threading.Lock()     # boş alan hesabına aynı anda tek thread dokunsun
running_lock = threading.Lock()   # ortak verilere aynı anda tek thread dokunsun
state_lock = threading.Lock()     # state.json'a aynı anda tek thread yazsın
stopping = threading.Event()      # servis durduruluyor mu?
last_api_check = 0                # API'ye en son ne zaman soruldu
api_error = False                 # API'ye ulaşılamadığı zaten loglandı mı?


def sync_from_api():
    """API'deki işleri alır; config.json'dakilerden farklıysa config.json'ı günceller.
    config.json değişince reload_config() yeni işleri kendisi okur."""
    global api_error
    try:
        with urllib.request.urlopen(API_URL + "/jobs", timeout=5) as response:
            api_jobs = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as error:
        if not api_error:
            log.error("API could not be reached, keeping current config: %s", error)
            api_error = True
        return

    if api_error:
        log.info("API reachable again")
        api_error = False

    try:
        config = read_config()
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}

    if config.get("jobs") == api_jobs:
        return   # değişiklik yok

    config["jobs"] = api_jobs   # sadece işler değişir, settings olduğu gibi kalır
    write_json(CONFIG_PATH, config)
    log.info("Config updated from API: %d job(s)", len(api_jobs))


def read_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def now_text():
    return datetime.now().isoformat(timespec="seconds")


def write_json(path, data):
    """Önce geçici dosyaya yazar, sonra tek adımda yerine koyar; yarım dosya oluşmaz."""
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(temp_path, path)


def state_key(key):
    # JSON'da anahtar metin olmak zorunda: ("C:\\A", "D:\\A") -> "C:\\A|D:\\A"
    return key[0] + "|" + key[1]


def load_state():
    """Servis açılırken son çalışma zamanlarını ve bekleyen yeniden denemeleri state.json'dan okur."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as error:
        log.error("State file could not be read, starting without it: %s", error)
        return

    for text_key, entry in data.items():
        source, _, target = text_key.partition("|")
        key = (source, target)
        try:
            if isinstance(entry, str):
                entry = {"last_run": entry}   # eski biçim: sadece son çalışma zamanı

            if entry.get("last_run"):
                last_runs[key] = datetime.fromisoformat(entry["last_run"])

            retry = entry.get("retry")
            if retry:
                retries[key] = {
                    "paths": list(retry["paths"]),
                    "at": datetime.fromisoformat(retry["at"]).timestamp(),
                    "left": int(retry["left"])
                }
        except (TypeError, ValueError, KeyError, AttributeError):
            log.error("Invalid entry in state file skipped: %s", text_key)


def save_state():
    """Son çalışma zamanlarını ve bekleyen yeniden denemeleri state.json'a yazar."""
    with running_lock:
        data = {}
        for key in set(last_runs) | set(retries):
            last_run = last_runs.get(key)
            retry = retries.get(key)
            data[state_key(key)] = {
                "last_run": last_run.isoformat(timespec="seconds") if last_run else None,
                "retry": {
                    "paths": list(retry["paths"]),
                    "at": datetime.fromtimestamp(retry["at"]).isoformat(timespec="seconds"),
                    "left": retry["left"]
                } if retry else None
            }

    with state_lock:
        try:
            write_json(STATE_PATH, data)
        except OSError as error:
            log.error("State file could not be written: %s", error)


def norm_path(path):
    # Yolları karşılaştırılabilir hale getir: tam yol, Windows'ta küçük harf, sonda \ olmadan
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def validate_job(item):
    """İş geçerliyse None, değilse hatanın açıklamasını döndürür."""
    if not isinstance(item, dict):
        return "job must be an object"

    for field in ("source", "target"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            return field + " is missing or empty"

    source, target = norm_path(item["source"]), norm_path(item["target"])
    if source == target:
        return "source and target cannot be the same folder"
    if (target + os.sep).startswith(source + os.sep):
        return "target cannot be inside source (backups would be copied again and again)"

    retry = item.get("retry", {})
    if not isinstance(retry, dict):
        return "retry must be an object, e.g. {\"count\": 3, \"delay\": 60}"
    for field in ("count", "delay"):
        value = retry.get(field, DEFAULT_RETRY[field])
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return "invalid retry.%s: %r (must be 0 or a positive whole number)" % (field, value)

    schedule = item.get("schedule")
    if not isinstance(schedule, dict):
        return "schedule is missing"

    schedule_type = schedule.get("type")
    if schedule_type not in SCHEDULE_TYPES:
        return "invalid schedule type: %r (valid: %s)" % (schedule_type, ", ".join(SCHEDULE_TYPES))

    if schedule_type in ("daily", "weekly"):
        if not isinstance(schedule.get("time"), str) or not TIME_RE.match(schedule["time"]):
            return "invalid time: %r (must be HH:MM, e.g. 03:00)" % schedule.get("time")

    if schedule_type == "weekly":
        days = schedule.get("days")
        if not isinstance(days, list) or not days:
            return "days is missing or empty"
        bad_days = [day for day in days if day not in DAYS]
        if bad_days:
            return "invalid days: %s (valid: %s)" % (bad_days, ", ".join(DAYS))

    if schedule_type == "interval":
        minutes = schedule.get("minutes")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
            return "invalid minutes: %r (must be a positive whole number)" % minutes

    return None


def apply_settings(raw):
    """config'teki "settings" bölümünü doğrular ve uygular. Hata mesajlarını döndürür."""
    global history, min_free_bytes
    errors = []
    if not isinstance(raw, dict):
        errors.append("settings must be an object, using defaults")
        raw = {}

    new = dict(DEFAULT_SETTINGS)

    value = raw.get("log_dir", new["log_dir"])
    if isinstance(value, str) and value.strip():
        new["log_dir"] = value
    else:
        errors.append("invalid settings.log_dir: %r, using default" % value)

    rules = {
        "log_retention_days": (0, "0 or a positive whole number"),
        "history_max": (1, "a positive whole number"),
        "min_free_mb": (0, "0 or a positive whole number")
    }
    for name, (minimum, text) in rules.items():
        value = raw.get(name, new[name])
        if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
            new[name] = value
        else:
            errors.append("invalid settings.%s: %r (must be %s), using default" % (name, value, text))

    settings.update(new)
    min_free_bytes = settings["min_free_mb"] * 1024 * 1024
    if settings["history_max"] != history.maxlen:
        with running_lock:
            history = deque(history, maxlen=settings["history_max"])   # en yeni kayıtlar korunur
    try:
        apply_log_settings()
    except OSError as error:
        errors.append("log folder could not be opened: %s" % error)

    for error in errors:
        log.error("Config %s", error)
    return errors


def reload_config():
    """Config değiştiyse yeniden okur. Okunamazsa ya da bozuksa eski işlerle devam edilir."""
    global jobs, config_mtime, config_errors

    try:
        mtime = os.stat(CONFIG_PATH).st_mtime_ns
    except OSError as error:
        if config_mtime != "missing":
            log.error("Config file not found, keeping previous jobs: %s", error)
            config_errors = ["Config file not found: %s" % error]
            config_mtime = "missing"
        return

    if mtime == config_mtime:
        return
    config_mtime = mtime

    try:
        data = read_config()
    except (OSError, ValueError) as error:
        log.error("Config could not be read, keeping previous jobs: %s", error)
        config_errors = ["Config could not be read: %s" % error]
        return

    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        log.error("Config must contain a \"jobs\" list, keeping previous jobs")
        config_errors = ["Config must contain a \"jobs\" list"]
        return

    errors = apply_settings(data.get("settings", {}))

    valid_jobs = []
    keys = set()
    for number, item in enumerate(data["jobs"], start=1):
        error = validate_job(item)
        if error is None:
            key = (item["source"], item["target"])
            if key in keys:
                error = "duplicate job (same source and target)"
            keys.add(key)

        if error:
            log.error("Config job %d skipped: %s", number, error)
            errors.append("Job %d skipped: %s" % (number, error))
            continue
        valid_jobs.append(item)

    jobs = valid_jobs
    config_errors = errors
    log.info("Config loaded: %d valid job(s), %d skipped", len(valid_jobs), len(data["jobs"]) - len(valid_jobs))


def get_retry(job):
    # Config'te yazılmayan değerler için varsayılanı kullan
    return {**DEFAULT_RETRY, **job.get("retry", {})}


def calculate_hash(path):
    sha256 = hashlib.sha256()
    with open(lp(path), "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


def clear_readonly(path):
    """Windows'ta dosyanın salt okunur özelliğini kaldırır (dosya yoksa bir şey yapmaz)."""
    if os.name != "nt":
        return
    try:
        mode = os.stat(lp(path)).st_mode
    except FileNotFoundError:
        return
    if not mode & stat.S_IWRITE:
        os.chmod(lp(path), mode | stat.S_IWRITE)


def probe_target(folder):
    """Hedefte gerçekten deneme dosyası oluşturur, adını değiştirir ve siler.
    Servisin çalıştığı hesabın gerçek yazma iznini ölçer; hata varsa OSError fırlatır."""
    token = secrets.token_hex(4)
    first = os.path.join(folder, ".bksvc-probe-%s%s" % (token, TEMP_SUFFIX))
    second = os.path.join(folder, ".bksvc-probe-%s-b%s" % (token, TEMP_SUFFIX))
    current = None
    try:
        with open(lp(first), "wb") as f:
            f.write(b"x")
        current = first
        os.replace(lp(first), lp(second))
        current = second
        os.remove(lp(second))
        current = None
    finally:
        if current:
            try:
                os.remove(lp(current))
            except OSError:
                pass


def check_access(job):
    """İş için gereken izinler varsa None, yoksa sorunun açıklamasını döndürür."""
    # Kaynak: klasör var mı, listelenebiliyor mu, içindeki bir dosya okunabiliyor mu
    try:
        with os.scandir(lp(job["source"])) as entries:
            sample = next((e for e in entries if e.is_file() and not e.name.endswith(TEMP_SUFFIX)), None)
    except FileNotFoundError:
        return "source folder not found"
    except NotADirectoryError:
        return "source is not a folder"
    except OSError as error:
        return "source folder cannot be listed: %s" % error

    if sample is not None:
        try:
            with open(sample.path, "rb") as f:
                f.read(1)
        except OSError as error:
            # Hata 32: dosya başka bir programda açık; bu bir izin sorunu değil
            if getattr(error, "winerror", None) != 32:
                return "source files cannot be read (%s): %s" % (sample.name, error)

    # Hedef: klasör oluşturulabiliyor mu, içine yazılabiliyor mu
    try:
        os.makedirs(lp(job["target"]), exist_ok=True)
    except OSError as error:
        return "target folder cannot be created: %s" % error
    try:
        probe_target(job["target"])
    except OSError as error:
        return "no write permission on target: %s" % error

    return None


def disk_of(folder):
    """Klasörün bulunduğu diski ve boş alanı sorulacak yolu döndürür."""
    drive = os.path.splitdrive(os.path.abspath(folder))[0]
    if drive:
        return drive, drive + os.sep            # Windows: "C:" ya da "\\\\sunucu\\paylasim"
    return os.stat(folder).st_dev, folder       # Linux: diskin numarası


def reserve_space(folder, need):
    """Hedef diskte yeterli boş alan yoksa hata verir; varsa alanı bu dosya için ayırır."""
    disk, path = disk_of(folder)
    with space_lock:
        free = shutil.disk_usage(path).free - reserved.get(disk, 0)
        if free < need + min_free_bytes:
            raise IOError(
                "not enough free space on target: need %d MB + %d MB reserve, free %d MB"
                % (need // 1048576, min_free_bytes // 1048576, max(free, 0) // 1048576))
        reserved[disk] = reserved.get(disk, 0) + need
    return disk, need


def release_space(reservation):
    if reservation:
        with space_lock:
            reserved[reservation[0]] -= reservation[1]


def copy_one(path, source, target, info):
    if stopping.is_set():
        raise InterruptedError("Service is stopping")

    source_info = os.stat(lp(path))
    relative_path = os.path.relpath(path, source)
    target_path = os.path.join(target, relative_path)
    info["target"] = target_path
    info["size"] = source_info.st_size
    os.makedirs(lp(os.path.dirname(target_path)), exist_ok=True)

    # Önce geçici dosyaya yaz; doğrulanmadan hedefe dokunma
    # Rastgele kod: aynı dosyaya aynı anda yazan iki kopyalama birbirine karışmasın
    temp_path = target_path + "." + secrets.token_hex(4) + TEMP_SUFFIX

    reservation = None
    try:
        # Yer yoksa hiç başlama; varsa bu dosyanın boyutu kadar yeri ayır
        reservation = reserve_space(os.path.dirname(target_path), source_info.st_size)

        # Dosyayı parça parça kopyala, kaynağın hash'ini kopyalarken hesapla
        source_hash = hashlib.sha256()
        info["status"] = "copying"
        with open(lp(path), "rb") as src, open(lp(temp_path), "wb") as dst:
            while True:
                if stopping.is_set():
                    raise InterruptedError("Service is stopping")

                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break

                source_hash.update(chunk)
                dst.write(chunk)
                info["copied"] += len(chunk)   # status.json'daki ilerleme için

            # Bellekte bekleyen veriyi şimdi diske yazdır (elektrik kesintisine karşı)
            dst.flush()
            os.fsync(dst.fileno())

        # Sadece tarihleri kaynakla aynı yap (salt okunur gibi özellikler kopyalanmaz)
        os.utime(lp(temp_path), ns=(source_info.st_atime_ns, source_info.st_mtime_ns))

        info["status"] = "verifying"
        # Doğrulama: geçici dosyayı diskten tekrar oku, hash'ini kaynakla karşılaştır
        if source_hash.hexdigest() != calculate_hash(temp_path):
            raise IOError("Verification failed: " + target_path)

        # Kaynak kopyalama sırasında değiştiyse kopya karışık olabilir: hedefe koyma,
        # hata say; yeniden deneme kuralı dosyayı biraz sonra tekrar kopyalar
        after = os.stat(lp(path))
        if (after.st_size, after.st_mtime_ns) != (source_info.st_size, source_info.st_mtime_ns) \
                or after.st_size != info["copied"]:
            raise IOError("Source changed during copy, will be retried: " + path)

        # Doğrulandı: hedefteki eski kopya salt okunursa önce bu özelliği kaldır,
        # sonra geçici dosyayı tek adımda hedefin yerine koy
        clear_readonly(target_path)
        os.replace(lp(temp_path), lp(target_path))

    finally:
        release_space(reservation)
        # Bir hata olduysa yarım ya da bozuk geçici dosyayı sil
        if os.path.exists(lp(temp_path)):
            os.remove(lp(temp_path))

    log.info("Copied and verified: %s -> %s", path, target_path)


def list_files(source):
    paths = []
    for folder, _, files in os.walk(lp(source)):
        for name in files:
            if name.endswith(TEMP_SUFFIX):
                continue   # yarım kalmış geçici dosya: yedekleme
            # Önekli yolu loglarda ve hesaplarda normal haliyle kullan
            paths.append(unlp(os.path.join(folder, name)))
    return paths


def is_due(job, key, now):
    schedule = job["schedule"]
    last_run = last_runs.get(key)

    if schedule["type"] == "interval":
        if last_run is None:
            return True
        return now - last_run >= timedelta(minutes=schedule["minutes"])

    if schedule["type"] in ("daily", "weekly"):
        if schedule["type"] == "weekly" and DAYS[now.weekday()] not in schedule["days"]:
            return False
        if now.strftime("%H:%M") < schedule["time"]:
            return False
        return last_run is None or last_run.date() != now.date()

    return False


def start_job(pool, job, key, paths, retries_left):
    with running_lock:
        running[key] = {"pending": len(paths), "failed_paths": [], "retries_left": retries_left}

    # Her dosya ayrı bir görev: boştaki thread hangisiyse o alır
    for path in paths:
        pool.submit(run_file, path, job, key)


def run_file(path, job, key):
    global history_dirty
    akey = (key, path)
    info = {
        "source": path, "target": None, "size": None, "copied": 0,
        "status": "queued", "start": now_text(), "end": None, "error": None
    }
    with running_lock:
        active[akey] = info

    failed = False
    try:
        copy_one(path, job["source"], job["target"], info)
        info["status"] = "done"
    except InterruptedError:
        info["status"] = "cancelled"
        info["error"] = "Service is stopping"
        log.info("Copy cancelled: %s", path)
    except Exception as error:
        failed = True
        info["status"] = "error"
        info["error"] = str(error)
        log.exception("Copy failed: %s", path)
    finally:
        info["end"] = now_text()
        with running_lock:
            del active[akey]
            history.append(info)
            history_dirty = True
            if failed:
                last_errors[key] = info["error"]

        retry = None
        with running_lock:
            state = running[key]
            state["pending"] -= 1
            if failed:
                state["failed_paths"].append(path)
            done = state["pending"] == 0

            if done:
                del running[key]
                if not state["failed_paths"]:
                    last_errors.pop(key, None)   # iş tamamen başarılı: eski hatayı temizle
                # Hatalı dosya varsa ve hak kaldıysa sadece onları yeniden denemek üzere sıraya koy
                if state["failed_paths"] and state["retries_left"] > 0:
                    retry = {
                        "paths": state["failed_paths"],
                        "at": time.time() + get_retry(job)["delay"],
                        "left": state["retries_left"] - 1
                    }
                    retries[key] = retry
                else:
                    retries.pop(key, None)   # biten yeniden denemenin kaydını sil

        if done:
            save_state()   # yeni yeniden deneme (ya da silinen kayıt) servis yeniden başlasa da kaybolmasın
            failed_count = len(state["failed_paths"])
            if failed_count == 0:
                log.info("Job finished: %s -> %s (all files copied)", job["source"], job["target"])
            elif retry:
                total = get_retry(job)["count"]
                log.warning("Job finished with %d failed file(s): %s -> %s, retry %d/%d in %d s",
                            failed_count, job["source"], job["target"],
                            total - retry["left"], total, get_retry(job)["delay"])
            else:
                log.error("Job failed: %d file(s) could not be copied, no retries left: %s -> %s",
                          failed_count, job["source"], job["target"])


def write_status():
    """Servisin anlık durumunu status.json'a yazar (her saniye)."""
    with running_lock:
        job_list = []
        for job in jobs:
            key = (job["source"], job["target"])
            last_run = last_runs.get(key)
            entry = {
                "source": job["source"],
                "target": job["target"],
                "schedule": job["schedule"],
                "state": "idle",
                "pending": 0,
                "last_run": last_run.isoformat(timespec="seconds") if last_run else None,
                "retry_at": None,
                "retries_left": None,
                "last_error": last_errors.get(key)
            }
            if key in running:
                entry["state"] = "running"
                entry["pending"] = running[key]["pending"]
            elif key in access_problems:
                entry["state"] = "waiting_access"
                entry["last_error"] = access_problems[key]["error"]
            elif key in retries and not retries[key].get("started"):
                entry["state"] = "waiting_retry"
                entry["retry_at"] = datetime.fromtimestamp(retries[key]["at"]).isoformat(timespec="seconds")
                entry["retries_left"] = retries[key]["left"]
            job_list.append(entry)

        data = {
            "updated": now_text(),
            "config_errors": list(config_errors),
            "jobs": job_list,
            "active": [dict(info) for info in active.values()]
        }

    try:
        write_json(STATUS_PATH, data)
    except OSError:
        pass   # dosya o an başka bir program tarafından kilitliyse bir sonraki saniye yazılır


def load_history():
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as error:
        log.error("History file could not be read, starting without it: %s", error)
        return
    if isinstance(data, list):
        history.extend(data)


def write_history():
    """history değiştiyse history.json'a yazar."""
    global history_dirty
    with running_lock:
        if not history_dirty:
            return
        data = list(history)
        history_dirty = False

    try:
        write_json(HISTORY_PATH, data)
    except OSError:
        with running_lock:
            history_dirty = True   # yazılamadı, bir sonraki saniye tekrar denenir


stop_event = threading.Event()   # ana döngüyü uyandırmak için (Windows'taki win32event'in karşılığı)


def handle_stop(signum, frame):
    """systemd servisi durdururken SIGTERM gönderir; Ctrl+C ise SIGINT gönderir."""
    log.info("Service stopping")
    stopping.set()     # thread'ler bir sonraki 1 MB'ta dursun
    stop_event.set()   # ana döngü beklemeden uyansın


def run():
    """Servisin ana döngüsü (Windows'taki SvcDoRun'ın karşılığı)."""
    global last_api_check
    reload_config()   # önce ayarları oku: loglar doğru klasöre gitsin
    log.info("Service started")
    load_state()
    load_history()
    pool = ThreadPoolExecutor(WORKERS)

    try:
        while True:
            # Belirli aralıklarla API'ye sor, işler değiştiyse config.json'ı güncelle
            if time.time() - last_api_check >= API_CHECK_SECONDS:
                last_api_check = time.time()
                sync_from_api()

            reload_config()
            now = datetime.now()

            for job in jobs:
                key = (job["source"], job["target"])

                # Bu işin dosyaları hâlâ kopyalanıyorsa tekrar başlatma
                with running_lock:
                    if key in running:
                        continue

                try:
                    # Ne yapılacak: normal çalışma mı, yeniden deneme mi, hiçbiri mi?
                    action = None
                    if is_due(job, key, now):
                        action = "run"
                    else:
                        with running_lock:
                            retry = retries.get(key)
                            if retry and not retry.get("started") and time.time() >= retry["at"]:
                                action = "retry"
                    if action is None:
                        continue

                    # İzin kontrolü: sorun varsa iş bekler, ACCESS_RECHECK saniyede bir tekrar bakılır
                    problem = access_problems.get(key)
                    if problem and time.time() < problem["next_check"]:
                        continue

                    error = check_access(job)
                    if error:
                        if not problem or problem["error"] != error:
                            log.error("Access problem, job waiting: %s -> %s: %s",
                                      job["source"], job["target"], error)
                        with running_lock:
                            access_problems[key] = {"error": error, "next_check": time.time() + ACCESS_RECHECK}
                        continue

                    if problem:
                        log.info("Access OK again: %s -> %s", job["source"], job["target"])
                        with running_lock:
                            del access_problems[key]

                    if action == "run":
                        # Zamanı gelen normal çalışma bekleyen yeniden denemenin yerini alır
                        with running_lock:
                            retries.pop(key, None)
                            last_runs[key] = now
                        save_state()
                        log.info("Job started: %s -> %s", job["source"], job["target"])
                        paths = list_files(job["source"])
                        if not paths:
                            log.info("Job finished: %s -> %s (no files)", job["source"], job["target"])
                            continue
                        start_job(pool, job, key, paths, get_retry(job)["count"])

                    else:
                        # Sadece hatalı dosyaları tekrar kopyala. Kayıt, deneme bitene kadar
                        # state.json'da kalır; servis bu sırada yeniden başlarsa deneme tekrar yapılır
                        with running_lock:
                            retry = retries[key]
                            retry["started"] = True
                        log.info("Retrying %d file(s): %s -> %s",
                                 len(retry["paths"]), job["source"], job["target"])
                        start_job(pool, job, key, retry["paths"], retry["left"])

                except Exception:
                    log.exception("Job failed: %s -> %s", job["source"], job["target"])
                    continue

            write_status()
            write_history()

            # 1 saniye bekle; bu sırada durdurma sinyali gelirse hemen çık
            if stop_event.wait(1):
                break
    except Exception:
        log.exception("Service stopped because of an error")
        raise
    finally:
        # Çalışan thread'lere dur de, sırada bekleyenleri hiç başlatma, bitmelerini bekle
        stopping.set()
        pool.shutdown(wait=True, cancel_futures=True)
        write_status()
        write_history()

    log.info("Service stopped")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    run()