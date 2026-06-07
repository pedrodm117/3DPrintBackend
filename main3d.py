from datetime import datetime

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel
import requests
import trimesh
import os
import uuid

from handshake_bot import AppliedJobsTracker, HandshakeBot, CONFIG_FILE, load_config

app = FastAPI()

# ---- Handshake automation state ----
_handshake_state: dict = {"running": False, "last_run": None, "last_results": None}

# ----- CONFIG -----
MATERIAL_COST_PER_CM3 = 0.35  # $ per cm³
BASE_FEE = 3.00               # flat fee
# -------------------

class FileRequest(BaseModel):
    fileUrl: str


@app.post("/analyze")
def analyze_stl(file_request: FileRequest):
    try:
        # Download STL file
        file_url = file_request.fileUrl
        file_name = f"{uuid.uuid4()}.stl"

        response = requests.get(file_url)
        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to download STL file.")

        with open(file_name, "wb") as f:
            f.write(response.content)

        # Load STL and compute volume
        mesh = trimesh.load(file_name)

        if not mesh.is_volume:
            os.remove(file_name)
            raise HTTPException(status_code=500, detail="STL is not watertight. Volume cannot be computed.")

        # Check bounds for debugging
        bounds = mesh.bounds
        print(f"Mesh bounds: {bounds}")

        # ⚠️ SCALE: Assuming STL is in millimeters, convert to centimeters
        mesh.apply_scale(0.1)  # Convert from mm to cm

        # Compute volume (in cm³)
        volume_cm3 = mesh.volume

        # Pricing calculation
        material_cost = volume_cm3 * MATERIAL_COST_PER_CM3
        total_price = material_cost + BASE_FEE

        # Clean up temp file
        os.remove(file_name)

        return {
            "volume_cm3": round(volume_cm3, 2),
            "price": round(total_price, 2)
        }

    except Exception as e:
        if os.path.exists(file_name):
            os.remove(file_name)
        raise HTTPException(status_code=500, detail=str(e))


# ---- Handshake job automation endpoints ----

async def _run_handshake_bot() -> None:
    try:
        config = load_config()
        bot = HandshakeBot(config)
        results = await bot.run()
        _handshake_state["last_results"] = results
        _handshake_state["last_run"] = datetime.now().isoformat()
    except Exception as exc:
        _handshake_state["last_results"] = [{"error": str(exc)}]
    finally:
        _handshake_state["running"] = False


@app.post("/handshake/run")
async def start_handshake_run(background_tasks: BackgroundTasks):
    if _handshake_state["running"]:
        raise HTTPException(status_code=409, detail="A run is already in progress.")
    if not CONFIG_FILE.exists():
        raise HTTPException(
            status_code=400,
            detail="handshake_config.json not found. Copy handshake_config.example.json and fill in your details.",
        )
    _handshake_state["running"] = True
    background_tasks.add_task(_run_handshake_bot)
    return {"message": "Handshake automation started in the background."}


@app.get("/handshake/status")
def get_handshake_status():
    return {
        "running": _handshake_state["running"],
        "last_run": _handshake_state["last_run"],
        "last_results": _handshake_state["last_results"],
    }


@app.get("/handshake/applications")
def list_applications():
    return AppliedJobsTracker().all_applications()
