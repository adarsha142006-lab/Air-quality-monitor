"""
AirQuali IoT Dashboard — Production Backend
Features: AQI Engine, ML Predictions, Anomaly Detection, Telegram Alerts,
          Multi-Device, PDF Reports, Analytics API, Data Persistence, ngrok
"""

# ── Imports ───────────────────────────────────────────────────────────────────
from flask import Flask, request, jsonify, send_from_directory, Response, redirect
from flask_cors import CORS
from datetime import datetime, timedelta, date
from collections import deque, defaultdict
import csv, os, json, threading, time, io

import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.ensemble import IsolationForest
from sklearn.metrics import r2_score
import requests as http_req       # Telegram Bot API calls

# Optional: reportlab for PDF reports
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas as _pdf_canvas
    from reportlab.lib.units import inch
    REPORTLAB = True
except ImportError:
    REPORTLAB = False
    print("[WARN] reportlab not installed — PDF reports disabled. Run: pip install reportlab")

# Optional: pyngrok for public URL tunnelling
try:
    from pyngrok import ngrok as _ngrok, conf as _ngrok_conf
    PYNGROK = True
except ImportError:
    PYNGROK = False

app  = Flask(__name__)
CORS(app)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
CONFIG_PATH = 'config.json'
_DEFAULT_CFG = {
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "alerts":   {"temperature_threshold": 35, "aqi_threshold": 150, "cooldown_seconds": 60},
    "security": {"clear_key": "changeme123"},
    "ngrok":    {"enabled": False, "auth_token": ""}
}

def _load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(_DEFAULT_CFG, f, indent=2)
        return _DEFAULT_CFG

cfg = _load_config()

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS & STATE
# ═══════════════════════════════════════════════════════════════════════════════
HISTORY_MAX     = 100
RETRAIN_EVERY   = 20
PREDICT_STEPS   = 5
DEFAULT_DEVICE  = 'esp32_main'
CSV_PATH        = 'sensor_log.csv'
CSV_HEADERS     = ['datetime','device_id','temperature','humidity',
                   'air_quality','gas_status','aqi','aqi_category','anomaly']
REPORTS_DIR     = 'reports'
MAX_ALERTS      = 20

# Per-device history (device_id → deque of readings)
device_history          = defaultdict(lambda: deque(maxlen=HISTORY_MAX))
# Per-device ML models
device_ml               = defaultdict(lambda: dict(
    temp_model=None, hum_model=None, aq_model=None, iso_forest=None,
    r2_temp=None, r2_hum=None, r2_aq=None, train_size=0, last_retrain=None))
device_retrain_counter  = defaultdict(int)
model_lock              = threading.Lock()
csv_lock                = threading.Lock()

# Alert log (shared across all devices)
alerts_log    = deque(maxlen=MAX_ALERTS)
alert_cooldown = {}          # alert_type_key → last triggered epoch

public_url    = None         # set by ngrok thread
server_start  = datetime.now()

# Thresholds from config
TEMP_THRESH  = cfg['alerts']['temperature_threshold']
AQI_THRESH   = cfg['alerts']['aqi_threshold']
COOLDOWN_SEC = cfg['alerts']['cooldown_seconds']

os.makedirs(REPORTS_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════════
# AQI ENGINE  (ADC 0–4095 → AQI 0–500)
# ═══════════════════════════════════════════════════════════════════════════════
_AQI_BP = [   # (adc_lo, adc_hi, aqi_lo, aqi_hi, category, color)
    (   0,  819,   0,  50, 'Good',                    '#00e400'),
    ( 820, 1638,  51, 100, 'Moderate',                '#ffff00'),
    (1639, 2458, 101, 150, 'Unhealthy for Sensitive', '#ff7e00'),
    (2459, 3277, 151, 200, 'Unhealthy',               '#ff0000'),
    (3278, 3686, 201, 300, 'Very Unhealthy',           '#8f3f97'),
    (3687, 4095, 301, 500, 'Hazardous',               '#7e0023'),
]

def adc_to_aqi(adc):
    """Convert raw MQ135 ADC (0–4095) to AQI score + category + hex color."""
    adc = max(0, min(4095, int(float(adc or 0))))
    for lo, hi, qlo, qhi, cat, col in _AQI_BP:
        if lo <= adc <= hi:
            q = qlo + (adc - lo) * (qhi - qlo) / (hi - lo)
            return round(q), cat, col
    return 500, 'Hazardous', '#7e0023'

# ═══════════════════════════════════════════════════════════════════════════════
# CSV HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_csv():
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, 'w', newline='') as f:
            csv.writer(f).writerow(CSV_HEADERS)

def append_csv(row: dict):
    with csv_lock:
        with open(CSV_PATH, 'a', newline='') as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction='ignore').writerow(row)

# ═══════════════════════════════════════════════════════════════════════════════
# ML ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
def _fit_1d(vals):
    n = len(vals)
    X = np.arange(n).reshape(-1, 1)
    y = np.array(vals, dtype=float)
    m = LinearRegression().fit(X, y)
    return m, float(r2_score(y, m.predict(X)))

def retrain_models(device_id):
    data = list(device_history[device_id])
    if len(data) < 5:
        return
    try:
        temps = [float(d['temperature']) for d in data]
        hums  = [float(d['humidity'])    for d in data]
        aqs   = [float(d['air_quality']) for d in data]
        tm, r2t = _fit_1d(temps)
        hm, r2h = _fit_1d(hums)
        am, r2a = _fit_1d(aqs)
        iso = IsolationForest(contamination=0.05, random_state=42).fit(
            np.column_stack([temps, hums, aqs]))
        with model_lock:
            ml = device_ml[device_id]
            ml.update(temp_model=tm, hum_model=hm, aq_model=am, iso_forest=iso,
                      r2_temp=round(r2t,4), r2_hum=round(r2h,4), r2_aq=round(r2a,4),
                      train_size=len(data),
                      last_retrain=datetime.now().strftime('%H:%M:%S'))
        print(f"[ML:{device_id}] Retrained {len(data)} pts R2 T:{r2t:.3f} H:{r2h:.3f} AQ:{r2a:.3f}")
    except Exception as e:
        print(f"[ML:{device_id}] Error: {e}")

def is_anomaly(device_id, reading):
    with model_lock:
        iso = device_ml[device_id]['iso_forest']
    if iso is None:
        return False
    X = np.array([[float(reading.get('temperature', 0)),
                   float(reading.get('humidity', 0)),
                   float(reading.get('air_quality', 0))]])
    return bool(iso.predict(X)[0] == -1)

def predict_next(device_id):
    with model_lock:
        ml = device_ml[device_id]
        tm, hm, am = ml['temp_model'], ml['hum_model'], ml['aq_model']
    n = len(device_history[device_id])
    def _p(m):
        if m is None: return [None] * PREDICT_STEPS
        X = np.arange(n, n + PREDICT_STEPS).reshape(-1, 1)
        return [round(float(v), 2) for v in m.predict(X)]
    return _p(tm), _p(hm), _p(am)

# ═══════════════════════════════════════════════════════════════════════════════
# ALERT SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════
def _cooldown_ok(key):
    now = time.time()
    if now - alert_cooldown.get(key, 0) >= COOLDOWN_SEC:
        alert_cooldown[key] = now
        return True
    return False

def _send_telegram(msg):
    tg = cfg.get('telegram', {})
    if not tg.get('enabled'):
        return
    try:
        http_req.post(
            f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
            json={'chat_id': tg['chat_id'], 'text': msg, 'parse_mode': 'HTML'},
            timeout=6)
    except Exception as e:
        print(f"[Telegram] {e}")

def trigger_alert(atype, severity, message, data):
    entry = {
        'timestamp':    datetime.now().strftime('%H:%M:%S'),
        'date':         datetime.now().strftime('%Y-%m-%d'),
        'type':         atype,
        'severity':     severity,
        'message':      message,
        'temperature':  data.get('temperature'),
        'humidity':     data.get('humidity'),
        'aqi':          data.get('aqi'),
        'aqi_category': data.get('aqi_category'),
        'gas_status':   data.get('gas_status'),
    }
    alerts_log.append(entry)
    icon = {'WARNING': '⚠️', 'DANGER': '🚨', 'CRITICAL': '🔴'}.get(severity, '⚠️')
    tg_msg = (f"{icon} <b>{severity}: {message}</b>\n"
              f"🕐 {entry['date']} {entry['timestamp']}\n"
              f"🌡️ Temp: {data.get('temperature')}°C  💧 Hum: {data.get('humidity')}%\n"
              f"🫧 AQI: {data.get('aqi')} ({data.get('aqi_category')})  🛡️ Gas: {data.get('gas_status')}")
    threading.Thread(target=_send_telegram, args=(tg_msg,), daemon=True).start()

def evaluate_alerts(data):
    temp = float(data.get('temperature', 0))
    aqi  = int(data.get('aqi', 0))
    gas  = data.get('gas_status', 'Safe')
    anom = data.get('anomaly', False)
    if gas != 'Safe'       and _cooldown_ok('gas'):       trigger_alert('gas',         'DANGER',   'Gas / Smoke Detected!',        data)
    if temp > TEMP_THRESH  and _cooldown_ok('temp'):      trigger_alert('temperature', 'WARNING',  f'High Temp: {temp:.1f}°C',     data)
    if aqi  > 200          and _cooldown_ok('aqi_crit'):  trigger_alert('aqi',         'CRITICAL', f'AQI Critical: {aqi}',         data)
    elif aqi > AQI_THRESH  and _cooldown_ok('aqi'):       trigger_alert('aqi',         'WARNING',  f'AQI Elevated: {aqi}',         data)
    if anom                and _cooldown_ok('anomaly'):   trigger_alert('anomaly',     'WARNING',  'Sensor Anomaly Detected',      data)

# ═══════════════════════════════════════════════════════════════════════════════
# PDF REPORT
# ═══════════════════════════════════════════════════════════════════════════════
def generate_pdf_report(target_date=None):
    if not REPORTLAB:
        return None
    if target_date is None:
        target_date = date.today()
    ds  = target_date.strftime('%Y-%m-%d')
    out = os.path.join(REPORTS_DIR, f'report_{ds}.pdf')
    rows = []
    try:
        with open(CSV_PATH, newline='') as f:
            for r in csv.DictReader(f):
                if r.get('datetime', '').startswith(ds):
                    rows.append(r)
    except FileNotFoundError:
        pass

    def _sf(key):
        return [float(r[key]) for r in rows if r.get(key)]

    temps = _sf('temperature'); hums = _sf('humidity')
    aqs   = _sf('air_quality'); aqis = _sf('aqi')
    n_anom  = sum(1 for r in rows if r.get('anomaly','').lower() == 'true')
    n_gas   = sum(1 for r in rows if r.get('gas_status','Safe') != 'Safe')

    c = _pdf_canvas.Canvas(out, pagesize=letter)
    w, h = letter
    # Header bar
    c.setFillColorRGB(0.24, 0.25, 0.95); c.rect(0, h-80, w, 80, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1); c.setFont('Helvetica-Bold', 22)
    c.drawString(0.75*inch, h-42, 'AirQuali IoT — Daily Sensor Report')
    c.setFont('Helvetica', 11)
    c.drawString(0.75*inch, h-62, f'Date: {ds}  |  Readings: {len(rows)}  |  Generated: {datetime.now().strftime("%H:%M:%S")}')

    def _section(title, y):
        c.setFillColorRGB(0.24,0.25,0.95); c.setFont('Helvetica-Bold',13)
        c.drawString(0.75*inch, y, title)
        c.setFillColorRGB(0.7,0.7,0.7); c.line(0.75*inch, y-4, w-0.75*inch, y-4)

    def _stat(label, vals, unit, y):
        c.setFont('Helvetica',11); c.setFillColorRGB(0.15,0.15,0.15)
        txt = (f'{label}: Min {min(vals):.1f}{unit}  |  Max {max(vals):.1f}{unit}  |  Avg {sum(vals)/len(vals):.1f}{unit}'
               if vals else f'{label}: No data')
        c.drawString(0.75*inch, y, txt)

    _section('Sensor Statistics', h-1.3*inch)
    _stat('Temperature', temps, ' °C', h-1.65*inch)
    _stat('Humidity',    hums,  ' %',  h-1.95*inch)
    _stat('Air Quality (ADC)', aqs, '', h-2.25*inch)
    _stat('AQI',         aqis,  '',   h-2.55*inch)
    _section('Events', h-3.0*inch)
    c.setFont('Helvetica',11)
    c.setFillColorRGB(0.7,0.1,0.1) if n_anom else c.setFillColorRGB(0.1,0.5,0.1)
    c.drawString(0.75*inch, h-3.35*inch, f'Anomaly Detections: {n_anom}')
    c.setFillColorRGB(0.7,0.3,0.0) if n_gas else c.setFillColorRGB(0.1,0.5,0.1)
    c.drawString(0.75*inch, h-3.65*inch, f'Gas Detection Events: {n_gas}')
    if aqis:
        avg_aqi = sum(aqis)/len(aqis)
        _, cat, _ = adc_to_aqi(int(avg_aqi/500*4095))
        c.setFillColorRGB(0.1,0.1,0.1)
        c.drawString(0.75*inch, h-3.95*inch, f'Average AQI: {avg_aqi:.0f} — {cat}')
    c.setFont('Helvetica',9); c.setFillColorRGB(0.5,0.5,0.5)
    c.drawString(0.75*inch, 0.5*inch, 'Generated by AirQuali IoT Dashboard')
    c.save()
    print(f'[PDF] Saved {out}')
    return out

def _pdf_scheduler():
    while True:
        now = datetime.now()
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        time.sleep((nxt - now).total_seconds())
        generate_pdf_report(date.today() - timedelta(days=1))

# ═══════════════════════════════════════════════════════════════════════════════
# ANALYTICS DATA
# ═══════════════════════════════════════════════════════════════════════════════
def get_analytics_data(device_id, days):
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d') if days < 9999 else '0000-00-00'
    rows = []
    try:
        with open(CSV_PATH, newline='') as f:
            for r in csv.DictReader(f):
                dev = r.get('device_id', DEFAULT_DEVICE) or DEFAULT_DEVICE
                if dev == device_id and r.get('datetime','')[:10] >= cutoff:
                    rows.append(r)
    except FileNotFoundError:
        pass

    daily   = defaultdict(lambda: dict(temps=[], hums=[], aqs=[], aqis=[], anomalies=0, gas_events=0))
    hourmap = defaultdict(list)   # (date_str, hour) → [temps]

    for r in rows:
        dt = r.get('datetime', '')
        ds = dt[:10]
        hr = int(dt[11:13]) if len(dt) > 12 else 0
        try:
            if r.get('temperature'): daily[ds]['temps'].append(float(r['temperature']))
            if r.get('humidity'):    daily[ds]['hums'].append(float(r['humidity']))
            if r.get('air_quality'): daily[ds]['aqs'].append(float(r['air_quality']))
            if r.get('aqi'):         daily[ds]['aqis'].append(float(r['aqi']))
            if r.get('anomaly','false').lower() == 'true': daily[ds]['anomalies'] += 1
            if r.get('gas_status','Safe') != 'Safe':       daily[ds]['gas_events'] += 1
            if r.get('temperature'): hourmap[(ds,hr)].append(float(r['temperature']))
        except Exception:
            pass

    def _agg(vals):
        return {'min':round(min(vals),1),'max':round(max(vals),1),'avg':round(sum(vals)/len(vals),1)} if vals else None

    daily_agg = [{'date':ds,'temp':_agg(v['temps']),'hum':_agg(v['hums']),'aq':_agg(v['aqs']),
                  'aqi':_agg(v['aqis']),'anomalies':v['anomalies'],'gas_events':v['gas_events'],
                  'count':len(v['temps'])} for ds in sorted(daily)]

    dates   = sorted({k[0] for k in hourmap})[-7:]
    heatmap = [{'date':d, 'hours':[round(sum(hourmap[(d,h)])/len(hourmap[(d,h)]),1)
                                   if hourmap.get((d,h)) else None for h in range(24)]}
               for d in dates]

    n_total = len(rows)
    n_anom  = sum(v['anomalies'] for v in daily.values())
    n_gas   = sum(v['gas_events'] for v in daily.values())
    return {
        'daily':   daily_agg,
        'heatmap': heatmap,
        'stats': {
            'total_readings': n_total,
            'anomaly_count':  n_anom,
            'anomaly_pct':    round(n_anom/n_total*100, 1) if n_total else 0,
            'gas_events':     n_gas,
            'uptime':         str(datetime.now()-server_start).split('.')[0],
        }
    }

# ═══════════════════════════════════════════════════════════════════════════════
# NGROK SETUP
# ═══════════════════════════════════════════════════════════════════════════════
def _setup_ngrok():
    global public_url
    ngrok_cfg = cfg.get('ngrok', {})
    if not PYNGROK or not ngrok_cfg.get('enabled'):
        return
    try:
        token = ngrok_cfg.get('auth_token', '')
        if token:
            _ngrok_conf.get_default().auth_token = token
        tunnel    = _ngrok.connect(5000)
        public_url = tunnel.public_url
        print(f'[ngrok] Public URL: {public_url}')
    except Exception as e:
        print(f'[ngrok] Error: {e}')

# ═══════════════════════════════════════════════════════════════════════════════
# BOOT — pre-load CSV history and warm ML models
# ═══════════════════════════════════════════════════════════════════════════════
ensure_csv()
try:
    with open(CSV_PATH, newline='') as _f:
        _rows = list(csv.DictReader(_f))
    _by_device = defaultdict(list)
    for _r in _rows:
        _by_device[_r.get('device_id', DEFAULT_DEVICE) or DEFAULT_DEVICE].append(_r)
    for _dev, _drows in _by_device.items():
        for _r in _drows[-HISTORY_MAX:]:
            _aqi, _aqi_cat, _aqi_col = adc_to_aqi(_r.get('air_quality', 0))
            device_history[_dev].append({
                'timestamp':    _r['datetime'].split(' ')[-1] if ' ' in _r['datetime'] else _r['datetime'],
                'device_id':    _dev,
                'temperature':  _r.get('temperature'),
                'humidity':     _r.get('humidity'),
                'air_quality':  _r.get('air_quality'),
                'gas_status':   _r.get('gas_status'),
                'aqi':          int(float(_r.get('aqi', _aqi) or _aqi)),
                'aqi_category': _r.get('aqi_category', _aqi_cat),
                'aqi_color':    _aqi_col,
                'anomaly':      _r.get('anomaly','false').lower() == 'true',
            })
        if len(device_history[_dev]) >= 5:
            retrain_models(_dev)
    _n = sum(len(v) for v in device_history.values())
    print(f'[Boot] Loaded {_n} readings. Devices: {list(device_history.keys())}')
except Exception as _e:
    print(f'[Boot] CSV pre-load skipped: {_e}')

threading.Thread(target=_pdf_scheduler,  daemon=True).start()
threading.Thread(target=_setup_ngrok,    daemon=True).start()

# ═══════════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/data', methods=['POST'])
def receive_data():
    data      = request.get_json(force=True)
    device_id = data.get('device_id', DEFAULT_DEVICE) or DEFAULT_DEVICE
    data['device_id']  = device_id
    data['timestamp']  = datetime.now().strftime('%H:%M:%S')

    aqi, aqi_cat, aqi_col = adc_to_aqi(data.get('air_quality', 0))
    data.update(aqi=aqi, aqi_category=aqi_cat, aqi_color=aqi_col)

    anomaly = is_anomaly(device_id, data)
    data['anomaly'] = anomaly

    device_history[device_id].append(data)
    append_csv({'datetime':datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'device_id':device_id, 'temperature':data.get('temperature'),
                'humidity':data.get('humidity'), 'air_quality':data.get('air_quality'),
                'gas_status':data.get('gas_status'), 'aqi':aqi,
                'aqi_category':aqi_cat, 'anomaly':anomaly})

    device_retrain_counter[device_id] += 1
    if device_retrain_counter[device_id] >= RETRAIN_EVERY:
        device_retrain_counter[device_id] = 0
        threading.Thread(target=retrain_models, args=(device_id,), daemon=True).start()

    threading.Thread(target=evaluate_alerts, args=(data,), daemon=True).start()

    print(f"[{device_id}|{data['timestamp']}] T:{data.get('temperature')}C "
          f"H:{data.get('humidity')}% AQI:{aqi}({aqi_cat}) Gas:{data.get('gas_status')} Anom:{anomaly}")
    return jsonify({'status':'ok','aqi':aqi,'aqi_category':aqi_cat,'anomaly':anomaly})


@app.route('/latest')
def get_latest():
    dev = request.args.get('device', DEFAULT_DEVICE)
    h   = device_history.get(dev)
    return jsonify(h[-1] if h else {})


@app.route('/history')
def get_history():
    dev = request.args.get('device', DEFAULT_DEVICE)
    return jsonify(list(device_history.get(dev, [])))


@app.route('/predict')
def get_predict():
    dev     = request.args.get('device', DEFAULT_DEVICE)
    t, h, a = predict_next(dev)
    with model_lock:
        ml = device_ml[dev]
        st = {k: ml[k] for k in ('train_size','last_retrain','r2_temp','r2_hum','r2_aq')}
    return jsonify({'temperature':t,'humidity':h,'air_quality':a,
                    'intervals_ahead':list(range(1,PREDICT_STEPS+1)),**st})


@app.route('/anomalies')
def get_anomalies():
    dev = request.args.get('device', DEFAULT_DEVICE)
    return jsonify([d for d in device_history.get(dev,[]) if d.get('anomaly')])


@app.route('/alerts')
def get_alerts():
    return jsonify(list(alerts_log))


@app.route('/devices')
def get_devices():
    out = []
    for dev, h in device_history.items():
        if h: out.append({'device_id':dev,'readings':len(h),
                          'last_seen':h[-1].get('timestamp'),'latest':h[-1]})
    return jsonify(out)


@app.route('/analytics-data')
def analytics_data_route():
    dev  = request.args.get('device', DEFAULT_DEVICE)
    days = request.args.get('days', '7')
    days = 9999 if days == 'all' else (int(days) if days.isdigit() else 7)
    return jsonify(get_analytics_data(dev, days))


@app.route('/export')
def export_csv():
    try:
        with open(CSV_PATH) as f:
            content = f.read()
    except FileNotFoundError:
        return jsonify({'error': 'No data logged yet.'}), 404
    fn = f"sensor_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(content, mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{fn}"'})


@app.route('/clear', methods=['POST'])
def clear_history():
    body = request.get_json(force=True) or {}
    if body.get('key') != cfg.get('security',{}).get('clear_key',''):
        return jsonify({'error':'Unauthorized'}), 403
    dev = body.get('device', DEFAULT_DEVICE)
    device_history[dev].clear()
    return jsonify({'status':'cleared','device':dev})


@app.route('/reports')
def list_reports():
    files = sorted((f for f in os.listdir(REPORTS_DIR) if f.endswith('.pdf')), reverse=True)
    return jsonify([{'filename':f,'url':f'/reports/{f}'} for f in files])


@app.route('/reports/<filename>')
def download_report(filename):
    return send_from_directory(REPORTS_DIR, filename, as_attachment=True)


@app.route('/generate-report', methods=['POST'])
def trigger_report():
    if not REPORTLAB:
        return jsonify({'error':'reportlab not installed'}), 500
    path = generate_pdf_report(date.today())
    fn   = os.path.basename(path) if path else ''
    return jsonify({'status':'ok','filename':fn,'url':f'/reports/{fn}'})


@app.route('/public-url')
def get_public_url():
    return jsonify({'url': public_url or request.host_url.rstrip('/')})


@app.route('/')
def dashboard():
    return send_from_directory('static', 'index.html')


@app.route('/analytics')
def analytics_page():
    return send_from_directory('static', 'analytics.html')


# ── Dynamic PWA icon generation ───────────────────────────────────────────────
_ICON_SIZES = {72, 96, 128, 144, 152, 192, 384, 512}
try:
    import cairosvg as _cairosvg
    _SVG_RASTER = True
except ImportError:
    _SVG_RASTER = False


@app.route('/icon-<int:size>.png')
def serve_icon(size):
    if size not in _ICON_SIZES:
        return ('Not Found', 404)
    if _SVG_RASTER:
        svg_path = os.path.join('static', 'icon.svg')
        try:
            png_bytes = _cairosvg.svg2png(url=svg_path, output_width=size, output_height=size)
            return Response(png_bytes, mimetype='image/png')
        except Exception as e:
            print(f'[Icon] cairosvg error: {e}')
    return redirect('/icon.svg', code=302)


@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)


if __name__ == '__main__':
    # Retrieve port from environment variables (e.g. Railway) or fallback to 5000
    port = int(os.environ.get('PORT', 5000))
    print(f'Server running -> open http://localhost:{port} in your browser')
    app.run(host='0.0.0.0', port=port, debug=True)