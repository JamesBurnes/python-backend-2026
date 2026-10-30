from http import HTTPStatus
from typing import Any, Optional
from itertools import count
from fastapi import FastAPI, HTTPException, Query, Path, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ConfigDict
import uuid

app = FastAPI(title="Shop API")

items_db: dict[int, dict[str, Any]] = {}
carts_db: dict[int, dict[str, Any]] = {}

item_id_counter = count(1)
cart_id_counter = count(1)

class ItemCreate(BaseModel):
    name: str
    price: float = Field(gt=0, description="Цена должна быть больше 0")

class ItemUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = Field(default=None, gt=0)
    model_config = ConfigDict(extra="forbid")

class ItemResponse(BaseModel):
    id: int
    name: str
    price: float
    deleted: bool = False
    model_config = ConfigDict(from_attributes=True)

def get_item_or_404(item_id: int) -> dict[str, Any]:
    item = items_db.get(item_id)
    if not item or item.get("deleted", False):
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Item not found")
    return item


def recalculate_cart_price(cart: dict[str, Any]):
    total = 0.0
    for item_in_cart in cart["items"]:
        original_item = items_db.get(item_in_cart["id"])
        if original_item:
            total += original_item["price"] * item_in_cart["quantity"]
    cart["price"] = total

@app.post("/item", status_code=HTTPStatus.CREATED, response_model=ItemResponse)
def create_item(item: ItemCreate):
    item_id = next(item_id_counter)
    new_item = {
        "id": item_id,
        "name": item.name,
        "price": item.price,
        "deleted": False
    }
    items_db[item_id] = new_item
    return new_item

@app.get("/item/{item_id}", response_model=ItemResponse)
def get_item(item_id: int = Path(..., ge=1)):
    item = items_db.get(item_id)
    if not item or item["deleted"]:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Item not found")
    return item

@app.get("/item", response_model=list[ItemResponse])
def list_items(
        offset: int = Query(0, ge=0),
        limit: int = Query(10, ge=1),
        min_price: Optional[float] = Query(None, ge=0),
        max_price: Optional[float] = Query(None, ge=0),
        show_deleted: bool = Query(False)
):
    result = []
    for item in sorted(items_db.values(), key=lambda x: x["id"]):
        if not show_deleted and item["deleted"]:
            continue
        if min_price is not None and item["price"] < min_price:
            continue
        if max_price is not None and item["price"] > max_price:
            continue
        result.append(item)
    return result[offset: offset + limit]

@app.put("/item/{item_id}", response_model=ItemResponse)
def replace_item(item_id: int = Path(..., ge=1), item: ItemCreate = None):
    existing = items_db.get(item_id)
    if not existing or existing["deleted"]:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Item not found")

    items_db[item_id].update({
        "name": item.name,
        "price": item.price
    })
    return items_db[item_id]

@app.patch("/item/{item_id}", response_model=ItemResponse)
def update_item(item_id: int = Path(..., ge=1), item: ItemUpdate = None):
    existing = items_db.get(item_id)
    if not existing or existing["deleted"]:
        if existing and existing["deleted"]:
            return Response(status_code=HTTPStatus.NOT_MODIFIED)
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Item not found")
    update_data = item.model_dump(exclude_unset=True)
    if "deleted" in update_data:
        raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail="Cannot update 'deleted' field")
    items_db[item_id].update(update_data)
    return items_db[item_id]

@app.delete("/item/{item_id}")
def delete_item(item_id: int = Path(..., ge=1)):
    item = items_db.get(item_id)
    if not item:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Item not found")
    item["deleted"] = True
    return {"status": "ok"}

@app.post("/cart", status_code=HTTPStatus.CREATED)
def create_cart(response: Response):
    cart_id = next(cart_id_counter)
    carts_db[cart_id] = {
        "id": cart_id,
        "items": [],
        "price": 0.0
    }
    response.headers["location"] = f"/cart/{cart_id}"
    return {"id": cart_id}

@app.get("/cart/{cart_id}")
def get_cart(cart_id: int = Path(..., ge=1)):
    cart = carts_db.get(cart_id)
    if not cart:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Cart not found")
    return cart

@app.get("/cart")
def list_carts(
        offset: int = Query(0, ge=0),
        limit: int = Query(10, ge=1),
        min_price: Optional[float] = Query(None, ge=0),
        max_price: Optional[float] = Query(None, ge=0),
        min_quantity: Optional[int] = Query(None, ge=0),
        max_quantity: Optional[int] = Query(None, ge=0)
):
    result = []
    for cart in sorted(carts_db.values(), key=lambda x: x["id"]):
        recalculate_cart_price(cart)
        total_quantity = sum(item["quantity"] for item in cart["items"])
        if min_price is not None and cart["price"] < min_price:
            continue
        if max_price is not None and cart["price"] > max_price:
            continue
        if min_quantity is not None and total_quantity < min_quantity:
            continue
        if max_quantity is not None and total_quantity > max_quantity:
            continue
        result.append(cart)
    return result[offset: offset + limit]

@app.post("/cart/{cart_id}/add/{item_id}")
def add_item_to_cart(cart_id: int = Path(..., ge=1), item_id: int = Path(..., ge=1)):
    cart = carts_db.get(cart_id)
    if not cart:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Cart not found")
    item = items_db.get(item_id)
    if not item or item["deleted"]:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Item not found")
    found = False
    for cart_item in cart["items"]:
        if cart_item["id"] == item_id:
            cart_item["quantity"] += 1
            found = True
            break
    if not found:
        cart["items"].append({
            "id": item_id,
            "name": item["name"],
            "quantity": 1,
            "available": True
        })
    recalculate_cart_price(cart)
    return {"status": "ok"}


active_connections: dict[str, list[WebSocket]] = {}

@app.websocket("/chat/{chat_name}")
async def websocket_endpoint(websocket: WebSocket, chat_name: str):
    await websocket.accept()

    if chat_name not in active_connections:
        active_connections[chat_name] = []

    active_connections[chat_name].append(websocket)
    username = f"user_{uuid.uuid4().hex[:4]}"

    try:
        while True:
            data = await websocket.receive_text()
            broadcast_message = f"{username} :: {data}"
            for connection in active_connections[chat_name]:
                await connection.send_text(broadcast_message)

    except WebSocketDisconnect:
        active_connections[chat_name].remove(websocket)
        if not active_connections[chat_name]:
            del active_connections[chat_name]