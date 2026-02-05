from fastapi import FastAPI, Request
from fastapi import HTTPException
import os
import asyncio
from typing import List
from fastapi import Query

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "ok"}
