from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
import trimesh
import os
import uuid

app = FastAPI()

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
