"""Constants for the Aiwit / EKEN integration."""

DOMAIN = "aiwit"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_UDID = "udid"
# Vendor client keys: NOT shipped with this repository. The user extracts them
# from the official app with tools/extract_keys.py and enters them at setup.
CONF_LOGIN_SIGN_KEY = "login_sign_key"
CONF_SOCKET_KEY = "socket_key"
CONF_THUMBNAIL_PATH = "thumbnail_path"

DEFAULT_NAME = "Aiwit Doorbell"
DEFAULT_SCAN_INTERVAL = 60  # not used for push events, only for reconnect watchdog

# Binary sensor keys
SENSOR_RING = "ring"
SENSOR_MOTION = "motion"
SENSOR_CHARGING = "charging"

# Sensor keys
SENSOR_BATTERY = "battery"
SENSOR_RSSI = "rssi"
SENSOR_ONLINE = "online"

# Event auto-off delay (seconds) — ring/motion stay ON briefly then OFF
RING_OFF_DELAY = 5
MOTION_OFF_DELAY = 30

# Délai avant d'aller chercher la vignette du clip après un event ring/PIR,
# pour laisser le temps au cloud de terminer l'upload (observé en capture
# réseau : l'upload OSS démarre juste après la fin de la session P2P).
THUMBNAIL_FETCH_DELAY = 10
THUMBNAIL_RETRY_DELAY = 5
THUMBNAIL_RETRIES = 16  # ~10s + 16x5s : le cloud publie parfois le clip tard

# --- Détection humaine (nn_sens / nn_label) -------------------------------
# Confirmé par désassemblage de l'app 2.6.2 (SetPIRForHumanDetection*Frag) :
#   * nn_sens  = curseur 15..50 (défaut app = 25), PAS un enum 0/1/5/10.
#     Le texte affiché dépend de la zone du curseur :
#         < 27      -> "Low Accuracy"      (plus de clips, batterie minimale)
#         27 .. 38  -> "Standard Accuracy"
#         >= 39     -> "High Accuracy"     (moins de clips, batterie maximale)
#   * nn_label = interrupteur 0/1 (détection humaine / alerte humaine).
NN_SENS_MIN = 15
NN_SENS_MAX = 50
NN_SENS_STANDARD_FROM = 27
NN_SENS_HIGH_FROM = 39
NN_SENS_LEVELS = {"Faible": 20, "Standard": 33, "Haut": 45}  # valeur écrite

# Événements / fichiers vignette
EVENT_AIWIT = "aiwit_event"
EVENT_THUMBNAIL = "aiwit_thumbnail_ready"
THUMBNAIL_WWW_DIR = "aiwit"  # <config>/www/aiwit -> /local/aiwit/...


def nn_sens_label(value):
    """Return Faible/Standard/Haut for a raw nn_sens value (None if unknown)."""
    if value is None or value < NN_SENS_MIN:
        # Valeurs 0/1/5/10 = anciennes ecritures erronees (<= 0.7.6) : inconnu
        return None
    if value < NN_SENS_STANDARD_FROM:
        return "Faible"
    if value < NN_SENS_HIGH_FROM:
        return "Standard"
    return "Haut"
