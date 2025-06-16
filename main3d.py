from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
import trimesh
import os
import uuid

app = FastAPI()

MATERIAL_COST_PER_CM3 = 0.35
BASE_FEE = 3.00


class FileRequest(BaseModel):
    fileUrl: str


@app.post("/analyze")
def analyze_stl(file_request: FileRequest):
    try:
        file_url = file_request.fileUrl
        file_name = f"{uuid.uuid4()}.stl"

        with open(file_name, "wb") as f:
            f.write(requests.get(file_url).content)

        mesh = trimesh.load(file_name)

        # 🔥 Disable watertight check
        # if not mesh.is_volume:
        #     raise Exception("STL is not watertight. Volume cannot be computed.")

        volume_cm3 = mesh.volume

        material_cost = volume_cm3 * MATERIAL_COST_PER_CM3
        total_price = material_cost + BASE_FEE

        os.remove(file_name)

        return {
            "volume_cm3": round(volume_cm3, 2),
            "price": round(total_price, 2)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
