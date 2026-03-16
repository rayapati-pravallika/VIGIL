"""
VIGIL – Urban Waste Command Center  v4.0
FastAPI Backend — SQLite + WebSockets + YOLO bridge + GPS Reports

Install:
    pip install fastapi uvicorn pydantic websockets aiofiles

Run:
    uvicorn vigil_backend:app --reload --port 8000
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import random, asyncio, json, sqlite3, os
from datetime import datetime, timedelta

app = FastAPI(title="VIGIL API", version="4.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── SQLite ─────────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vigil.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS citizen_reports (
            id TEXT PRIMARY KEY, location TEXT, description TEXT,
            lat REAL, lng REAL, photo_url TEXT,
            reporter TEXT DEFAULT 'Anonymous',
            status TEXT DEFAULT 'PENDING', created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS litter_events (
            id TEXT PRIMARY KEY, location TEXT, object_type TEXT,
            lat REAL, lng REAL, confidence REAL,
            source TEXT DEFAULT 'auto', status TEXT DEFAULT 'UNRESOLVED', created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS detection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id TEXT, object_type TEXT, confidence REAL,
            source TEXT DEFAULT 'sim', created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS cleaning_ops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id TEXT, status TEXT,
            started_at TEXT, completed_at TEXT, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS yolo_detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_name TEXT, confidence REAL,
            x1 REAL, y1 REAL, x2 REAL, y2 REAL,
            frame_w INTEGER, frame_h INTEGER,
            source TEXT DEFAULT 'yolo', created_at TEXT
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ── WebSocket Manager ──────────────────────────────────────────────────────────
class WSManager:
    def __init__(self):
        self.clients: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)

    async def broadcast(self, payload: dict):
        dead = []
        msg  = json.dumps(payload)
        for ws in self.clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

ws_manager = WSManager()

# ── Static data ────────────────────────────────────────────────────────────────
LOCATIONS = [
    {"id":"bus_stop",         "name":"Bus Stop",         "lat":17.4400,"lng":78.4900},
    {"id":"market_street",    "name":"Market Street",    "lat":17.4450,"lng":78.4950},
    {"id":"food_street",      "name":"Food Street",      "lat":17.4350,"lng":78.4850},
    {"id":"public_park",      "name":"Public Park",      "lat":17.4480,"lng":78.4800},
    {"id":"college_entrance", "name":"College Entrance", "lat":17.4420,"lng":78.5000},
    {"id":"railway_station",  "name":"Railway Station",  "lat":17.4300,"lng":78.4950},
    {"id":"temple_road",      "name":"Temple Road",      "lat":17.4500,"lng":78.4870},
]
LOC_MAP = {l["id"]: l for l in LOCATIONS}

TRASH_TYPES = ["plastic_bottle","food_wrapper","paper_cup","paper_waste",
               "garbage_bag","food_container","cigarette_butt","plastic_bag","tin_can"]

TRASH_SOURCES = {
    "bus_stop":"Nearby Snack Stalls & Passenger Overflow",
    "market_street":"Street Vendors & Retail Packaging",
    "food_street":"Restaurant Takeaways & Food Carts",
    "public_park":"Recreational Activities & Picnic Waste",
    "college_entrance":"Student Footfall & Canteen Packaging",
    "railway_station":"Platform Food Vendors & Transit Passengers",
    "temple_road":"Devotional Offerings & Flower Waste",
}

BASE_TRASH = {"bus_stop":65,"market_street":78,"food_street":82,"public_park":40,
              "college_entrance":55,"railway_station":70,"temple_road":48}
BASE_CROWD = {"bus_stop":72,"market_street":60,"food_street":68,"public_park":35,
              "college_entrance":80,"railway_station":75,"temple_road":42}

INFRA_RECS = [
    {"location":"Food Street",     "type":"DUSTBIN",  "priority":"HIGH",   "reason":"Frequent food packaging waste near vendor cluster"},
    {"location":"Bus Stop",         "type":"DUSTBIN",  "priority":"HIGH",   "reason":"High passenger throughput – no bins within 20m"},
    {"location":"College Entrance", "type":"RECYCLING","priority":"MEDIUM", "reason":"High plastic/paper waste from student footfall"},
    {"location":"Railway Station",  "type":"COMPACTOR","priority":"HIGH",   "reason":"Peak-hour overflow requires compacting capacity"},
    {"location":"Market Street",    "type":"DUSTBIN",  "priority":"MEDIUM", "reason":"Vendor packaging accumulates between cycles"},
    {"location":"Public Park",      "type":"RECYCLING","priority":"LOW",    "reason":"Recreational waste suitable for sorted collection"},
]

# ── Simulation helpers ─────────────────────────────────────────────────────────
def hour_factor(h: int) -> float:
    return {8:0.7,9:0.9,12:1.0,13:1.0,17:0.9,18:1.0,19:1.0,20:0.8}.get(h, 0.35)

def sim_location(loc_id: str) -> dict:
    h  = datetime.now().hour
    hf = hour_factor(h)
    n  = random.uniform(0.85, 1.15)
    td = min(100, int(BASE_TRASH.get(loc_id,50)*hf*n))
    cd = min(100, int(BASE_CROWD.get(loc_id,50)*hf*n))
    cl = max(0, 100 - int(td*0.65 + cd*0.2 + random.randint(0,8)))
    if   td>60 and cd<40: rec,det = "CLEAN NOW","Deploy unit – low crowd window"
    elif cd>70:           rec,det = "STANDBY",  f"High crowd – schedule in {random.randint(20,55)} min"
    elif td>40:           rec,det = "MONITOR",  f"Accumulating – escalate in {random.randint(10,25)} min"
    else:                 rec,det = "CLEAR",    "Within acceptable thresholds"
    loc = LOC_MAP.get(loc_id, {})
    return {"id":loc_id,"name":loc.get("name",""),"lat":loc.get("lat",0),"lng":loc.get("lng",0),
            "trash":td,"crowd":cd,"cleanliness":cl,"recommendation":rec,"recDetail":det,
            "timestamp":datetime.now().isoformat()}

# ── Pydantic models ────────────────────────────────────────────────────────────
class CitizenReport(BaseModel):
    location: str; description: str
    lat: Optional[float] = None; lng: Optional[float] = None
    photo_url: Optional[str] = None; reporter: Optional[str] = "Anonymous"

class YoloDetection(BaseModel):
    class_name: str; confidence: float
    x1: float; y1: float; x2: float; y2: float
    frame_w: Optional[int] = 640; frame_h: Optional[int] = 480

class CleaningUpdate(BaseModel):
    location_id: str; status: str; notes: Optional[str] = None

# ── Endpoints ──────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status":"VIGIL v4.0 online","ws":"ws://localhost:8000/ws","docs":"/docs"}

@app.get("/locations")
def get_locations():
    return LOCATIONS

@app.get("/dashboard")
def get_dashboard():
    return {"locations":[sim_location(l["id"]) for l in LOCATIONS],
            "generated_at":datetime.now().isoformat()}

@app.get("/forecast")
def get_forecast():
    result = []
    for loc in LOCATIONS:
        for h_off in [1,3,6]:
            fh = (datetime.now()+timedelta(hours=h_off)).hour
            tp = min(100, int(random.uniform(45,90)*hour_factor(fh)))
            result.append({
                "location":loc["name"],"id":loc["id"],"hours":h_off,
                "time":(datetime.now()+timedelta(hours=h_off)).strftime("%H:%M"),
                "trash":tp,"severity":"HIGH" if tp>70 else "MEDIUM" if tp>45 else "LOW",
                "lat":loc["lat"],"lng":loc["lng"],
            })
    return {"forecasts":result}

@app.get("/infrastructure")
def get_infrastructure():
    return {"recommendations":INFRA_RECS}

@app.get("/waste-sources")
def get_waste_sources():
    return {"sources":[{
        "location":l["name"],
        "probable_source":TRASH_SOURCES.get(l["id"],"Unknown"),
        "primary_waste":random.choice(TRASH_TYPES),
        "trash_density":sim_location(l["id"])["trash"],
        "confidence":round(random.uniform(0.78,0.96),2),
    } for l in LOCATIONS]}

# ── Citizen Reports ────────────────────────────────────────────────────────────
@app.post("/citizen-reports")
async def submit_report(report: CitizenReport):
    rpt_id = f"RPT-{random.randint(1000,9999)}"
    now    = datetime.now().isoformat()
    conn   = get_db()
    conn.execute("INSERT INTO citizen_reports VALUES (?,?,?,?,?,?,?,?,?)",
        (rpt_id,report.location,report.description,report.lat,report.lng,
         report.photo_url,report.reporter,"PENDING",now))
    conn.commit(); conn.close()
    payload = {"type":"new_report","id":rpt_id,"location":report.location,
               "description":report.description,"lat":report.lat,"lng":report.lng,
               "reporter":report.reporter,"status":"PENDING","created_at":now}
    await ws_manager.broadcast(payload)
    return {"success":True,"report_id":rpt_id}

@app.get("/citizen-reports")
def list_reports():
    conn = get_db()
    rows = conn.execute("SELECT * FROM citizen_reports ORDER BY created_at DESC LIMIT 50").fetchall()
    conn.close()
    return {"reports":[dict(r) for r in rows]}

@app.patch("/citizen-reports/{report_id}/status")
async def update_report(report_id: str, status: str):
    conn = get_db()
    conn.execute("UPDATE citizen_reports SET status=? WHERE id=?",(status,report_id))
    conn.commit(); conn.close()
    await ws_manager.broadcast({"type":"report_updated","id":report_id,"status":status})
    return {"success":True}

# ── Litter Events ──────────────────────────────────────────────────────────────
@app.get("/litter-events")
def get_litter_events():
    conn = get_db()
    rows = conn.execute("SELECT * FROM litter_events ORDER BY created_at DESC LIMIT 40").fetchall()
    conn.close()
    db_events = [dict(r) for r in rows]
    while len(db_events) < 8:
        loc = random.choice(LOCATIONS)
        db_events.append({"id":f"EVT-{random.randint(1000,9999)}","location":loc["name"],
            "object_type":random.choice(TRASH_TYPES),"lat":loc["lat"],"lng":loc["lng"],
            "confidence":round(random.uniform(0.75,0.97),2),"source":"sim",
            "status":random.choice(["UNRESOLVED","UNRESOLVED","RESOLVED"]),
            "created_at":(datetime.now()-timedelta(minutes=random.randint(0,30))).isoformat()})
    return {"events":sorted(db_events,key=lambda e:e["created_at"],reverse=True)}

# ── YOLO Bridge ────────────────────────────────────────────────────────────────
@app.post("/yolo/detect")
async def receive_yolo(det: YoloDetection):
    now  = datetime.now().isoformat()
    conn = get_db()
    conn.execute("INSERT INTO yolo_detections (class_name,confidence,x1,y1,x2,y2,frame_w,frame_h,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (det.class_name,det.confidence,det.x1,det.y1,det.x2,det.y2,det.frame_w,det.frame_h,"yolo",now))
    conn.commit(); conn.close()
    await ws_manager.broadcast({"type":"yolo_detection","class_name":det.class_name,
        "confidence":det.confidence,"bbox":[det.x1,det.y1,det.x2,det.y2],
        "frame_w":det.frame_w,"frame_h":det.frame_h,"timestamp":now})
    return {"ok":True}

@app.get("/yolo/recent")
def get_recent_yolo():
    conn = get_db()
    rows = conn.execute("SELECT * FROM yolo_detections ORDER BY created_at DESC LIMIT 30").fetchall()
    conn.close()
    return {"detections":[dict(r) for r in rows]}

# ── Cleaning Ops ───────────────────────────────────────────────────────────────
@app.post("/cleaning")
async def log_cleaning(op: CleaningUpdate):
    now  = datetime.now().isoformat()
    conn = get_db()
    conn.execute("INSERT INTO cleaning_ops (location_id,status,started_at,notes) VALUES (?,?,?,?)",
        (op.location_id,op.status,now,op.notes))
    conn.commit(); conn.close()
    await ws_manager.broadcast({"type":"cleaning_update","location_id":op.location_id,
        "status":op.status,"timestamp":now})
    return {"success":True}

# ── Stats ──────────────────────────────────────────────────────────────────────
@app.get("/stats")
def get_stats():
    conn = get_db()
    r = conn.execute("SELECT COUNT(*) FROM citizen_reports").fetchone()[0]
    l = conn.execute("SELECT COUNT(*) FROM litter_events").fetchone()[0]
    y = conn.execute("SELECT COUNT(*) FROM yolo_detections").fetchone()[0]
    c = conn.execute("SELECT COUNT(*) FROM cleaning_ops").fetchone()[0]
    conn.close()
    return {"citizen_reports":r,"litter_events":l,"yolo_detections":y,
            "cleaning_ops":c,"ws_clients":len(ws_manager.clients)}

# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        await ws.send_json({"type":"init",
            "locations":[sim_location(l["id"]) for l in LOCATIONS],
            "timestamp":datetime.now().isoformat()})
        while True:
            await asyncio.sleep(4)
            loc  = random.choice(LOCATIONS)
            msgs = [f"⚠ Trash spike at {loc['name']}",f"👥 Crowd surge at {loc['name']}",
                    f"🚨 Litter event at {loc['name']}",f"✅ Area cleaned: {loc['name']}",
                    f"🔔 Bin overflow near {loc['name']}"]
            sevs = ["INFO","INFO","WARNING","WARNING","CRITICAL"]
            i    = random.randint(0,4)
            await ws_manager.broadcast({"type":"tick","message":msgs[i],
                "severity":sevs[i],"location":loc["name"],
                "updated_location":sim_location(loc["id"]),
                "timestamp":datetime.now().isoformat()})
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception as e:
        print(f"[WS] Error: {e}")
        ws_manager.disconnect(ws)