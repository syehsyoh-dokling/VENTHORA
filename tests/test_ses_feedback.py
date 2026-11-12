# ruff: noqa: E501
"""Amazon SES bounces and complaints arrive by Amazon SNS: only a signed message on our topic is believed; a permanent
bounce marks the message bounced (never retried) and the address as an issue; a person corrects the address and it goes
out again."""
import base64
import datetime as dt
import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from venthora import api, outbox

TOPIC = "arn:aws:sns:us-east-1:111122223333:venthora-ses-feedback"
CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem"
FCO = "role=super_admin&by=Rosa%20Lindqvist"


@pytest.fixture(scope="module")
def key():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + dt.timedelta(days=1)).sign(k, hashes.SHA256()))
    return k, cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def client(monkeypatch, key):
    monkeypatch.setenv("VENTHORA_SES_TOPIC_ARN", TOPIC)
    monkeypatch.setitem(api._SNS_CERTS, CERT_URL, key[1])
    with TestClient(api.app) as c:
        yield c


def signed(key, msg: dict, version="1") -> dict:
    msg = {"TopicArn": TOPIC, "SigningCertURL": CERT_URL, "SignatureVersion": version, "Timestamp": "2026-11-12T00:00:00Z", "MessageId": "m-1"} | msg
    keys = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
    text = "".join(f"{k}\n{msg[k]}\n" for k in keys if msg.get(k) is not None)
    algo = hashes.SHA256() if version == "2" else hashes.SHA1()
    return msg | {"Signature": base64.b64encode(key[0].sign(text.encode(), padding.PKCS1v15(), algo)).decode()}


def bounce(msg_id: str, address: str) -> str:
    return json.dumps({"notificationType": "Bounce", "mail": {"headers": [{"name": "X-Venthora-Outbox-Id", "value": msg_id}]},
                       "bounce": {"bounceType": "Permanent", "bounceSubType": "General",
                                  "bouncedRecipients": [{"emailAddress": address, "diagnosticCode": "smtp; 550 5.1.1 user unknown"}]}})


class Up:
    sent: list = []

    def __init__(self, host, port, timeout=15):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, m):
        Up.sent.append((m["To"], m["X-Venthora-Outbox-Id"]))

    def quit(self):
        pass


def test_a_permanent_bounce_marks_the_message_and_a_corrected_address_sends_it_again(client, key, monkeypatch):
    org = client.app.state.org
    monkeypatch.setattr(outbox.smtplib, "SMTP", Up)
    monkeypatch.setattr(outbox.secret_values, "get", lambda k: "pw")
    monkeypatch.setattr(outbox.secret_values, "info", lambda k: {"set": True})
    outbox.save_smtp(org, {"host": "email-smtp.us-east-1.amazonaws.com", "port": 587, "user": "u", "sender": "pay@muwahid.org", "security": "starttls"}, "Rosa")
    rec = outbox.queue(org, to=["wrong@consultant.example"], subject="Call-back", body="x", ref="REQ-7", by="Rosa")
    assert outbox.deliver(org, rec["_id"])["status"] == "sent" and Up.sent[-1] == ("wrong@consultant.example", rec["_id"])

    r = client.post("/webhooks/ses", content=json.dumps(signed(key, {"Type": "Notification", "Message": bounce(rec["_id"], "wrong@consultant.example")})))
    assert r.status_code == 200 and r.json()["status"] == "bounced"
    assert org.get("outbox", rec["_id"])["status"] == "bounced" and outbox.flush(org) == []  # never retried to a dead address
    issues = client.get("/v1/outbox/issues").json()
    assert [i["address"] for i in issues if not i["resolved"]] == ["wrong@consultant.example"]

    fixed = client.post(f"/v1/outbox/{rec['_id']}/readdress?{FCO}", json={"to": ["right@consultant.example"]}).json()
    assert fixed["status"] == "sent" and fixed["readdressed_from"] == ["wrong@consultant.example"] and Up.sent[-1][0] == "right@consultant.example"
    assert all(i["resolved"] for i in client.get("/v1/outbox/issues").json() if i["address"] == "wrong@consultant.example")


def test_only_a_signed_message_on_our_topic_is_believed(client, key):
    good = signed(key, {"Type": "Notification", "Message": bounce("nope", "x@y.example")}, version="2")
    assert client.post("/webhooks/ses", content=json.dumps(good)).status_code == 404  # signed, but not one of our messages
    forged = good | {"Message": bounce("other", "x@y.example")}
    assert client.post("/webhooks/ses", content=json.dumps(forged)).status_code == 403
    other_topic = signed(key, {"Type": "Notification", "Message": "{}", "TopicArn": "arn:aws:sns:us-east-1:999:evil"})
    assert client.post("/webhooks/ses", content=json.dumps(other_topic)).status_code == 403
    off_aws = signed(key, {"Type": "Notification", "Message": "{}", "SigningCertURL": "https://evil.example/cert.pem"})
    assert client.post("/webhooks/ses", content=json.dumps(off_aws)).status_code == 403
