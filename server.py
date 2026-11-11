import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
import hashlib
import mimetypes
import io
import zipfile
import concurrent.futures
import gzip
import base64
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import requests
from flask import (
    Flask, jsonify, request, send_from_directory, make_response, Response
)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder='.')

# ---------------------------------------------------------------------------
# Αρχεία δεδομένων στον δίσκο
# ---------------------------------------------------------------------------
CHANNELS_FILE = 'channels.json'
PLAYLISTS_FILE = 'playlists.json'
CUSTOM_CHANNELS_FILE = 'custom_channels.json'
RECORDINGS_DIR_DEFAULT = 'recordings'


def get_recordings_dir():
  """Ο φάκελος εγγραφών είναι πλέον ρυθμιζόμενος (Ρυθμίσεις -> Φάκελος Εγγραφών), όχι
  σταθερός. Διαβάζεται ζωντανά κάθε φορά (όχι μια φορά στο import) ώστε μια αλλαγή
  από τις Ρυθμίσεις να ισχύσει αμέσως, χωρίς restart."""
  d = get_setting('recordings_dir')
  return d if d else RECORDINGS_DIR_DEFAULT
RECORDINGS_STATE_FILE = 'recordings.json'
SETTINGS_FILE = 'settings.json'
EPG_FILE = 'epg.xml'
EPG_OVERRIDES_FILE = 'epg_overrides.json'
CATEGORY_OVERRIDES_FILE = 'category_overrides.json'
LOGOS_CACHE_DIR = 'logos_cache'
os.makedirs(LOGOS_CACHE_DIR, exist_ok=True)

# Session με μηδενικά retries/μικρό timeout για τα λογότυπα: αν το δίκτυο μπλοκάρει
# ένα domain (π.χ. SSLError/ConnectionError σε κάθε προσπάθεια), δεν θέλουμε η
# προεπιλεγμένη συμπεριφορά της requests να ξαναδοκιμάζει πολλές φορές το ίδιο ήδη
# αποτυχημένο URL — αυτό απλώς καθυστερεί πολύ τη φόρτωση της λίστας καναλιών.
_logo_session = requests.Session()
_logo_adapter = requests.adapters.HTTPAdapter(max_retries=0)
_logo_session.mount('http://', _logo_adapter)
_logo_session.mount('https://', _logo_adapter)

# Μικρό in-memory cache αποτυχιών: αν ένα λογότυπο απέτυχε πρόσφατα (π.χ. το domain
# είναι μπλοκαρισμένο στο δίκτυο), μην το ξαναπροσπαθήσεις σε κάθε φόρτωση σελίδας —
# απάντησε κατευθείαν 404 για λίγη ώρα.
_logo_failure_cache = {}
_logo_failure_lock = threading.Lock()
LOGO_FAILURE_TTL_SEC = 600  # 10 λεπτά

CHANNEL_HEALTH = {}  # url -> {'ok': bool, 'checked_at': epoch, 'error': str|None}
CHANNEL_HEALTH_LOCK = threading.Lock()
CHANNEL_HEALTH_SCAN = {'active': False, 'total': 0, 'done': 0, 'started_at': None}


def _check_one_channel_url(url):
  try:
    resp = _logo_session.get(
        url, timeout=6, stream=True,
        headers={'User-Agent': 'Mozilla/5.0 (compatible; IPTVPlayerPro-HealthCheck)'},
    )
    ok = resp.status_code < 400
    resp.close()
    return ok, (None if ok else f'HTTP {resp.status_code}')
  except requests.RequestException as e:
    return False, type(e).__name__


def _run_channel_health_scan(urls):
  with CHANNEL_HEALTH_LOCK:
    CHANNEL_HEALTH_SCAN.update({'active': True, 'total': len(urls), 'done': 0, 'started_at': time.time()})
  try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
      futures = {executor.submit(_check_one_channel_url, u): u for u in urls}
      for fut in concurrent.futures.as_completed(futures):
        u = futures[fut]
        try:
          ok, err = fut.result()
        except Exception as e:
          ok, err = False, str(e)
        with CHANNEL_HEALTH_LOCK:
          CHANNEL_HEALTH[u] = {'ok': ok, 'checked_at': time.time(), 'error': err}
          CHANNEL_HEALTH_SCAN['done'] += 1
  finally:
    with CHANNEL_HEALTH_LOCK:
      CHANNEL_HEALTH_SCAN['active'] = False

SERVER_START_TIME = time.time()

DEFAULT_SETTINGS = {
    'auth_enabled': True,
    'auth_user': 'admin',
    'auth_pass_hash': None,       # γεμίζει αυτόματα στο πρώτο ξεκίνημα
    'protect_all': False,         # αν True, ζητά auth ΠΑΝΤΟΥ (και για προβολή καναλιών/EPG)
    'timezone': 'Europe/Athens',
    'epg_url': 'https://ext.greektv.app/epg/epg.xml',
    'epg_extra_urls': [],          # συμπληρωματικά XMLTV sources (π.χ. Pluto TV/Plex FAST κανάλια), συγχωνεύονται με το κύριο
    'epg_refresh_hours': 6,
    'epg_retry_cooldown_sec': 30,
    'rec_pre_pad_min': 10,
    'rec_post_pad_min': 25,
    'max_duration_hours': 6,
    'cleanup_days': 0,             # 0 = απενεργοποιημένο auto-cleanup
    'recordings_dir': 'recordings',  # φάκελος αποθήκευσης εγγραφών (μπορεί να αλλάξει από Ρυθμίσεις)
    'parental_pin_hash': None,     # PIN για κλείδωμα κατηγοριών (π.χ. KIDS) — hashed
    'locked_categories': [],       # λίστα ονομάτων κατηγοριών που ζητούν PIN
}

settings_lock = threading.Lock()
playlists_lock = threading.Lock()
recordings_lock = threading.Lock()

recordings = {}  # rec_id -> dict (βλ. schedule_rec για τα πεδία)

_epg_state_lock = threading.Lock()
_epg_state = {'last_success': None, 'last_attempt': 0.0, 'last_error': None}


# ---------------------------------------------------------------------------
# Γενικά json helpers
# ---------------------------------------------------------------------------

def _load_json_file(path, default):
  if not os.path.exists(path):
    return default
  try:
    with open(path, 'r', encoding='utf-8') as f:
      return json.load(f)
  except Exception as e:
    print(f'[ERROR] Αποτυχία ανάγνωσης {path}:', e)
    return default


def _save_json_file(path, data):
  with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Settings — ώστε οι ρυθμίσεις να αλλάζουν από το UI, όχι από τον κώδικα
# ---------------------------------------------------------------------------

def load_settings():
  with settings_lock:
    data = _load_json_file(SETTINGS_FILE, None)
    if data is None:
      data = dict(DEFAULT_SETTINGS)
      # Bootstrap από env vars αν υπάρχουν (backward-compat) αλλιώς auto-generate.
      env_user = os.environ.get('IPTV_AUTH_USER')
      env_pass = os.environ.get('IPTV_AUTH_PASS')
      if env_user and env_pass:
        data['auth_user'] = env_user
        data['auth_pass_hash'] = generate_password_hash(env_pass)
        print(f"[INFO] Auth bootstrap από environment variables (user='{env_user}').")
      else:
        generated_pass = secrets.token_urlsafe(9)
        data['auth_pass_hash'] = generate_password_hash(generated_pass)
        print('=' * 70)
        print('[SETUP] Δεν βρέθηκαν στοιχεία σύνδεσης — δημιουργήθηκαν αυτόματα:')
        print(f"         Χρήστης : {data['auth_user']}")
        print(f"         Κωδικός : {generated_pass}")
        print('         Άλλαξέ τα ελεύθερα από το ⚙ Ρυθμίσεις μέσα στην εφαρμογή.')
        print('=' * 70)
      _save_json_file(SETTINGS_FILE, data)
    else:
      # Συμπλήρωσε τυχόν νέα πεδία που προστέθηκαν σε νεότερη έκδοση.
      changed = False
      for k, v in DEFAULT_SETTINGS.items():
        if k not in data:
          data[k] = v
          changed = True
      if changed:
        _save_json_file(SETTINGS_FILE, data)
    return data


def save_settings(data):
  with settings_lock:
    _save_json_file(SETTINGS_FILE, data)


SETTINGS = load_settings()


def get_setting(key):
  with settings_lock:
    return SETTINGS.get(key, DEFAULT_SETTINGS.get(key))


def get_timezone():
  try:
    return ZoneInfo(get_setting('timezone') or 'Europe/Athens')
  except Exception:
    return ZoneInfo('Europe/Athens')


def now_local():
  return datetime.now(get_timezone())


def parse_local_datetime(date_str):
  """Παίρνει 'YYYY-MM-DD HH:MM' εννοώντας ώρα στη ρυθμισμένη ζώνη ώρας (π.χ. Ελλάδας),
  ανεξάρτητα από τη ζώνη ώρας του λειτουργικού συστήματος του server."""
  naive = datetime.strptime(date_str, '%Y-%m-%d %H:%M')
  return naive.replace(tzinfo=get_timezone())


def parse_iso_aware(iso_str):
  """Διαβάζει ένα αποθηκευμένο ISO timestamp· αν είναι naive (παλιά εγγραφή από πριν
  το timezone-aware fix), του κολλάει τη ρυθμισμένη ζώνη ώρας για συμβατότητα."""
  dt = datetime.fromisoformat(iso_str)
  if dt.tzinfo is None:
    dt = dt.replace(tzinfo=get_timezone())
  return dt


# ---------------------------------------------------------------------------
# Authentication (HTTP Basic) — ενεργό by default, με auto-generated credentials
# ---------------------------------------------------------------------------

def check_auth(username, password):
  with settings_lock:
    expected_user = SETTINGS.get('auth_user')
    pass_hash = SETTINGS.get('auth_pass_hash')
  if not pass_hash:
    return False
  return username == expected_user and check_password_hash(pass_hash, password)


def requires_auth(f):
  @wraps(f)
  def decorated(*args, **kwargs):
    if not get_setting('auth_enabled'):
      return f(*args, **kwargs)
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
      return Response(
          'Απαιτείται πιστοποίηση.', 401,
          {'WWW-Authenticate': 'Basic realm="IPTV Server"'},
      )
    return f(*args, **kwargs)
  return decorated


@app.before_request
def _maybe_protect_everything():
  """Αν protect_all=True, ζήτα auth για ΟΛΑ τα endpoints, όχι μόνο τα ευαίσθητα."""
  if not get_setting('auth_enabled') or not get_setting('protect_all'):
    return None
  auth = request.authorization
  if not auth or not check_auth(auth.username, auth.password):
    return Response(
        'Απαιτείται πιστοποίηση.', 401,
        {'WWW-Authenticate': 'Basic realm="IPTV Server"'},
    )
  return None


# ---------------------------------------------------------------------------
# EPG
# ---------------------------------------------------------------------------

def _fetch_epg_source_bytes(url, timeout=60):
  """Κατεβάζει ένα XMLTV source (πρωτεύον ή συμπληρωματικό). Χειρίζεται διαφανώς
  gzip-συμπιεσμένα αρχεία (π.χ. τα .xml.gz του i.mjh.nz για Pluto TV/Plex), είτε
  το gzip φαίνεται από το URL/headers είτε όχι — ελέγχουμε τα magic bytes."""
  headers = {
      'User-Agent': (
          'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
      )
  }
  r = requests.get(url, headers=headers, timeout=timeout)
  if r.status_code != 200:
    raise ValueError(f'HTTP {r.status_code}')
  content = r.content
  if content[:2] == b'\x1f\x8b':  # gzip magic bytes
    content = gzip.decompress(content)
  return content


def update_epg():
  global _epg_state
  print('[INFO] Κατέβασμα EPG XML...')
  epg_url = get_setting('epg_url')
  extra_urls = get_setting('epg_extra_urls') or []
  try:
    content = _fetch_epg_source_bytes(epg_url)
    # Sanity check: μην αντικαταστήσεις ένα ΚΑΛΟ υπάρχον αρχείο με κάτι άδειο/κατεστραμμένο.
    if len(content) < 200 or (b'<tv' not in content[:2048] and b'<?xml' not in content[:200]):
      raise ValueError('Το περιεχόμενο δεν μοιάζει με έγκυρο XMLTV (πολύ μικρό ή λάθος μορφή).')

    merge_errors = []
    if extra_urls:
      # Συγχώνευση συμπληρωματικών πηγών (π.χ. Pluto TV, Plex — FAST κανάλια που
      # δεν καλύπτονται από το κύριο epg_url). Ένα σφάλμα σε ΜΙΑ συμπληρωματική
      # πηγή δεν πρέπει να ρίξει όλη την ενημέρωση — απλά παραλείπεται.
      try:
        root = ET.fromstring(content)
      except ET.ParseError as e:
        raise ValueError(f'Το κύριο epg_url δεν είναι έγκυρο XML: {e}')

      for extra_url in extra_urls:
        extra_url = (extra_url or '').strip()
        if not extra_url:
          continue
        try:
          extra_content = _fetch_epg_source_bytes(extra_url, timeout=30)
          extra_root = ET.fromstring(extra_content)
          added = 0
          for child in extra_root:
            if child.tag in ('channel', 'programme'):
              root.append(child)
              added += 1
          print(f'[INFO] Συγχωνεύτηκε συμπληρωματικό EPG source ({extra_url}): +{added} στοιχεία.')
        except Exception as e:
          merge_errors.append(f'{extra_url}: {e}')
          print(f'[WARN] Παράλειψη συμπληρωματικού EPG source {extra_url}: {e}')

      content = ET.tostring(root, encoding='utf-8', xml_declaration=True)

    with open(EPG_FILE, 'wb') as f:
      f.write(content)
    print('[SUCCESS] Το EPG ενημερώθηκε επιτυχώς!')
    with _epg_state_lock:
      _epg_state['last_success'] = now_local().isoformat()
      _epg_state['last_error'] = ('Μερικά σφάλματα σε συμπληρωματικά sources: ' + '; '.join(merge_errors)) if merge_errors else None
    return True
  except Exception as e:
    print('[ERROR] Σφάλμα EPG:', e)
    with _epg_state_lock:
      _epg_state['last_error'] = str(e)
    return False


def epg_refresh_loop():
  """Ξανακατεβάζει περιοδικά το EPG σε background thread.

  Ελέγχει τη ρύθμιση epg_refresh_hours κάθε λίγα δευτερόλεπτα (αντί να κάνει ένα
  μονολιθικό sleep() για ολόκληρο το διάστημα) ώστε μια αλλαγή της ρύθμισης από
  το UI να πιάνει τόπο σύντομα, και όχι μόνο όταν ξυπνήσει το επόμενο, πιθανώς
  πολύωρο, sleep."""
  last_refresh = time.time()
  while True:
    time.sleep(60)
    hours = get_setting('epg_refresh_hours') or 6
    if time.time() - last_refresh >= max(1, hours) * 3600:
      with _epg_state_lock:
        _epg_state['last_attempt'] = time.time()
      update_epg()
      last_refresh = time.time()


def cleanup_loop():
  """Διαγράφει παλιά ολοκληρωμένα recordings αν είναι ενεργό το cleanup_days."""
  first_run = True
  while True:
    if first_run:
      # Έλεγξε αμέσως στο ξεκίνημα αντί να περιμένεις μια ολόκληρη ώρα πριν τον
      # πρώτο έλεγχο — σημαντικό αν το cleanup_days ενεργοποιήθηκε ενώ υπήρχαν
      # ήδη παλιά αρχεία στο δίσκο.
      first_run = False
    else:
      time.sleep(3600)  # έλεγχος κάθε ώρα
    days = get_setting('cleanup_days') or 0
    if days <= 0:
      continue
    cutoff = time.time() - days * 86400
    if not os.path.isdir(get_recordings_dir()):
      continue
    removed = 0
    for fname in os.listdir(get_recordings_dir()):
      fpath = os.path.join(get_recordings_dir(), fname)
      try:
        if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
          os.remove(fpath)
          removed += 1
          with recordings_lock:
            for rec in recordings.values():
              if rec.get('filename') == fname:
                rec['filename'] = None
                rec['cleaned_up'] = True
      except Exception as e:
        print(f'[ERROR] Αποτυχία διαγραφής παλιού αρχείου {fname}:', e)
    if removed:
      print(f'[INFO] Auto-cleanup: διαγράφηκαν {removed} παλιά αρχεία εγγραφών.')
      save_recordings_state()


# ---------------------------------------------------------------------------
# Recordings: persistence
# ---------------------------------------------------------------------------

def save_recordings_state():
  with recordings_lock:
    serializable = {
        rid: {k: v for k, v in rec.items() if k not in ('timer', 'process')}
        for rid, rec in recordings.items()
    }
  try:
    with open(RECORDINGS_STATE_FILE, 'w', encoding='utf-8') as f:
      json.dump(serializable, f, ensure_ascii=False, indent=2)
  except Exception as e:
    print('[ERROR] Αποτυχία αποθήκευσης recordings.json:', e)


def load_recordings_state():
  if not os.path.exists(RECORDINGS_STATE_FILE):
    return
  try:
    with open(RECORDINGS_STATE_FILE, 'r', encoding='utf-8') as f:
      saved = json.load(f)
  except Exception as e:
    print('[ERROR] Αποτυχία φόρτωσης recordings.json:', e)
    return

  for rid, rec in saved.items():
    if rec.get('status') == 'scheduled':
      try:
        start_dt = parse_iso_aware(rec['start_time'])
      except Exception:
        rec['status'] = 'missed'
        recordings[rid] = rec
        continue
      delay = (start_dt - now_local()).total_seconds()
      if delay <= 0:
        rec['status'] = 'missed'
        recordings[rid] = rec
        schedule_next_recurrence(rec)
      else:
        rec['status'] = 'scheduled'
        recordings[rid] = rec
        t = threading.Timer(delay, run_recording, args=(rid,))
        t.daemon = True
        rec['timer'] = t
        t.start()
    elif rec.get('status') == 'recording':
      rec['status'] = 'interrupted'
      recordings[rid] = rec
    else:
      recordings[rid] = rec

  print(f'[INFO] Φορτώθηκαν {len(saved)} εγγραφές από προηγούμενη συνεδρία.')
  save_recordings_state()


def is_valid_stream_url(url):
  if not isinstance(url, str):
    return False
  return url.startswith('http://') or url.startswith('https://')


# ---------------------------------------------------------------------------
# Εξτρα λίστες M3U (custom playlists) — δημιουργούν νέα κατηγορία καναλιών
# ---------------------------------------------------------------------------

def load_playlists():
  with playlists_lock:
    return _load_json_file(PLAYLISTS_FILE, {})


def save_playlists(data):
  with playlists_lock:
    _save_json_file(PLAYLISTS_FILE, data)


def load_custom_channels():
  with playlists_lock:
    return _load_json_file(CUSTOM_CHANNELS_FILE, {})


def save_custom_channels(data):
  with playlists_lock:
    _save_json_file(CUSTOM_CHANNELS_FILE, data)


epg_overrides_lock = threading.Lock()


def load_epg_overrides():
  """channel_key (σταθερό hash του URL, βλ. frontend stableChannelKey) -> epg channel id."""
  with epg_overrides_lock:
    return _load_json_file(EPG_OVERRIDES_FILE, {})


def save_epg_overrides(data):
  with epg_overrides_lock:
    _save_json_file(EPG_OVERRIDES_FILE, data)


category_overrides_lock = threading.Lock()


def load_category_overrides():
  """channel_key (ίδιο σταθερό hash) -> όνομα κατηγορίας που επέλεξε χειροκίνητα ο
  χρήστης. Όταν υπάρχει, υπερισχύει της αρχικής κατηγορίας/λίστας του καναλιού και
  το κανάλι εμφανίζεται ΜΟΝΟ εκεί (βλ. εφαρμογή στο frontend, loadChannels())."""
  with category_overrides_lock:
    return _load_json_file(CATEGORY_OVERRIDES_FILE, {})


def save_category_overrides(data):
  with category_overrides_lock:
    _save_json_file(CATEGORY_OVERRIDES_FILE, data)


def parse_m3u(text, fallback_category_name):
  """Απλός parser για M3U/M3U8 (#EXTINF + tvg-logo/group-title + γραμμή URL).

  Η κατηγορία κάθε καναλιού παίρνεται ΑΥΤΟΜΑΤΑ από το δικό του group-title μέσα
  στη λίστα (αν υπάρχει) — έτσι μια λίστα με πολλές διαφορετικές κατηγορίες
  μοιράζεται αυτόματα σε αντίστοιχες κατηγορίες στην εφαρμογή, αντί να μπαίνουν
  ΟΛΑ τα κανάλια της κάτω από ένα ενιαίο όνομα. Το fallback_category_name
  χρησιμοποιείται μόνο για κανάλια που δεν έχουν καθόλου group-title."""
  lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
  channels = []
  pending_name = None
  pending_logo = ''
  pending_group = ''

  for raw_line in lines:
    line = raw_line.strip()
    if not line:
      continue
    if line.startswith('#EXTINF'):
      logo_match = re.search(r'tvg-logo="([^"]*)"', line)
      group_match = re.search(r'group-title="([^"]*)"', line)
      name_match = re.search(r',(.*)$', line)
      pending_name = name_match.group(1).strip() if name_match else 'Άγνωστο Κανάλι'
      pending_logo = logo_match.group(1) if logo_match else ''
      pending_group = (group_match.group(1).strip() if group_match else '') or ''
    elif line.startswith('#'):
      continue
    else:
      if pending_name is not None and is_valid_stream_url(line):
        channels.append({
            'name': pending_name,
            'group': pending_group or fallback_category_name or 'Χωρίς κατηγορία',
            'logo': pending_logo,
            'url': line,
        })
      pending_name = None
      pending_logo = ''
      pending_group = ''

  return channels


def sanitize_filename_part(s):
  s = (s or '').strip()
  return ''.join(c for c in s if c.isalnum() or c in (' ', '_', '-')).strip()


RECURRENCE_LABELS = {
    'none': 'Καμία',
    'daily': 'Καθημερινά',
    'weekly': 'Εβδομαδιαία',
    'weekdays': 'Δευτέρα-Παρασκευή',
    'weekend': 'Σάββατο-Κυριακή',
}


def compute_next_occurrence(start_dt, recurrence):
  if recurrence == 'daily':
    return start_dt + timedelta(days=1)
  if recurrence == 'weekly':
    return start_dt + timedelta(days=7)
  if recurrence == 'weekdays':
    nxt = start_dt + timedelta(days=1)
    while nxt.weekday() >= 5:
      nxt += timedelta(days=1)
    return nxt
  if recurrence == 'weekend':
    nxt = start_dt + timedelta(days=1)
    while nxt.weekday() < 5:
      nxt += timedelta(days=1)
    return nxt
  return None


def arm_recording(rec):
  delay = (parse_iso_aware(rec['start_time']) - now_local()).total_seconds()
  if delay <= 0:
    threading.Thread(target=run_recording, args=(rec['id'],), daemon=True).start()
  else:
    t = threading.Timer(delay, run_recording, args=(rec['id'],))
    t.daemon = True
    rec['timer'] = t
    t.start()


def schedule_next_recurrence(rec):
  recurrence = rec.get('recurrence')
  if not recurrence or recurrence == 'none':
    return
  if rec.get('status') == 'cancelled':
    return

  try:
    current_start = parse_iso_aware(rec['start_time'])
  except Exception:
    return
  next_start = compute_next_occurrence(current_start, recurrence)
  if not next_start:
    return

  guard = 0
  while next_start <= now_local() and guard < 1000:
    next_start = compute_next_occurrence(next_start, recurrence)
    guard += 1
  if not next_start or next_start <= now_local():
    print(f"[ERROR] Δεν βρέθηκε μελλοντική επανάληψη για '{rec['name']}' — σταματά η σειρά.")
    return

  new_id = uuid.uuid4().hex[:12]
  new_rec = {
      'id': new_id,
      'name': rec['name'],
      'url': rec['url'],
      'channel_id': rec.get('channel_id'),
      'program': rec.get('program'),
      'start_time': next_start.isoformat(),
      'duration': rec['duration'],
      'status': 'scheduled',
      'filename': None,
      'recurrence': recurrence,
      'series_id': rec.get('series_id') or rec['id'],
  }
  with recordings_lock:
    recordings[new_id] = new_rec
    arm_recording(new_rec)
  save_recordings_state()
  print(f"[INFO] Προγραμματίστηκε η επόμενη επανάληψη ({RECURRENCE_LABELS.get(recurrence, recurrence)}) "
        f"για '{rec['name']}' στις {next_start}.")


def build_ffmpeg_cmd(stream_url, duration_sec, filepath):
  return [
      'ffmpeg', '-y',
      # Reconnect flags: αν κοπεί προσωρινά το HTTP(S)-based IPTV stream (HLS/DASH),
      # η ffmpeg θα προσπαθήσει να ξανασυνδεθεί αντί να τερματίσει αμέσως.
      '-reconnect', '1',
      '-reconnect_streamed', '1',
      '-reconnect_at_eof', '1',
      '-reconnect_delay_max', '5',
      '-i', stream_url,
      '-t', str(duration_sec),
      '-c', 'copy', filepath,
  ]


def run_recording(rec_id):
  with recordings_lock:
    rec = recordings.get(rec_id)
    if not rec or rec.get('status') == 'cancelled':
      return
    rec['status'] = 'recording'

  if not os.path.exists(get_recordings_dir()):
    os.makedirs(get_recordings_dir())

  now = now_local()
  channel_part = sanitize_filename_part(rec['name']) or 'Κανάλι'
  program_part = sanitize_filename_part(rec.get('program')) or 'Live'
  date_part = now.strftime('%Y-%m-%d')
  time_part = now.strftime('%Hh%M')
  filename = f'{date_part} {time_part} - {channel_part} - {program_part}.ts'
  filepath = os.path.join(get_recordings_dir(), filename)

  cmd = build_ffmpeg_cmd(rec['url'], rec['duration'], filepath)

  print(f'[REC START] {filepath}')
  try:
    popen_kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    if os.name == 'nt':
      popen_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, **popen_kwargs)
    with recordings_lock:
      rec['process'] = proc
      rec['filename'] = filename
    returncode = proc.wait()
    with recordings_lock:
      if rec.get('status') == 'cancelled':
        pass
      elif returncode != 0:
        rec['status'] = 'failed'
        rec['error'] = f'Το ffmpeg τερμάτισε με κωδικό {returncode}.'
      else:
        rec['status'] = 'completed'
      rec.pop('process', None)
    print(f'[REC FINISH] {filepath} (exit code {returncode})')
  except FileNotFoundError:
    with recordings_lock:
      rec['status'] = 'failed'
      rec['error'] = 'Δεν βρέθηκε το ffmpeg στο PATH του server.'
    print('[ERROR] Δεν βρέθηκε το ffmpeg — εγκατέστησέ το και βεβαιώσου ότι είναι στο PATH.')
  except Exception as e:
    with recordings_lock:
      rec['status'] = 'failed'
      rec['error'] = str(e)
    print('[ERROR] Σφάλμα εγγραφής:', e)
  finally:
    save_recordings_state()
    schedule_next_recurrence(rec)


def has_active_recording_for_channel(channel_id):
  """Προστασία από διπλή εγγραφή: True αν το κανάλι ΗΔΗ γράφεται αυτή τη στιγμή."""
  if channel_id is None:
    return False
  with recordings_lock:
    for rec in recordings.values():
      if rec.get('channel_id') == channel_id and rec.get('status') == 'recording':
        return True
  return False


def resolve_recording_conflict(channel_id, start_dt, duration_sec, pre_pad_sec=0, post_pad_sec=0):
  """Έλεγχος + αυτόματη επίλυση επικαλυπτόμενων προγραμματισμένων εγγραφών στο ίδιο
  κανάλι. Δύο συνεχόμενες εκπομπές (π.χ. πρόγραμμα Α τελειώνει ακριβώς όταν αρχίζει
  το Β) συχνά "επικαλύπτονται" ΜΟΝΟ λόγω του pre/post padding ασφαλείας γύρω από
  την πραγματική ώρα της κάθε εκπομπής (βλ. rec_pre_pad_min/rec_post_pad_min στις
  ρυθμίσεις) — όχι επειδή όντως ζητήθηκε να γραφτεί το ίδιο διάστημα δύο φορές. Σε
  αυτή την περίπτωση περικόπτουμε αυτόματα το padding που επικαλύπτεται (ώστε οι δύο
  εγγραφές απλά να ακουμπάνε, χωρίς κενό ή επικάλυψη) αντί να αρνηθούμε ολόκληρο το
  αίτημα. Αν όμως επικαλύπτεται ο ΠΡΑΓΜΑΤΙΚΟΣ πυρήνας (μετά την αφαίρεση του padding)
  των δύο εκπομπών, ή υπάρχει ήδη ενεργή εγγραφή, μπλοκάρουμε όπως πριν.

  Επιστρέφει (ok: bool, τελική_διάρκεια_sec: int)."""
  if channel_id is None:
    return True, duration_sec

  new_start = start_dt
  new_end = start_dt + timedelta(seconds=duration_sec)
  new_core_start = new_start + timedelta(seconds=pre_pad_sec)
  new_core_end = new_end - timedelta(seconds=post_pad_sec)
  needs_save = False

  with recordings_lock:
    for rec in recordings.values():
      if rec.get('channel_id') != channel_id:
        continue
      status = rec.get('status')
      if status == 'recording':
        return False, duration_sec
      if status != 'scheduled':
        continue
      try:
        other_start = parse_iso_aware(rec['start_time'])
      except Exception:
        continue
      other_duration = rec.get('duration', 0)
      other_end = other_start + timedelta(seconds=other_duration)
      if not (new_start < other_end and other_start < new_end):
        continue  # καμία επικάλυψη καν

      other_pre = rec.get('pre_pad_sec', 0)
      other_post = rec.get('post_pad_sec', 0)
      other_core_start = other_start + timedelta(seconds=other_pre)
      other_core_end = other_end - timedelta(seconds=other_post)

      if new_core_start < other_core_end and other_core_start < new_core_end:
        return False, duration_sec  # πραγματική επικάλυψη προγραμμάτων

      # Επικάλυψη μόνο στο padding -> περικοπή του "νωρίτερου" ώστε να ακουμπάνε.
      if other_start <= new_start:
        trimmed = int((new_start - other_start).total_seconds())
        if 0 < trimmed < other_duration:
          rec['duration'] = trimmed
          needs_save = True
      else:
        allowed = int((other_start - new_start).total_seconds())
        if 0 < allowed < duration_sec:
          duration_sec = allowed

  if needs_save:
    save_recordings_state()

  return True, duration_sec


# ---------------------------------------------------------------------------
# Static / χώρος εργασίας
# ---------------------------------------------------------------------------

@app.route('/')
def index():
  return send_from_directory('.', 'index.html')


def _compute_merged_channels():
  """Ενοποιεί βασικά + custom-playlist κανάλια, αφαιρεί διπλότυπα (ίδιο URL).
  Ξεχωριστή συνάρτηση ώστε να τη χρησιμοποιεί και ο έλεγχος υγείας καναλιών."""
  base = _load_json_file(CHANNELS_FILE, {'channels': [], 'epgMap': {}})
  channels = list(base.get('channels', []))

  playlists = load_playlists()
  custom = load_custom_channels()
  ordered_pids = sorted(custom.keys(), key=lambda pid: playlists.get(pid, {}).get('added_at', ''))
  for pid in ordered_pids:
    channels.extend(custom[pid])

  # Αφαίρεση διπλότυπων καναλιών: όταν το ΙΔΙΟ ακριβώς stream URL υπάρχει σε
  # παραπάνω από ένα playlist (π.χ. επικολλήθηκε το ίδιο "PLAY TV" σε δύο διαφορετικές
  # λίστες), μέχρι τώρα εμφανιζόταν πολλαπλές φορές στη λίστα καναλιών — και όλα τα
  # αντίγραφα εμφανίζονταν ταυτόχρονα ως "γίνεται εγγραφή" μόλις γραφόταν ένα από αυτά,
  # αφού μοιράζονται ακριβώς το ίδιο URL/ταυτότητα. Κρατάμε μόνο την πρώτη εμφάνιση κάθε
  # URL· τα βασικά κανάλια (channels.json) έρχονται πάντα πρώτα, οπότε η σειρά/θέση τους
  # (και άρα η αντιστοίχιση με το epgMap) δεν επηρεάζεται — μόνο μεταγενέστερα (custom
  # playlist) διπλότυπα αφαιρούνται.
  seen_urls = set()
  deduped = []
  for ch in channels:
    url_key = (ch.get('url') or '').strip().lower()
    if url_key:
      if ch.get('force_duplicate'):
        # Ο χρήστης ζήτησε ρητά να προστεθεί ΑΝΕΞΑΡΤΗΤΑ από το αν το ίδιο URL
        # υπάρχει ήδη αλλού (π.χ. θέλει το ίδιο stream και σε άλλη κατηγορία/όνομα).
        seen_urls.add(url_key)
      elif url_key in seen_urls:
        continue
      else:
        seen_urls.add(url_key)
    deduped.append(ch)

  return deduped, base.get('epgMap', {})


@app.route('/api/channels')
def get_channels():
  deduped, epg_map = _compute_merged_channels()
  return jsonify({'channels': deduped, 'epgMap': epg_map})


@app.route('/api/browse_dirs')
@requires_auth
def browse_dirs():
  """Λίστα υποφακέλων ενός φακέλου του SERVER (όχι του browser του χρήστη) — αυτό
  δίνει τη δυνατότητα "browse" για επιλογή φακέλου εγγραφών, αφού ένας browser δεν
  έχει καμία πρόσβαση στο filesystem ενός απομακρυσμένου server μέσω native dialog."""
  requested = request.args.get('path', '').strip()
  base = os.path.abspath(requested) if requested else os.path.abspath(os.path.expanduser('~'))

  if not os.path.isdir(base):
    return jsonify({'status': 'error', 'message': 'Ο φάκελος δεν υπάρχει.'}), 400

  try:
    entries = []
    for name in sorted(os.listdir(base), key=str.lower):
      full = os.path.join(base, name)
      if os.path.isdir(full) and not name.startswith('.'):
        entries.append(name)
  except PermissionError:
    return jsonify({'status': 'error', 'message': 'Δεν υπάρχει δικαίωμα ανάγνωσης σε αυτόν τον φάκελο.'}), 403

  parent = os.path.dirname(base)

  # Στα Windows δεν υπάρχει ένα ενιαίο root filesystem — κάθε δίσκος (C:\, D:\, ...)
  # είναι δικό του root, οπότε το ανέβασμα μέσω parent_path ΔΕΝ φτάνει ποτέ σε άλλον
  # δίσκο. Εδώ βρίσκουμε ποιοι δίσκοι υπάρχουν ώστε το frontend να δείχνει κουμπιά
  # γρήγορης εναλλαγής ανάμεσά τους. Σε Linux/Mac η λίστα μένει άδεια (άσχετο εκεί).
  drives = []
  if os.name == 'nt':
    import string
    for letter in string.ascii_uppercase:
      drive_path = f'{letter}:\\'
      if os.path.exists(drive_path):
        drives.append(drive_path)

  return jsonify({
      'status': 'success',
      'current_path': base,
      'parent_path': parent if parent != base else None,
      'writable': os.access(base, os.W_OK),
      'entries': entries,
      'drives': drives,
  })


@app.route('/api/channels/health_check', methods=['POST'])
@requires_auth
def start_channel_health_check():
  with CHANNEL_HEALTH_LOCK:
    if CHANNEL_HEALTH_SCAN['active']:
      return jsonify({
          'status': 'already_running',
          'total': CHANNEL_HEALTH_SCAN['total'],
          'done': CHANNEL_HEALTH_SCAN['done'],
      })

  deduped, _ = _compute_merged_channels()
  urls = sorted({(ch.get('url') or '').strip() for ch in deduped if ch.get('url')})
  threading.Thread(target=_run_channel_health_scan, args=(urls,), daemon=True).start()
  return jsonify({'status': 'started', 'total': len(urls)})


@app.route('/api/channels/health')
def get_channel_health():
  with CHANNEL_HEALTH_LOCK:
    return jsonify({
        'scanning': CHANNEL_HEALTH_SCAN['active'],
        'total': CHANNEL_HEALTH_SCAN['total'],
        'done': CHANNEL_HEALTH_SCAN['done'],
        'results': dict(CHANNEL_HEALTH),
    })


@app.route('/api/logo')
def proxy_logo():
  """Φέρνει ένα λογότυπο καναλιού μέσω του server (όχι απευθείας από τον browser).

  Γιατί: πολλά λογότυπα δείχνουν σε imgur.com, το οποίο συχνά μπλοκάρει ή
  αντικαθιστά "hotlinked" εικόνες (δηλαδή <img> που φορτώνει απευθείας ο browser
  από άλλο site/localhost, χωρίς σωστό Referer) με ένα δικό του υποκατάστατο —
  με HTTP 200, όχι 404, οπότε ο browser ΔΕΝ βλέπει κανένα σφάλμα και δεν
  ενεργοποιείται ποτέ το onerror fallback του frontend, μένοντας έτσι μια κενή/
  λευκή εικόνα. Κάνοντας το request από τον ίδιο τον server (χωρίς προβληματικό
  Referer, σαν κανονικό API call) αποφεύγουμε αυτό το μπλοκάρισμα στις
  περισσότερες περιπτώσεις. Αποτελέσματα αποθηκεύονται σε cache στο δίσκο ώστε
  να μην ξαναγίνεται το ίδιο εξωτερικό αίτημα σε κάθε φόρτωση της σελίδας.
  Αν η λήψη αποτύχει πραγματικά (νεκρό link, timeout), επιστρέφουμε γνήσιο 404
  ώστε να δουλέψει κανονικά το onerror fallback (τοπικό SVG με τα αρχικά)."""
  url = request.args.get('url', '').strip()
  if not url.startswith(('http://', 'https://')):
    return '', 400

  cache_key = hashlib.sha256(url.encode('utf-8')).hexdigest()
  # Ψάξε τυχόν ήδη cache-αρισμένο αρχείο, όποια κι αν είναι η επέκτασή του.
  for existing in os.listdir(LOGOS_CACHE_DIR):
    if existing.startswith(cache_key + '.'):
      return send_from_directory(LOGOS_CACHE_DIR, existing)

  with _logo_failure_lock:
    failed_at = _logo_failure_cache.get(cache_key)
  if failed_at is not None and (time.time() - failed_at) < LOGO_FAILURE_TTL_SEC:
    # Απέτυχε πρόσφατα (π.χ. το domain είναι μπλοκαρισμένο) — μην το ξαναπροσπαθήσεις τώρα.
    return '', 404

  try:
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
    }
    resp = _logo_session.get(url, headers=headers, timeout=5)
    content_type = resp.headers.get('Content-Type', '')
    if resp.status_code != 200 or not content_type.startswith('image/'):
      print(f'[LOGO] Απέτυχε {url} -> status={resp.status_code} content-type={content_type!r}')
      with _logo_failure_lock:
        _logo_failure_cache[cache_key] = time.time()
      return '', 404
    ext = mimetypes.guess_extension(content_type.split(';')[0].strip()) or '.img'
    cache_path = os.path.join(LOGOS_CACHE_DIR, cache_key + ext)
    with open(cache_path, 'wb') as f:
      f.write(resp.content)
    return send_from_directory(LOGOS_CACHE_DIR, cache_key + ext)
  except requests.RequestException as e:
    print(f'[LOGO] Εξαίρεση για {url} -> {type(e).__name__}: {e}')
    with _logo_failure_lock:
      _logo_failure_cache[cache_key] = time.time()
    return '', 404


@app.route('/api/playlists', methods=['GET'])
def list_playlists():
  playlists = load_playlists()
  deduped, _ = _compute_merged_channels()
  visible_counts = {}
  for ch in deduped:
    lst = ch.get('list')
    if lst:
      visible_counts[lst] = visible_counts.get(lst, 0) + 1
  result = []
  for p in playlists.values():
    p = dict(p)
    p['visible_count'] = visible_counts.get(p.get('category'), 0)
    result.append(p)
  return jsonify(result)


@app.route('/api/playlists', methods=['POST'])
@requires_auth
def add_playlist():
  data = request.json or {}
  category_name = (data.get('category') or '').strip()
  url = (data.get('url') or '').strip() or None
  raw_m3u = data.get('m3u_text')
  force_duplicates = bool(data.get('force_duplicates'))

  # Το "category" είναι το όνομα ΑΥΤΗΣ ΤΗΣ ΛΙΣΤΑΣ (π.χ. "hacker") — λειτουργεί σαν
  # γονική κατηγορία (branch) στο sidebar. Αν η ίδια η λίστα έχει group-title ανά
  # κανάλι (π.χ. "Sports", "News"), αυτά γίνονται ΠΑΡΑΚΛΑΔΙΑ μέσα σε αυτή τη λίστα
  # (βλ. parse_m3u/ch['group']) — όχι ξεχωριστές, συγκεντρωτικές κατηγορίες στο ίδιο
  # επίπεδο με τις υπόλοιπες.
  if not category_name:
    return jsonify({'status': 'error', 'message': 'Δώσε ένα όνομα για τη λίστα.'}), 400
  if not url and not raw_m3u:
    return jsonify({'status': 'error', 'message': 'Δώσε είτε URL M3U είτε επικόλλησε το περιεχόμενο.'}), 400
  if url and not is_valid_stream_url(url):
    return jsonify({'status': 'error', 'message': 'Μη έγκυρο URL (πρέπει να ξεκινά με http:// ή https://).'}), 400

  try:
    if url:
      headers = {'User-Agent': 'Mozilla/5.0 (compatible; IPTVPlayer/1.0)'}
      r = requests.get(url, headers=headers, timeout=30)
      r.raise_for_status()
      text = r.text
    else:
      text = raw_m3u
    parsed = parse_m3u(text, category_name)
  except requests.RequestException as e:
    return jsonify({'status': 'error', 'message': f'Αποτυχία λήψης της λίστας: {e}'}), 400
  except Exception as e:
    return jsonify({'status': 'error', 'message': f'Αποτυχία ανάλυσης M3U: {e}'}), 400

  if not parsed:
    return jsonify({'status': 'error', 'message': 'Δεν βρέθηκαν έγκυρα κανάλια μέσα στη λίστα.'}), 400

  # Κάθε κανάλι κρατάει το δικό του group-title (ή το ίδιο το category_name αν δεν
  # είχε) σαν ΠΑΡΑΚΛΑΔΙ (ch['group']), και επιπλέον σημειώνουμε ΣΕ ΠΟΙΑ ΛΙΣΤΑ ανήκει
  # (ch['list']) ώστε το sidebar να τα εμφανίζει φωλιασμένα κάτω από τη λίστα τους
  # αντί να τα ανακατεύει επίπεδα με όλες τις υπόλοιπες κατηγορίες.
  for ch in parsed:
    ch['list'] = category_name
    if force_duplicates:
      ch['force_duplicate'] = True

  distinct_groups = sorted({c['group'] for c in parsed if c.get('group') and c['group'] != category_name})

  pid = uuid.uuid4().hex[:12]
  playlists = load_playlists()
  playlists[pid] = {
      'id': pid,
      'category': category_name,
      'source': url or 'Επικολλημένο περιεχόμενο',
      'added_at': now_local().isoformat(),
      'channel_count': len(parsed),
  }
  save_playlists(playlists)

  custom = load_custom_channels()
  custom[pid] = parsed
  save_custom_channels(custom)

  # Πόσα από τα κανάλια που μόλις προστέθηκαν είναι στην πραγματικότητα διπλότυπα
  # (ίδιο ακριβώς stream URL) κάποιου ήδη υπάρχοντος καναλιού, και θα αποκρυφτούν
  # αυτόματα από τη λίστα (βλ. _compute_merged_channels). Ο χρήστης βλέπει συχνά
  # "λιγότερα κανάλια απ' όσα περίμενε" και αναρωτιέται γιατί — αυτό το μήνυμα το
  # εξηγεί αμέσως, χωρίς να χρειάζεται να ψάξει.
  deduped, _ = _compute_merged_channels()
  deduped_urls = {(c.get('url') or '').strip().lower() for c in deduped}
  duplicates = [c['name'] for c in parsed if (c.get('url') or '').strip().lower() not in deduped_urls]
  visible_count = len(parsed) - len(duplicates)

  if len(distinct_groups) >= 1:
    cat_desc = f'λίστα «{category_name}» με παρακλάδια: {", ".join(distinct_groups[:6])}{"..." if len(distinct_groups) > 6 else ""}'
  else:
    cat_desc = f'λίστα «{category_name}»'

  msg = f'Προστέθηκαν {len(parsed)} κανάλια — {cat_desc} — {visible_count} νέα/μοναδικά.'
  if duplicates:
    preview = ', '.join(duplicates[:8]) + (f' και {len(duplicates) - 8} ακόμα' if len(duplicates) > 8 else '')
    msg += f' {len(duplicates)} αποκρύφτηκαν επειδή έχουν ίδιο URL με κανάλι που υπάρχει ήδη ({preview}).'

  return jsonify({
      'status': 'success', 'id': pid, 'count': len(parsed),
      'visible_count': visible_count, 'duplicate_names': duplicates,
      'message': msg,
  })


@app.route('/api/playlists/<pid>', methods=['DELETE'])
@requires_auth
def delete_playlist(pid):
  playlists = load_playlists()
  if pid not in playlists:
    return jsonify({'status': 'error', 'message': 'Άγνωστη λίστα.'}), 404

  del playlists[pid]
  save_playlists(playlists)
  custom = load_custom_channels()
  custom.pop(pid, None)
  save_custom_channels(custom)

  return jsonify({'status': 'success', 'message': 'Η λίστα αφαιρέθηκε.'})


@app.route('/api/epg/refresh', methods=['POST'])
@requires_auth
def force_epg_refresh():
  """Αναγκάζει άμεσο ξανακατέβασμα του EPG (κύριο + όλα τα συμπληρωματικά sources),
  χωρίς να περιμένει το προγραμματισμένο interval ή restart του server. Χρήσιμο π.χ.
  αμέσως μετά την προσθήκη ενός νέου epg_extra_urls."""
  with _epg_state_lock:
    _epg_state['last_attempt'] = time.time()
  ok = update_epg()
  with _epg_state_lock:
    message = _epg_state['last_error'] if not ok else (_epg_state['last_error'] or 'Το EPG ανανεώθηκε επιτυχώς.')
  return jsonify({'status': 'success' if ok else 'error', 'message': message})


@app.route('/epg.xml')
def get_epg():
  cooldown = get_setting('epg_retry_cooldown_sec') or 30
  if not os.path.exists(EPG_FILE):
    with _epg_state_lock:
      last_attempt = _epg_state['last_attempt']
    now_ts = time.time()
    if now_ts - last_attempt >= cooldown:
      with _epg_state_lock:
        _epg_state['last_attempt'] = now_ts
      update_epg()
    else:
      wait_left = round(cooldown - (now_ts - last_attempt))
      resp = make_response(jsonify({'error': 'EPG Not Ready', 'retry_after_sec': wait_left}), 503)
      resp.headers['Retry-After'] = str(wait_left)
      return resp
  if os.path.exists(EPG_FILE):
    resp = make_response(send_from_directory('.', EPG_FILE))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp
  return jsonify({'error': 'EPG Not Ready', 'retry_after_sec': cooldown}), 503


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

PUBLIC_SETTINGS_KEYS = [
    'timezone', 'rec_pre_pad_min', 'rec_post_pad_min',
    'max_duration_hours', 'cleanup_days', 'auth_enabled', 'protect_all',
    'locked_categories',
]


@app.route('/api/settings', methods=['GET'])
def get_settings_public():
  """Μόνο μη-ευαίσθητα πεδία — ΠΟΤΕ δεν επιστρέφεται το password hash."""
  with settings_lock:
    return jsonify({k: SETTINGS.get(k, DEFAULT_SETTINGS.get(k)) for k in PUBLIC_SETTINGS_KEYS})


@app.route('/api/settings', methods=['POST'])
@requires_auth
def update_settings():
  """Αλλαγή ρυθμίσεων από το UI — δεν χρειάζεται να πειράξεις τον κώδικα/env vars."""
  global SETTINGS
  data = request.json or {}

  with settings_lock:
    current = dict(SETTINGS)

  errors = []

  if 'timezone' in data:
    try:
      ZoneInfo(data['timezone'])
      current['timezone'] = data['timezone']
    except Exception:
      errors.append('Μη έγκυρη ζώνη ώρας (π.χ. "Europe/Athens").')

  for key in ('rec_pre_pad_min', 'rec_post_pad_min', 'cleanup_days'):
    if key in data:
      try:
        val = int(data[key])
        if val < 0:
          raise ValueError()
        current[key] = val
      except (TypeError, ValueError):
        errors.append(f'Μη έγκυρη τιμή για {key}.')

  if 'max_duration_hours' in data:
    try:
      val = int(data['max_duration_hours'])
      if not (1 <= val <= 24):
        raise ValueError()
      current['max_duration_hours'] = val
    except (TypeError, ValueError):
      errors.append('Η μέγιστη διάρκεια πρέπει να είναι 1-24 ώρες.')

  if 'auth_enabled' in data:
    current['auth_enabled'] = bool(data['auth_enabled'])
  if 'protect_all' in data:
    current['protect_all'] = bool(data['protect_all'])
  if 'locked_categories' in data:
    if isinstance(data['locked_categories'], list):
      current['locked_categories'] = [str(c) for c in data['locked_categories']]
    else:
      errors.append('Μη έγκυρη λίστα κλειδωμένων κατηγοριών.')
  if 'epg_url' in data and is_valid_stream_url(data['epg_url']):
    current['epg_url'] = data['epg_url']
  if 'epg_extra_urls' in data:
    if isinstance(data['epg_extra_urls'], list):
      cleaned = [u.strip() for u in data['epg_extra_urls'] if u and u.strip()]
      bad = [u for u in cleaned if not is_valid_stream_url(u)]
      if bad:
        errors.append(f'Μη έγκυρα URLs: {", ".join(bad)}')
      else:
        current['epg_extra_urls'] = cleaned
    else:
      errors.append('Μη έγκυρη λίστα επιπλέον EPG sources.')
  new_recordings_dir_msg = None
  if 'recordings_dir' in data and (data['recordings_dir'] or '').strip():
    new_dir = data['recordings_dir'].strip()
    old_dir = get_recordings_dir()
    try:
      os.makedirs(new_dir, exist_ok=True)
      if not os.access(new_dir, os.W_OK):
        raise PermissionError('Δεν υπάρχει δικαίωμα εγγραφής.')
      current['recordings_dir'] = new_dir
      if os.path.abspath(new_dir) != os.path.abspath(old_dir):
        new_recordings_dir_msg = (
            f' Ο νέος φάκελος ισχύει από τώρα για ΝΕΕΣ εγγραφές — οι υπάρχουσες '
            f'παραμένουν στον παλιό φάκελο ({old_dir}), δεν μετακινούνται αυτόματα.'
        )
    except OSError as e:
      errors.append(f'Μη έγκυρος/μη προσβάσιμος φάκελος εγγραφών: {e}')
  if 'epg_refresh_hours' in data:
    try:
      val = int(data['epg_refresh_hours'])
      if val < 1:
        raise ValueError()
      current['epg_refresh_hours'] = val
    except (TypeError, ValueError):
      errors.append('Μη έγκυρες ώρες ανανέωσης EPG.')

  new_password_set = False
  if data.get('new_username'):
    current['auth_user'] = data['new_username'].strip()
  if data.get('new_password'):
    if len(data['new_password']) < 8:
      errors.append('Ο νέος κωδικός πρέπει να έχει τουλάχιστον 8 χαρακτήρες.')
    else:
      current['auth_pass_hash'] = generate_password_hash(data['new_password'])
      new_password_set = True

  if errors:
    return jsonify({'status': 'error', 'message': ' '.join(errors)}), 400

  with settings_lock:
    SETTINGS = current
  save_settings(current)

  msg = 'Οι ρυθμίσεις αποθηκεύτηκαν.'
  if new_password_set:
    msg += ' Ο νέος κωδικός ισχύει από τώρα.'
  if new_recordings_dir_msg:
    msg += new_recordings_dir_msg
  return jsonify({'status': 'success', 'message': msg})


@app.route('/api/backup/export')
@requires_auth
def export_backup():
  """Κατεβάζει ένα .zip με όλες τις ρυθμίσεις/λίστες/κανάλια (ΟΧΙ τα ίδια τα αρχεία
  εγγραφών .ts — μόνο τα μεταδεδομένα τους — αφού μπορεί να είναι πολύ μεγάλα)."""
  files_to_include = [
      CHANNELS_FILE, PLAYLISTS_FILE, CUSTOM_CHANNELS_FILE,
      SETTINGS_FILE, RECORDINGS_STATE_FILE, EPG_OVERRIDES_FILE, CATEGORY_OVERRIDES_FILE,
  ]
  buf = io.BytesIO()
  with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
    for fname in files_to_include:
      if os.path.isfile(fname):
        zf.write(fname, arcname=fname)
  buf.seek(0)
  ts = datetime.now().strftime('%Y-%m-%d_%H%M')
  return Response(
      buf.read(),
      mimetype='application/zip',
      headers={'Content-Disposition': f'attachment; filename="iptv_backup_{ts}.zip"'},
  )


@app.route('/api/backup/import', methods=['POST'])
@requires_auth
def import_backup():
  """Επαναφορά από .zip που φτιάχτηκε με το /api/backup/export. Κρατάει αντίγραφο
  ασφαλείας (.bak) των τρεχόντων αρχείων πριν τα αντικαταστήσει, ΜΟΝΟ για τα
  γνωστά/αναμενόμενα ονόματα αρχείων (καμία εξαγωγή αυθαίρετου path από το zip)."""
  if 'file' not in request.files:
    return jsonify({'status': 'error', 'message': 'Δεν στάλθηκε αρχείο.'}), 400

  allowed_files = {
      CHANNELS_FILE, PLAYLISTS_FILE, CUSTOM_CHANNELS_FILE,
      SETTINGS_FILE, RECORDINGS_STATE_FILE, EPG_OVERRIDES_FILE, CATEGORY_OVERRIDES_FILE,
  }
  upload = request.files['file']
  try:
    zf = zipfile.ZipFile(upload.stream)
  except zipfile.BadZipFile:
    return jsonify({'status': 'error', 'message': 'Το αρχείο δεν είναι έγκυρο .zip.'}), 400

  restored = []
  ts = datetime.now().strftime('%Y%m%d_%H%M%S')
  for name in zf.namelist():
    basename = os.path.basename(name)
    if basename not in allowed_files or basename != name:
      continue  # αγνόησε ό,τι δεν είναι στη λίστα ή έχει ύποπτο path (π.χ. "../")
    if os.path.isfile(basename):
      shutil.copy2(basename, f'{basename}.bak-{ts}')
    with zf.open(name) as src, open(basename, 'wb') as dst:
      dst.write(src.read())
    restored.append(basename)

  if not restored:
    return jsonify({
        'status': 'error',
        'message': 'Το zip δεν περιείχε κανένα αναγνωρίσιμο αρχείο backup.',
    }), 400

  return jsonify({
      'status': 'success',
      'message': (
          f'Επαναφέρθηκαν: {", ".join(restored)}. '
          'Συνιστάται restart του server για να φορτωθούν πλήρως οι αλλαγές '
          '(ειδικά αν άλλαξαν προγραμματισμένες εγγραφές).'
      ),
      'restored': restored,
  })



@app.route('/api/epg/overrides', methods=['GET'])
def get_epg_overrides():
  """channel_key -> epg channel id, για κανάλια που ο χρήστης αντιστοίχισε χειροκίνητα
  (π.χ. όταν η αυτόματη αντιστοίχιση/ταίριασμα ονόματος αποτυγχάνει)."""
  return jsonify(load_epg_overrides())


@app.route('/api/epg/overrides', methods=['POST'])
@requires_auth
def set_epg_override():
  data = request.json or {}
  channel_key = (data.get('channel_key') or '').strip()
  epg_id = (data.get('epg_id') or '').strip()
  if not channel_key:
    return jsonify({'status': 'error', 'message': 'Λείπει το channel_key.'}), 400
  overrides = load_epg_overrides()
  if epg_id:
    overrides[channel_key] = epg_id
  else:
    overrides.pop(channel_key, None)
  save_epg_overrides(overrides)
  return jsonify({'status': 'success', 'overrides': overrides})


@app.route('/api/category_overrides', methods=['GET'])
def get_category_overrides():
  """channel_key -> όνομα κατηγορίας. Το ίδιο endpoint καλύπτει και single-channel
  και μαζική μετακίνηση — η μαζικότητα γίνεται στο POST, στέλνοντας λίστα από keys."""
  return jsonify(load_category_overrides())


@app.route('/api/category_overrides', methods=['POST'])
@requires_auth
def set_category_overrides():
  data = request.get_json(force=True, silent=True) or {}
  channel_keys = data.get('channel_keys')
  category = (data.get('category') or '').strip()

  if not isinstance(channel_keys, list) or not channel_keys:
    return jsonify({'status': 'error', 'message': 'Δεν επιλέχθηκαν κανάλια.'}), 400

  overrides = load_category_overrides()
  for key in channel_keys:
    if not isinstance(key, str) or not key:
      continue
    if category:
      overrides[key] = category
    else:
      # Άδειο category = επαναφορά στην αρχική κατηγορία/λίστα του καναλιού.
      overrides.pop(key, None)
  save_category_overrides(overrides)
  return jsonify({'status': 'success', 'category': category, 'count': len(channel_keys), 'overrides': overrides})



@app.route('/api/parental/set_pin', methods=['POST'])
@requires_auth
def set_parental_pin():
  """Ορισμός/αλλαγή/αφαίρεση του PIN γονικού ελέγχου. Προστατεύεται με το ίδιο
  admin auth όπως και οι υπόλοιπες ρυθμίσεις — δεν χρειάζεται να ξέρεις το παλιό PIN,
  αρκεί να είσαι συνδεδεμένος admin."""
  global SETTINGS
  data = request.json or {}
  pin = (data.get('pin') or '').strip()
  with settings_lock:
    current = dict(SETTINGS)
  if not pin:
    current['parental_pin_hash'] = None
    msg = 'Το PIN γονικού ελέγχου αφαιρέθηκε — καμία κατηγορία δεν είναι πλέον κλειδωμένη με PIN.'
  else:
    if not pin.isdigit() or not (4 <= len(pin) <= 8):
      return jsonify({'status': 'error', 'message': 'Το PIN πρέπει να έχει 4-8 ψηφία.'}), 400
    current['parental_pin_hash'] = generate_password_hash(pin)
    msg = 'Το PIN γονικού ελέγχου αποθηκεύτηκε.'
  with settings_lock:
    SETTINGS = current
  save_settings(current)
  return jsonify({'status': 'success', 'message': msg})


@app.route('/api/parental/verify', methods=['POST'])
def verify_parental_pin():
  """Δημόσιο endpoint (το χρησιμοποιεί ο θεατής, όχι μόνο ο admin) για να ξεκλειδώσει
  προσωρινά μια κλειδωμένη κατηγορία δίνοντας το PIN."""
  data = request.json or {}
  pin = (data.get('pin') or '').strip()
  pin_hash = get_setting('parental_pin_hash')
  if not pin_hash:
    return jsonify({'ok': False, 'message': 'Δεν έχει οριστεί PIN.'}), 400
  ok = check_password_hash(pin_hash, pin)
  return jsonify({'ok': ok})



@app.route('/api/status')
def get_status():
  with recordings_lock:
    recording_count = sum(1 for r in recordings.values() if r['status'] == 'recording')
    scheduled_count = sum(1 for r in recordings.values() if r['status'] == 'scheduled')
    total_count = len(recordings)

  disk_total = disk_used = disk_free = None
  try:
    disk_total, disk_used, disk_free = shutil.disk_usage('.')
  except Exception:
    pass

  recordings_size = 0
  if os.path.isdir(get_recordings_dir()):
    for fname in os.listdir(get_recordings_dir()):
      fpath = os.path.join(get_recordings_dir(), fname)
      if os.path.isfile(fpath):
        recordings_size += os.path.getsize(fpath)

  cpu_percent = mem_percent = None
  try:
    import psutil  # προαιρετικό: αν δεν είναι εγκατεστημένο, απλά παραλείπεται
    cpu_percent = psutil.cpu_percent(interval=0.1)
    mem_percent = psutil.virtual_memory().percent
  except Exception:
    pass

  with _epg_state_lock:
    epg_info = dict(_epg_state)

  return jsonify({
      'uptime_sec': round(time.time() - SERVER_START_TIME),
      'active_recordings': recording_count,
      'scheduled_recordings': scheduled_count,
      'total_recordings_tracked': total_count,
      'recordings_dir_size_bytes': recordings_size,
      'disk_total_bytes': disk_total,
      'disk_used_bytes': disk_used,
      'disk_free_bytes': disk_free,
      'cpu_percent': cpu_percent,
      'mem_percent': mem_percent,
      'epg_last_success': epg_info['last_success'],
      'epg_last_error': epg_info['last_error'],
      'server_time': now_local().isoformat(),
      'timezone': get_setting('timezone'),
  })


@app.route('/api/screenshot', methods=['POST'])
def save_screenshot():
  data = request.get_json(force=True, silent=True) or {}
  image_data = data.get('image', '')
  channel_name = data.get('channel', '')

  if not isinstance(image_data, str) or not image_data.startswith('data:image/png;base64,'):
    return jsonify({'error': 'Μη έγκυρα δεδομένα εικόνας.'}), 400

  try:
    raw_bytes = base64.b64decode(image_data.split(',', 1)[1], validate=True)
  except Exception:
    return jsonify({'error': 'Μη έγκυρα δεδομένα εικόνας.'}), 400

  if len(raw_bytes) > 25 * 1024 * 1024:  # 25MB ασφάλεια, ένα καρέ δεν φτάνει ποτέ εκεί
    return jsonify({'error': 'Πολύ μεγάλη εικόνα.'}), 400

  recordings_dir = get_recordings_dir()
  if not os.path.exists(recordings_dir):
    os.makedirs(recordings_dir)

  channel_part = sanitize_filename_part(channel_name) or 'Κανάλι'
  now = now_local()
  date_part = now.strftime('%Y-%m-%d')
  time_part = now.strftime('%Hh%M-%S')
  filename = f'{date_part} {time_part} - {channel_part} - Στιγμιότυπο.png'
  filepath = os.path.join(recordings_dir, filename)

  try:
    with open(filepath, 'wb') as f:
      f.write(raw_bytes)
  except Exception as e:
    return jsonify({'error': f'Αποτυχία αποθήκευσης: {e}'}), 500

  return jsonify({'success': True, 'filename': filename})


# ---------------------------------------------------------------------------
# Εγγραφές
# ---------------------------------------------------------------------------

@app.route('/api/schedule', methods=['POST'])
@requires_auth
def schedule_rec():
  data = request.json or {}
  stream_url = data.get('url')
  channel_name = data.get('name') or 'recording'
  start_time_str = data.get('start_time')
  channel_id = data.get('channel_id')
  program_title = data.get('program')
  recurrence = data.get('recurrence') or 'none'
  try:
    pre_pad_sec = max(0, int(data.get('pre_pad_sec', 0)))
  except (TypeError, ValueError):
    pre_pad_sec = 0
  try:
    post_pad_sec = max(0, int(data.get('post_pad_sec', 0)))
  except (TypeError, ValueError):
    post_pad_sec = 0

  if recurrence not in RECURRENCE_LABELS:
    return jsonify({'status': 'error', 'message': 'Μη έγκυρη επιλογή επανάληψης.'}), 400

  if not is_valid_stream_url(stream_url):
    return jsonify({
        'status': 'error',
        'message': 'Μη έγκυρο stream URL (πρέπει να ξεκινά με http:// ή https://).',
    }), 400

  try:
    duration_sec = int(data.get('duration', 3600))
  except (TypeError, ValueError):
    return jsonify({'status': 'error', 'message': 'Μη έγκυρη διάρκεια.'}), 400
  original_duration_sec = duration_sec

  max_duration_sec = (get_setting('max_duration_hours') or 6) * 3600
  if duration_sec <= 0 or duration_sec > max_duration_sec:
    return jsonify({
        'status': 'error',
        'message': f'Η διάρκεια πρέπει να είναι μεταξύ 1 και {max_duration_sec // 3600} ωρών.',
    }), 400

  try:
    start_dt = parse_local_datetime(start_time_str)
  except Exception as e:
    return jsonify({'status': 'error', 'message': f'Μη έγκυρη ημερομηνία/ώρα: {e}'}), 400

  ok, duration_sec = resolve_recording_conflict(channel_id, start_dt, duration_sec, pre_pad_sec, post_pad_sec)
  if not ok:
    return jsonify({
        'status': 'error',
        'message': f'Υπάρχει ήδη εγγραφή (ενεργή ή προγραμματισμένη) που επικαλύπτεται σε αυτό το κανάλι («{channel_name}») — αποφυγή διπλής εγγραφής.',
    }), 409
  trimmed_for_adjacent = duration_sec < original_duration_sec

  try:
    delay = (start_dt - now_local()).total_seconds()

    rec_id = uuid.uuid4().hex[:12]
    rec = {
        'id': rec_id,
        'name': channel_name,
        'url': stream_url,
        'channel_id': channel_id,
        'program': program_title,
        'start_time': start_dt.isoformat(),
        'duration': duration_sec,
        'pre_pad_sec': pre_pad_sec,
        'post_pad_sec': post_pad_sec,
        'status': 'scheduled',
        'filename': None,
        'recurrence': recurrence,
        'series_id': rec_id,
    }

    with recordings_lock:
      recordings[rec_id] = rec
      arm_recording(rec)
    save_recordings_state()

    if delay <= 0:
      message = 'Η εγγραφή ξεκίνησε αμέσως!'
    else:
      message = f'Η εγγραφή προγραμματίστηκε για τις {start_time_str}'
      if recurrence != 'none':
        message += f' ({RECURRENCE_LABELS[recurrence]})'
    if trimmed_for_adjacent:
      message += ' (το padding περικόπηκε αυτόματα ώστε να μην επικαλύπτεται με τη διπλανή εγγραφή στο ίδιο κανάλι)'

    return jsonify({'status': 'success', 'id': rec_id, 'message': message})

  except Exception as e:
    return jsonify({'status': 'error', 'message': str(e)}), 400


@app.route('/api/recordings', methods=['GET'])
def list_recordings():
  with recordings_lock:
    out = []
    for rec in recordings.values():
      out.append({
          'id': rec['id'],
          'name': rec['name'],
          'channel_id': rec.get('channel_id'),
          'program': rec.get('program'),
          'start_time': rec['start_time'],
          'duration': rec['duration'],
          'status': rec['status'],
          'filename': rec.get('filename'),
          'error': rec.get('error'),
          'recurrence': rec.get('recurrence', 'none'),
      })
  out.sort(key=lambda r: r['start_time'], reverse=True)
  return jsonify(out)


@app.route('/api/schedule/<rec_id>', methods=['DELETE'])
@requires_auth
def cancel_recording(rec_id):
  with recordings_lock:
    rec = recordings.get(rec_id)
    if not rec:
      return jsonify({'status': 'error', 'message': 'Άγνωστη εγγραφή.'}), 404

    if rec['status'] == 'scheduled':
      timer = rec.get('timer')
      if timer:
        timer.cancel()
      rec['status'] = 'cancelled'
    elif rec['status'] == 'recording':
      proc = rec.get('process')
      if proc:
        proc.terminate()
      rec['status'] = 'cancelled'
    else:
      return jsonify({
          'status': 'error',
          'message': f"Η εγγραφή είναι ήδη '{rec['status']}' και δεν μπορεί να ακυρωθεί.",
      }), 400

  save_recordings_state()
  return jsonify({'status': 'success', 'message': 'Η εγγραφή ακυρώθηκε.'})


def _delete_one_recording(rec_id):
  """Διαγράφει μία ολοκληρωμένη/ακυρωμένη/σε σφάλμα εγγραφή: το αρχείο (αν υπάρχει)
  και την καταχώρηση. Επιστρέφει (ok: bool, message: str). ΔΕΝ διαγράφει εγγραφές
  που είναι ακόμα 'scheduled' ή 'recording' — γι' αυτές χρησιμοποιείται η ακύρωση."""
  with recordings_lock:
    rec = recordings.get(rec_id)
    if not rec:
      return False, 'Άγνωστη εγγραφή.'
    if rec['status'] in ('scheduled', 'recording'):
      return False, 'Χρησιμοποίησε ακύρωση για ενεργές/προγραμματισμένες εγγραφές.'
    filename = rec.get('filename')
    if filename:
      filepath = os.path.join(get_recordings_dir(), filename)
      if os.path.isfile(filepath):
        try:
          os.remove(filepath)
        except OSError as e:
          return False, f'Δεν ήταν δυνατή η διαγραφή του αρχείου: {e}'
    del recordings[rec_id]
  return True, 'Διαγράφηκε.'


@app.route('/api/recordings/<rec_id>', methods=['DELETE'])
@requires_auth
def delete_recording(rec_id):
  ok, msg = _delete_one_recording(rec_id)
  if not ok:
    return jsonify({'status': 'error', 'message': msg}), 400
  save_recordings_state()
  return jsonify({'status': 'success', 'message': msg})


@app.route('/api/recordings/bulk_delete', methods=['POST'])
@requires_auth
def bulk_delete_recordings():
  data = request.json or {}
  ids = data.get('ids') or []
  if not isinstance(ids, list) or not ids:
    return jsonify({'status': 'error', 'message': 'Δεν δόθηκαν εγγραφές για διαγραφή.'}), 400
  deleted, failed = [], []
  for rec_id in ids:
    ok, msg = _delete_one_recording(rec_id)
    (deleted if ok else failed).append({'id': rec_id, 'message': msg})
  save_recordings_state()
  return jsonify({
      'status': 'success' if deleted else 'error',
      'deleted': deleted,
      'failed': failed,
      'message': f'Διαγράφηκαν {len(deleted)} από {len(ids)}.',
  })


@app.route('/api/recordings/bulk_download', methods=['POST'])
@requires_auth
def bulk_download_recordings():
  """Κατεβάζει πολλαπλές ολοκληρωμένες εγγραφές μαζί, σαν ένα .zip."""
  data = request.json or {}
  ids = data.get('ids') or []
  if not isinstance(ids, list) or not ids:
    return jsonify({'status': 'error', 'message': 'Δεν δόθηκαν εγγραφές.'}), 400

  buf = io.BytesIO()
  included = 0
  with recordings_lock:
    snapshot = {rid: dict(recordings[rid]) for rid in ids if rid in recordings}
  with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
    for rec in snapshot.values():
      filename = rec.get('filename')
      if not filename:
        continue
      filepath = os.path.join(get_recordings_dir(), filename)
      if os.path.isfile(filepath):
        zf.write(filepath, arcname=filename)
        included += 1

  if included == 0:
    return jsonify({'status': 'error', 'message': 'Κανένα από τα επιλεγμένα αρχεία δεν βρέθηκε στο δίσκο.'}), 404

  buf.seek(0)
  ts = datetime.now().strftime('%Y-%m-%d_%H%M')
  return Response(
      buf.read(),
      mimetype='application/zip',
      headers={'Content-Disposition': f'attachment; filename="recordings_{ts}.zip"'},
  )



@requires_auth
def download_recording(filename):
  # ΠΡΟΣΟΧΗ: τα ονόματα αρχείων εγγραφής περιέχουν ελληνικούς χαρακτήρες (π.χ.
  # "ERT1 HD - Δελτίο Ειδήσεων - ...ts"), αφού φτιάχνονται από το όνομα καναλιού
  # και τίτλο εκπομπής (βλ. sanitize_filename_part / run_recording). Το παλιό
  # secure_filename() πετάει ΟΛΟΥΣ τους μη-ASCII χαρακτήρες, οπότε το ζητούμενο
  # όνομα δεν ταίριαζε ΠΟΤΕ με το πραγματικό αρχείο στο δίσκο και η λήψη έδινε
  # πάντα 404. Εδώ κρατάμε το unicode όνομα ως έχει και απλώς αποτρέπουμε
  # path traversal (π.χ. "../../etc/passwd") ελέγχοντας ότι πρόκειται όντως για
  # απλό όνομα αρχείου μέσα στον (ρυθμιζόμενο) φάκελο εγγραφών.
  basename = os.path.basename(filename)
  if not basename or basename in ('.', '..') or basename != filename:
    return 'Μη έγκυρο όνομα αρχείου.', 400

  recordings_dir_abs = os.path.abspath(get_recordings_dir())
  filepath = os.path.abspath(os.path.join(recordings_dir_abs, basename))
  if os.path.commonpath([recordings_dir_abs, filepath]) != recordings_dir_abs:
    return 'Μη έγκυρο όνομα αρχείου.', 400

  if not os.path.isfile(filepath):
    return 'Δεν βρέθηκε το αρχείο.', 404
  return send_from_directory(get_recordings_dir(), basename, as_attachment=True)


if __name__ == '__main__':
  update_epg()
  with _epg_state_lock:
    _epg_state['last_attempt'] = time.time()
  load_recordings_state()

  threading.Thread(target=epg_refresh_loop, daemon=True).start()
  threading.Thread(target=cleanup_loop, daemon=True).start()

  debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
  app.run(host='0.0.0.0', port=5000, debug=debug_mode, threaded=True)
