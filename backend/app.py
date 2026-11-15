import os
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from postgrest import APIError
from supabase import Client, create_client


load_dotenv(Path(__file__).with_name(".env"))

app = Flask(__name__)

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY")
supabase: Client | None = (
    create_client(supabase_url, supabase_key)
    if supabase_url and supabase_key
    else None
)


@app.after_request
def add_cors_headers(response):
    """Allow browser clients from any origin to call this API."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Max-Age"] = "86400"
    return response


@app.errorhandler(APIError)
def handle_database_error(error):
    code = getattr(error, "code", None)
    if code == "23505":
        return jsonify({"error": "A registration for this user and event already exists."}), 409
    app.logger.exception("Supabase request failed")
    return jsonify({"error": "Database request failed."}), 500


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        return jsonify({"error": error.description}), error.code
    app.logger.exception("Unhandled API error")
    return jsonify({"error": "Internal server error."}), 500


@app.before_request
def ensure_supabase_configured():
    if request.method == "OPTIONS" or request.endpoint == "static":
        return None
    if supabase is None:
        return jsonify({"error": "Supabase is not configured. Set SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY."}), 503
    return None


def valid_uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


@app.get("/")
def index():
    return jsonify({"message": "Events API", "routes": [
        "GET /api/events",
        "GET /api/events/<id>",
        "POST /api/registrations",
        "GET /api/me/registrations?user_id=<uuid>",
        "GET /api/events/<id>/registrations",
    ]})


@app.get("/api/events")
def list_events():
    response = supabase.table("events").select("*").order("title").execute()
    return jsonify(response.data)


@app.get("/api/events/<event_id>")
def get_event(event_id):
    event_id = valid_uuid(event_id)
    if not event_id:
        return jsonify({"error": "id must be a valid UUID."}), 400
    response = supabase.table("events").select("*").eq("id", event_id).limit(1).execute()
    if not response.data:
        return jsonify({"error": "Event not found."}), 404
    return jsonify(response.data[0])


@app.post("/api/registrations")
def create_registration():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    user_id = valid_uuid(payload.get("user_id"))
    event_id = valid_uuid(payload.get("event_id"))
    if not user_id or not event_id:
        return jsonify({"error": "user_id and event_id must be valid UUIDs."}), 400

    event_result = supabase.table("events").select("id,capacity,status").eq("id", event_id).limit(1).execute()
    if not event_result.data:
        return jsonify({"error": "Event not found."}), 404
    event = event_result.data[0]
    user_result = supabase.table("users").select("id").eq("id", user_id).limit(1).execute()
    if not user_result.data:
        return jsonify({"error": "User not found."}), 404

    existing = (supabase.table("registrations").select("id")
                .eq("user_id", user_id).eq("event_id", event_id).limit(1).execute())
    if existing.data:
        return jsonify({"error": "User is already registered for this event."}), 409

    count_result = (supabase.table("registrations").select("id", count="exact")
                    .eq("event_id", event_id).execute())
    if event.get("capacity") is not None and (count_result.count or 0) >= event["capacity"]:
        return jsonify({"error": "Event has reached capacity."}), 409

    response = (supabase.table("registrations")
                .insert({"user_id": user_id, "event_id": event_id})
                .select("*").execute())
    return jsonify(response.data[0]), 201


@app.get("/api/me/registrations")
def current_user_registrations():
    # No auth is configured by request. The client supplies the user_id explicitly.
    user_id = valid_uuid(request.args.get("user_id"))
    if not user_id:
        return jsonify({"error": "Provide a valid user_id query parameter."}), 400
    response = (supabase.table("registrations")
                .select("*, events(*)").eq("user_id", user_id)
                .order("created_at", desc=True).execute())
    return jsonify(response.data)


@app.get("/api/events/<event_id>/registrations")
def event_registrations(event_id):
    # This route is public because authentication/authorization was explicitly excluded.
    event_id = valid_uuid(event_id)
    if not event_id:
        return jsonify({"error": "id must be a valid UUID."}), 400
    event_result = supabase.table("events").select("id").eq("id", event_id).limit(1).execute()
    if not event_result.data:
        return jsonify({"error": "Event not found."}), 404
    response = (supabase.table("registrations")
                .select("*, users(id,email,role)").eq("event_id", event_id)
                .order("created_at").execute())
    return jsonify(response.data)


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
