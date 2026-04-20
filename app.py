from flask import Flask, request, jsonify
import datetime

app = Flask(__name__)

# 🔐 Dummy license database (later we move to real DB)
licenses = {
    "ABC123": {
        "machine_id": None,
        "expiry": "2027-04-20",
        "type": "1YEAR"
    },
    "LIFE999": {
        "machine_id": None,
        "expiry": None,
        "type": "LIFETIME"
    }
}

@app.route("/")
def home():
    return "Devi Payroll License API Running"

@app.route("/activate", methods=["POST"])
def activate():
    data = request.json
    license_key = data.get("license_key")
    machine_id = data.get("machine_id")

    if license_key not in licenses:
        return jsonify({"status": "error", "message": "Invalid License"}), 400

    lic = licenses[license_key]

    # First time activation
    if lic["machine_id"] is None:
        lic["machine_id"] = machine_id
        return jsonify({"status": "success", "message": "Activated successfully"})

    # Already activated → check machine
    if lic["machine_id"] != machine_id:
        return jsonify({"status": "error", "message": "License already used on another device"}), 403

    return jsonify({"status": "success", "message": "Already activated"})


@app.route("/validate", methods=["POST"])
def validate():
    data = request.json
    license_key = data.get("license_key")
    machine_id = data.get("machine_id")

    if license_key not in licenses:
        return jsonify({"status": "error", "message": "Invalid License"}), 400

    lic = licenses[license_key]

    if lic["machine_id"] != machine_id:
        return jsonify({"status": "error", "message": "Unauthorized machine"}), 403

    if lic["type"] != "LIFETIME":
        expiry_date = datetime.datetime.strptime(lic["expiry"], "%Y-%m-%d").date()
        today = datetime.date.today()

        if today > expiry_date:
            return jsonify({"status": "expired", "message": "License expired"}), 403

    return jsonify({"status": "valid", "message": "License valid"})


if __name__ == "__main__":
    app.run(debug=True)