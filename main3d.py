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


# Request model
class FileRequest(BaseModel):
    fileUrl: str


# Response model
class AnalyzeResponse(BaseModel):
    volume_cm3: float
    price: float


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze_stl(file_request: FileRequest):
    try:
        file_url = file_request.fileUrl
        file_name = f"{uuid.uuid4()}.stl"

        # Download STL
        with open(file_name, "wb") as f:
            f.write(requests.get(file_url).content)

        # Load STL and compute volume
        mesh = trimesh.load(file_name)

        if not mesh.is_volume:
            os.remove(file_name)
            raise Exception("STL is not watertight. Volume cannot be computed.")

        volume_cm3 = mesh.volume * 1000  # Convert from m³ to cm³

        # Calculate price
        material_cost = volume_cm3 * MATERIAL_COST_PER_CM3
        total_price = material_cost + BASE_FEE

        # Clean temp file
        os.remove(file_name)

        return {
            "volume_cm3": round(volume_cm3, 2),
            "price": round(total_price, 2)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
