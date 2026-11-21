import hashlib
import importlib
import json
import logging
import os
import re
import secrets
import shutil
import ssl
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import signal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TARGETS_DIR = os.path.join(BASE_DIR, "targets")   # her hedef türü burada bir dosya: local.py, pcloud.py, gdrive.py ...
API_CA_PATH = os.path.join(BASE_DIR, "api_ca.crt")   # API'nin sertifikasını onaylayan CA (make_cert.py'nin ca.crt'si)

# targets/ ve hedeflerin kullandığı dosyalar (pcloud_storage.py, drive_storage.py) service.py'nin yanında;
# servis hangi klasörde çalışırsa çalışsın bulunabilsin
sys.path.insert(0, BASE_DIR)
STATE_PATH = os.path.join(BASE_DIR, "state.json")
STATUS_PATH = os.path.join(BASE_DIR, "status.json")
HISTORY_PATH = os.path.join(BASE_DIR, "history.json")
TEMP_SUFFIX = ".bksvc.part"   # servisin geçici dosyalarının uzantısı; taramada atlanır
WORKERS = 4
CHUNK_SIZE = 1024 * 1024   # 1 MB
DEFAULT_RETRY = {"count": 3, "delay": 60}   # 3 kez, 60 saniye arayla
ACCESS_RECHECK = 60   # kaynak/hedef izin sorunu varsa kaç saniyede bir tekrar bakılacak
API_URL = "https://192.168.89.1:8001"   # API başka bir bilgisayardaysa onun adresi yazılmalı
API_CHECK_SECONDS = 30                    # API'ye kaç saniyede bir sorulacak
STATUS_REPORT_SECONDS = 5                 # durum (sonuçlar ve ilerleme) ve yeni loglar API'ye kaç saniyede bir gönderilecek
PENDING_LOGS_MAX = 5000                   # API'ye gönderilmeyi bekleyen en fazla log satırı
# API'ye HTTPS ile bağlanırken sadece bizim CA'nın onayladığı sertifikaya güven
API_SSL = ssl.create_default_context(cafile=API_CA_PATH)
LOG_NAME_RE = re.compile(r"^log_(\d{4}-\d{2}-\d{2})\.jsonl$")

# config.json'daki "settings" bölümünde yazılmayan ayarlar için varsayılanlar
DEFAULT_SETTINGS = {
    "log_dir": "logs",            # log klasörü (göreli ise service.py'nin yanında)
    "log_retention_days": 30,     # kaç günlük log tutulacak (0 = hiç silme)
    "history_max": 1000,          # history.json'da tutulacak son biten dosya sayısı
    "min_free_mb": 100,           # hedef diskte her zaman boş bırakılacak alan
    "client_id": ""               # bu makinenin adı; API'den sadece bu makinenin işleri alınır
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


class ApiLogHandler(logging.Handler):
    """Log satırlarını, API'ye gönderilmek üzere hafızada biriktirir."""

    def __init__(self):
        super().__init__()
        self.pending = deque(maxlen=PENDING_LOGS_MAX)   # API'ye ulaşılamazsa en eskiler atılır

    def emit(self, record):
        try:
            self.pending.append(json.loads(self.format(record)))
        except Exception:
            self.handleError(record)


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
        if isinstance(old, DailyJsonHandler):   # API'ye giden log kuyruğuna dokunma
            log.removeHandler(old)
            old.close()
    log.addHandler(handler)
    log_settings = wanted


apply_log_settings()   # config okunana kadar varsayılan ayarlarla logla

# Log satırları dosyaya yazılırken bir kopyası da API'ye gönderilmek üzere biriktirilir
api_log_handler = ApiLogHandler()
api_log_handler.setFormatter(JsonFormatter())
log.addHandler(api_log_handler)

DAYS = ["pzt", "sal", "car", "per", "cum", "cmt", "paz"]
SCHEDULE_TYPES = ["daily", "weekly", "interval"]
MODES = ["copy", "move"]
COMPRESSIONS = ["none", "zip"]   # none: dosyalar olduğu gibi; zip: zip olmayan her dosya ayrı ayrı ziplenir   # copy: kaynak kalır; move: dosya hedefte doğrulandıktan sonra kaynaktan silinir
BACKUP_TYPES = ["full", "incremental"]   # full: her çalışmada bütün dosyalar; incremental: daha önce yedeklenmemiş (yeni/değişmiş) dosyalar
FOLDER_FORMAT = "%Y-%m-%d_%H-%M-%S"   # her çalışma hedefte bu adla bir klasöre yazılır, örn. 2026-11-23_02-00-00
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")   # HH:MM, örn. 03:00

jobs = []            # config'teki geçerli işler
config_mtime = None  # config'in en son okunduğu andaki değiştirilme zamanı
config_errors = []   # config'teki hatalı kayıtlar (status.json'da gösterilir)

last_runs = {}   # (source, target) -> işin son çalıştığı zaman
last_schedules = {}   # (source, target) -> iş son çalıştığında zamanlaması neydi

running = {}                      # (source, target) -> {"pending", "failed_paths", "retries_left"}
retries = {}                      # (source, target) -> {"paths", "at", "left"}: bekleyen yeniden denemeler
backed_up = {}                    # (source, target) -> {dosya yolu: [boyut, değişme zamanı, tarih klasörü, zip'li mi,
                                  #   kaynağın SHA-256'sı, hedefteki dosyanın hash'i (türü hedefe göre)]}: başarıyla yedeklenen dosyalar
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
last_status_report = 0            # durum API'ye en son ne zaman gönderildi
last_results = {}                 # (source, target) -> işin son çalışmasının sonucu
api_error = False                 # API'ye ulaşılamadığı zaten loglandı mı?


def sync_from_api():
    """API'deki işleri alır; config.json'dakilerden farklıysa config.json'ı günceller.
    config.json değişince reload_config() yeni işleri kendisi okur."""
    global api_error
    client_id = settings["client_id"]
    if not client_id:
        if not api_error:
            log.error("settings.client_id is not set, jobs are not taken from API")
            api_error = True
        return

    # Sadece bu makinenin işlerini iste: /jobs?client_id=muhasebe-pc
    url = API_URL + "/jobs?client_id=" + urllib.parse.quote(client_id)
    try:
        with urllib.request.urlopen(url, timeout=5, context=API_SSL) as response:
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
            if entry.get("schedule"):
                last_schedules[key] = entry["schedule"]
            if entry.get("files"):
                backed_up[key] = {path: list(value) for path, value in entry["files"].items()}

            retry = entry.get("retry")
            if retry:
                retries[key] = {
                    "paths": list(retry["paths"]),
                    "at": datetime.fromisoformat(retry["at"]).timestamp(),
                    "left": int(retry["left"]),
                    "folder": retry.get("folder")
                }
        except (TypeError, ValueError, KeyError, AttributeError):
            log.error("Invalid entry in state file skipped: %s", text_key)


def save_state():
    """Son çalışma zamanlarını ve bekleyen yeniden denemeleri state.json'a yazar."""
    with running_lock:
        data = {}
        for key in set(last_runs) | set(retries) | set(backed_up):
            last_run = last_runs.get(key)
            retry = retries.get(key)
            data[state_key(key)] = {
                "last_run": last_run.isoformat(timespec="seconds") if last_run else None,
                "schedule": last_schedules.get(key),
                "files": backed_up.get(key),
                "retry": {
                    "paths": list(retry["paths"]),
                    "at": datetime.fromtimestamp(retry["at"]).isoformat(timespec="seconds"),
                    "left": retry["left"],
                    "folder": retry.get("folder")
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

    # Hedef türü yazılmamışsa yerel klasör kabul edilir
    target_type = item.get("target_type", "local")
    if target_type not in available_target_types():
        return "invalid target_type: %r (available: %s)" % (target_type, ", ".join(available_target_types()))

    # İşlem yazılmamışsa kopyalama kabul edilir
    mode = item.get("mode", "copy")
    if mode not in MODES:
        return "invalid mode: %r (valid: %s)" % (mode, ", ".join(MODES))

    # Sıkıştırma yazılmamışsa dosyalar olduğu gibi aktarılır
    compression = item.get("compression", "none")
    if compression not in COMPRESSIONS:
        return "invalid compression: %r (valid: %s)" % (compression, ", ".join(COMPRESSIONS))

    # Yedek türü yazılmamışsa tam yedek (full) kabul edilir
    backup_type = item.get("backup_type", "full")
    if backup_type not in BACKUP_TYPES:
        return "invalid backup_type: %r (valid: %s)" % (backup_type, ", ".join(BACKUP_TYPES))

    # Hedefe özel kontroller hedefin kendi dosyasında (targets/<tür>.py içindeki validate)
    error = target_module(target_type).validate(item, helpers())
    if error:
        return error

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

    value = raw.get("client_id", new["client_id"])
    if isinstance(value, str):
        new["client_id"] = value.strip()
    else:
        errors.append("invalid settings.client_id: %r, must be text" % value)

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


def needs_zip(job, path):
    """İş zip'li ise ve dosya zaten .zip değilse True."""
    return job.get("compression", "none") == "zip" and not path.lower().endswith(".zip")


def zip_one(path, zip_path, info):
    """Dosyayı tek başına zip_path'e zipler; kaynağın SHA-256'sını döndürür."""
    source_hash = hashlib.sha256()
    with open(lp(path), "rb") as src, open(lp(zip_path), "wb") as raw:
        with ZipFile(raw, "w", ZIP_DEFLATED) as zf:
            # force_zip64: 4 GB'tan büyük dosyalar da ziplenebilsin
            with zf.open(os.path.basename(path), "w", force_zip64=True) as dst:
                while True:
                    if stopping.is_set():
                        raise InterruptedError("Service is stopping")
                    chunk = src.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    source_hash.update(chunk)
                    dst.write(chunk)
                    info["copied"] += len(chunk)   # ilerleme: kaynaktan okunan bayt
        # Bellekte bekleyen veriyi şimdi diske yazdır (elektrik kesintisine karşı)
        raw.flush()
        os.fsync(raw.fileno())
    return source_hash.hexdigest()


def zip_content_hash(zip_path):
    """Zip'in içindeki tek dosyayı açıp okur, SHA-256'sını döndürür (doğrulama için)."""
    sha256 = hashlib.sha256()
    with ZipFile(lp(zip_path)) as zf, zf.open(zf.namelist()[0]) as f:
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

    # Hedef hazır mı (klasör yazılabilir mi, token var mı ...): hedefin kendi dosyasında
    return get_target(job).check()


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


def available_target_types():
    """targets/ klasöründeki hedef türleri: local.py -> "local", pcloud.py -> "pcloud" ..."""
    try:
        names = os.listdir(TARGETS_DIR)
    except OSError:
        return []
    return sorted(name[:-3] for name in names if name.endswith(".py") and not name.startswith("_"))


def target_module(target_type):
    """Hedef türünün dosyasını yükler: "pcloud" -> targets/pcloud.py"""
    return importlib.import_module("targets." + target_type)


_helpers = None


def helpers():
    """Hedef dosyalarının kullandığı ortak yardımcılar (hedef dosyaları servisin geri kalanını bilmez)."""
    global _helpers
    if _helpers is None:
        _helpers = SimpleNamespace(
            BASE_DIR=BASE_DIR, CHUNK_SIZE=CHUNK_SIZE, TEMP_SUFFIX=TEMP_SUFFIX, stopping=stopping,
            lp=lp, norm_path=norm_path, calculate_hash=calculate_hash, clear_readonly=clear_readonly,
            reserve_space=reserve_space, release_space=release_space, list_files=list_files)
    return _helpers


def get_target(job):
    """İşin hedefi: config'teki target_type'a göre targets/ klasöründeki dosyadan."""
    return target_module(job.get("target_type", "local")).Target(job, helpers())


def backup_rel_path(job, path, folder, zipped):
    """Dosyanın hedefin içindeki yolu: "<tarih klasörü>/alt/a.txt" (zip'li saklandıysa a.txt.zip)."""
    rel = folder + "/" + os.path.relpath(path, job["source"]).replace(os.sep, "/")
    return rel + ".zip" if zipped else rel


def already_backed_up(job, key, path):
    """Incremental: bu dosyanın tam bu hali (aynı boyut, aynı değişme zamanı) daha önce yedeklendi mi?
    Yedeklendiyse kaydını [boyut, zaman, klasör, zip'li mi, kaynak SHA-256, hedefteki hash], yedeklenmediyse None döndürür."""
    record = backed_up.get(key, {}).get(path)
    if not record or len(record) < 6 or not record[4] or not record[5]:
        return None   # kayıt yok (ya da bu sürümden önceki eski biçimde)
    try:
        source_info = os.stat(lp(path))
    except OSError:
        return None   # dosya bu arada silinmiş: kopyalamaya çalışılınca hata olarak görünür
    if record[:2] != [source_info.st_size, source_info.st_mtime_ns]:
        return None   # dosya o yedekten sonra değişmiş
    # Hedefte o kopya hâlâ duruyor mu? (elle silindiyse tekrar yedeklenir)
    if not get_target(job).exists(backup_rel_path(job, path, record[2], record[3])):
        return None
    return record


def verify_backup(job, target, path, record):
    """Taşı + incremental: kaynağı kopyalamadan silmeden önce iki kontrol:
    1) hedefteki kopya bizim koyduğumuz dosya mı (hash'i kayıttakiyle aynı mı)
    2) kaynak, o zaman yedeklenen içerikle birebir aynı mı"""
    folder, zipped, source_sha256, stored_hash = record[2:6]
    if not target.verify(backup_rel_path(job, path, folder, zipped), stored_hash):
        return False
    return calculate_hash(path) == source_sha256


def save_file(job, target, path, folder, info, source_info):
    """Bir dosyayı (gerekirse zip'leyerek) hedefe koyar; bütün hedef türleri için aynı akış.
    Kayıt için (zip'li mi, kaynağın SHA-256'sı, hedefteki dosyanın hash'i) döndürür."""
    zipped = needs_zip(job, path)
    rel = backup_rel_path(job, path, folder, zipped)
    info["target"] = target.display(rel)

    upload_path = path
    temp_zip = None
    try:
        if zipped:
            # Önce bu bilgisayarın geçici klasöründe zip'le ve zip'i doğrula; hedefe zip konur
            temp_zip = os.path.join(tempfile.gettempdir(), "bksvc-" + secrets.token_hex(4) + ".zip" + TEMP_SUFFIX)
            info["status"] = "zipping"
            source_sha256 = zip_one(path, temp_zip, info)
            if source_sha256 != zip_content_hash(temp_zip):
                raise IOError("Zip verification failed: " + path)
            os.utime(lp(temp_zip), ns=(source_info.st_atime_ns, source_info.st_mtime_ns))   # zip'in tarihi = kaynağın tarihi
            upload_path = temp_zip

        info["copied"] = 0
        file_sha256, stored_hash = target.save(upload_path, rel, info)
        if not zipped:
            source_sha256 = file_sha256
    finally:
        # Geçici zip'i her durumda sil
        if temp_zip and os.path.exists(lp(temp_zip)):
            os.remove(lp(temp_zip))

    # Kaynak yedekleme sırasında değiştiyse kopya karışık olabilir: hata say, yeniden deneme kuralı tekrar dener
    after = os.stat(lp(path))
    if (after.st_size, after.st_mtime_ns) != (source_info.st_size, source_info.st_mtime_ns):
        raise IOError("Source changed during backup, will be retried: " + path)

    log.info("%s and verified: %s -> %s", "Zipped" if zipped else "Saved", path, info["target"])
    return zipped, source_sha256, stored_hash


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

    # Zamanlama, iş son çalıştığından beri değiştiyse iş yeni bir iş gibi sayılır
    if key in last_schedules and last_schedules[key] != schedule:
        last_run = None

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


def start_job(pool, job, key, paths, retries_left, folder, verify=None):
    """folder: bu çalışmanın tarih klasörü. verify: Taşı + incremental'da önce doğrulanacak dosyalar {dosya: kaydı}."""
    verify = verify or {}
    with running_lock:
        running[key] = {"pending": len(paths), "total": len(paths), "failed_paths": [], "retries_left": retries_left,
                        "folder": folder}

    # Her dosya ayrı bir görev: boştaki thread hangisiyse o alır
    for path in paths:
        pool.submit(run_file, path, job, key, folder, verify.get(path))


def run_file(path, job, key, folder, verify_record=None):
    global history_dirty
    akey = (key, path)
    info = {
        "source": path, "target": None, "size": None, "copied": 0,
        "status": "queued", "start": now_text(), "end": None, "error": None,
        "job_source": job["source"], "job_target": job["target"]   # panel dosyayı işiyle eşleştirsin
    }
    with running_lock:
        active[akey] = info

    failed = False
    try:
        source_info = os.stat(lp(path))   # incremental kaydı için: kopyalamadan önceki boyut ve değişme zamanı
        info["size"] = source_info.st_size
        target = get_target(job)
        if verify_record and verify_backup(job, target, path, verify_record):
            # Taşı + incremental: önceki bir yedekte birebir aynısı var, kopyalamaya gerek yok; aşağıda kaynaktan silinir
            saved_folder, zipped, source_sha256, stored_hash = verify_record[2:6]
            info["copied"] = source_info.st_size
            info["target"] = target.display(backup_rel_path(job, path, saved_folder, zipped))
            log.info("Already backed up in %s, verified (not copied): %s", saved_folder, path)
        else:
            saved_folder = folder
            zipped, source_sha256, stored_hash = save_file(job, target, path, folder, info, source_info)

        # Dosyayı "yedeklendi" diye kaydet (boyut + değişme zamanı + tarih klasörü). Incremental bu kayda bakar;
        # taşınan dosya kaynağa geri konursa da tanınır
        with running_lock:
            backed_up.setdefault(key, {})[path] = [source_info.st_size, source_info.st_mtime_ns, saved_folder,
                                                   zipped, source_sha256, stored_hash]

        # Taşıma: dosya hedefte doğrulandı, şimdi kaynaktan sil
        if job.get("mode", "copy") == "move":
            clear_readonly(path)   # salt okunur dosya Windows'ta silinemez
            os.remove(lp(path))
            log.info("Source deleted (move): %s", path)
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
                # Panelde gösterilecek sonuç: ne zaman bitti, kaç dosyadan kaçı hatalı
                last_results[key] = {
                    "finished": now_text(),
                    "files": state["total"],
                    "failed": len(state["failed_paths"])
                }
                if not state["failed_paths"]:
                    last_errors.pop(key, None)   # iş tamamen başarılı: eski hatayı temizle
                # Hatalı dosya varsa ve hak kaldıysa sadece onları yeniden denemek üzere sıraya koy
                if state["failed_paths"] and state["retries_left"] > 0:
                    retry = {
                        "paths": state["failed_paths"],
                        "at": time.time() + get_retry(job)["delay"],
                        "left": state["retries_left"] - 1,
                        "folder": state["folder"]   # hatalı dosyalar aynı tarih klasörüne tekrar denenir
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


def build_status():
    """Servisin anlık durumunu hazırlar: status.json'a da API'ye de bu gider."""
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
                "last_error": last_errors.get(key),
                "total": 0,
                "last_result": last_results.get(key)
            }
            if key in running:
                entry["state"] = "running"
                entry["pending"] = running[key]["pending"]
                entry["total"] = running[key]["total"]
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
    return data


def write_status(data):
    """Durumu status.json'a yazar (her saniye)."""
    try:
        write_json(STATUS_PATH, data)
    except OSError:
        pass   # dosya o an başka bir program tarafından kilitliyse bir sonraki saniye yazılır


def report_logs():
    """Biriken yeni log satırlarını API'ye gönderir; gönderilenler kuyruktan silinir."""
    client_id = settings["client_id"]
    pending = api_log_handler.pending
    if not client_id or not pending:
        return
    entries = list(pending)
    url = API_URL + "/logs?client_id=" + urllib.parse.quote(client_id)
    request = urllib.request.Request(
        url,
        data=json.dumps(entries, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=3, context=API_SSL):
            pass
    except (OSError, ValueError):
        return   # gönderilemedi: satırlar kuyrukta kalır, bir sonraki seferde tekrar denenir
    for _ in range(len(entries)):
        pending.popleft()


def api_post(path, data, content_type="application/json"):
    """API'ye POST gönderir. data: bayt ya da açık dosya (dosya parça parça gönderilir)."""
    request = urllib.request.Request(API_URL + path, data=data, method="POST",
                                     headers={"Content-Type": content_type})
    if hasattr(data, "read"):
        request.add_header("Content-Length", str(os.fstat(data.fileno()).st_size))
    with urllib.request.urlopen(request, timeout=60, context=API_SSL):
        pass


def handle_restores():
    """API'deki bekleyen geri yükleme isteklerini yerine getirir (bütün hedef türleri için aynı)."""
    client_id = settings["client_id"]
    if not client_id:
        return
    try:
        url = API_URL + "/restore/pending?client_id=" + urllib.parse.quote(client_id)
        with urllib.request.urlopen(url, timeout=5, context=API_SSL) as response:
            requests_ = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return   # API'ye ulaşılamadı: bir sonraki turda tekrar sorulur

    for r in requests_:
        base = "/restore/requests/%d" % r["id"]
        try:
            # Hedef türü ne olursa olsun aynı akış: listeyi ya da dosyayı hedeften al, API'ye gönder
            target = get_target({"target": r["target"], "target_type": r.get("target_type") or "local"})
            if r["kind"] == "list":
                files = target.list()
                api_post(base + "/list", json.dumps(files, ensure_ascii=False).encode("utf-8"))
                log.info("Restore: file list sent: %s (%d files)", r["target"], len(files))
            else:
                # Dosyayı geçici dosyaya al, API'ye gönder, geçici dosyayı sil
                temp = os.path.join(tempfile.gettempdir(), "bksvc-restore-" + secrets.token_hex(4) + TEMP_SUFFIX)
                try:
                    target.download(r["path"], temp)
                    with open(lp(temp), "rb") as f:
                        api_post(base + "/file", f, "application/octet-stream")
                finally:
                    if os.path.exists(lp(temp)):
                        os.remove(lp(temp))
                log.info("Restore: file sent: %s", target.display(r["path"]))
        except Exception as error:
            log.exception("Restore request %d failed", r["id"])
            try:
                api_post(base + "/error", json.dumps({"error": str(error)}).encode("utf-8"))
            except OSError:
                pass


def restore_loop():
    """Ayrı thread: 5 saniyede bir geri yükleme isteklerine bakar (yedeklemeyi bekletmesin diye)."""
    while not stopping.wait(STATUS_REPORT_SECONDS):
        handle_restores()


def report_status(data):
    """Durumu API'ye gönderir; panel buradan okur."""
    client_id = settings["client_id"]
    if not client_id:
        return
    url = API_URL + "/status?client_id=" + urllib.parse.quote(client_id)
    request = urllib.request.Request(
        url,
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=3, context=API_SSL):
            pass
    except (OSError, ValueError):
        pass   # API'ye ulaşılamıyorsa bu zaten sync_from_api() tarafından loglanıyor


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
    global last_api_check, last_status_report
    reload_config()   # önce ayarları oku: loglar doğru klasöre gitsin
    log.info("Service started")
    load_state()
    load_history()
    pool = ThreadPoolExecutor(WORKERS)
    threading.Thread(target=restore_loop, daemon=True).start()   # geri yükleme istekleri

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
                            last_schedules[key] = job["schedule"]
                        save_state()
                        log.info("Job started (%s): %s -> %s", job.get("backup_type", "full"),
                                 job["source"], job["target"])
                        folder = now.strftime(FOLDER_FORMAT)   # bu çalışmanın hedefteki tarih klasörü
                        paths = list_files(job["source"])
                        verify = {}
                        if job.get("backup_type", "full") == "incremental":
                            # Her dosya için tek soru: tam bu hali daha önce yedeklendi mi? (kayıttan; yedeklendiyse hangi klasörde)
                            done = {}
                            for p in paths:
                                record = already_backed_up(job, key, p)
                                if record:
                                    done[p] = record
                            if job.get("mode", "copy") == "move":
                                # Taşı: yedeklenmiş olanlar kopyalanmaz; o klasördeki kopya doğrulanıp kaynaktan silinir
                                verify = done
                                log.info("Incremental: %d of %d file(s) already backed up (will be verified, not copied)",
                                         len(done), len(paths))
                            else:
                                # Kopyala: yedeklenmiş olanlar atlanır
                                log.info("Incremental: %d of %d file(s) are new or changed", len(paths) - len(done), len(paths))
                                paths = [p for p in paths if p not in done]
                        if not paths:
                            log.info("Job finished: %s -> %s (no files to copy, no folder created)", job["source"], job["target"])
                            continue
                        if len(paths) > len(verify):   # klasör sadece en az bir dosya kopyalanacaksa oluşur
                            log.info("Backup folder for this run: %s", folder)
                        start_job(pool, job, key, paths, get_retry(job)["count"], folder, verify)

                    else:
                        # Sadece hatalı dosyaları tekrar kopyala. Kayıt, deneme bitene kadar
                        # state.json'da kalır; servis bu sırada yeniden başlarsa deneme tekrar yapılır
                        with running_lock:
                            retry = retries[key]
                            retry["started"] = True
                        log.info("Retrying %d file(s): %s -> %s",
                                 len(retry["paths"]), job["source"], job["target"])
                        start_job(pool, job, key, retry["paths"], retry["left"],
                                  retry.get("folder") or now.strftime(FOLDER_FORMAT))

                except Exception:
                    log.exception("Job failed: %s -> %s", job["source"], job["target"])
                    continue

            status = build_status()
            write_status(status)
            # Belirli aralıklarla durumu API'ye gönder (panelde sonuçlar ve ilerleme)
            if time.time() - last_status_report >= STATUS_REPORT_SECONDS:
                last_status_report = time.time()
                report_status(status)
                report_logs()
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
        write_status(build_status())
        write_history()

    log.info("Service stopped")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    run()