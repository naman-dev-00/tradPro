import uuid

def get_valid_payload():
    return {
        "id": "7b5ef35b-1175-430c-ab23-f22287955c45",
        "name": "Nifty RSI Touch",
        "description": "Validation test strategy",
        "timeframe": "15m",
        "candidate_selection_mode": "FIRST_ELIGIBLE",
        "global_conditions": {
            "type": "CONDITION",
            "lhs": {
                "indicator": "PRICE",
                "symbol": "NIFTY"
            },
            "operator": "GREATER_THAN",
            "rhs": {
                "type": "NUMBER",
                "value": 20000.0
            }
        },
        "action": {
            "type": "PAPER_TRADE",
            "risk_config": {
                "max_position_size": 10000.0,
                "stop_loss_pct": 1.5,
                "take_profit_pct": 3.0,
                "validity_window": 3
            }
        }
    }

def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}

def test_validate_endpoint_valid(client):
    payload = get_valid_payload()
    response = client.post("/strategies/validate", json=payload)
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["valid"] is True
    assert len(res_data["errors"]) == 0

def test_validate_endpoint_invalid_empty_conditions(client):
    payload = get_valid_payload()
    payload["global_conditions"] = None
    payload["candidate_conditions"] = None
    response = client.post("/strategies/validate", json=payload)
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["valid"] is False
    assert len(res_data["errors"]) > 0

def test_create_strategy_success(client):
    payload = get_valid_payload()
    response = client.post("/strategies", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["id"] == payload["id"]
    assert data["name"] == payload["name"]

def test_create_strategy_duplicate_id_fails(client):
    payload = get_valid_payload()
    # Create first
    resp1 = client.post("/strategies", json=payload)
    assert resp1.status_code == 201
    # Create second with same ID
    resp2 = client.post("/strategies", json=payload)
    assert resp2.status_code == 400

def test_list_strategies(client):
    payload = get_valid_payload()
    client.post("/strategies", json=payload)

    response = client.get("/strategies")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == payload["id"]

def test_get_strategy_by_id(client):
    payload = get_valid_payload()
    client.post("/strategies", json=payload)

    response = client.get(f"/strategies/{payload['id']}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == payload["id"]

def test_get_strategy_not_found(client):
    rand_uuid = str(uuid.uuid4())
    response = client.get(f"/strategies/{rand_uuid}")
    assert response.status_code == 404

def test_update_strategy_success(client):
    payload = get_valid_payload()
    client.post("/strategies", json=payload)

    # Update payload name
    payload["name"] = "Updated Strategy Name"
    response = client.put(f"/strategies/{payload['id']}", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Strategy Name"
